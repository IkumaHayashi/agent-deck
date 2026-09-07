#!/usr/bin/env python3
"""記録が始まる前に消した worktree の会話を、再開できるよう記録し直す。

Agent Deck は Issue / PR 用 worktree の作成元を worktrees.json へ残し、
セッション終了で消えても同じパスへ作り直して会話を再開する。この記録は
v0.1.49 から始まったので、それ以前に消した worktree の会話は再開一覧に
出てこない。

このスクリプトは会話ログに残る cwd を読み、worktree のパス名から作成元の
リポジトリと Issue / PR 番号を逆算して記録を補う。

    python3 tools/backfill-worktrees.py           # 何が記録されるか見るだけ
    python3 tools/backfill-worktrees.py --apply   # 実際に記録する

逆算した作成元から worktree のパスを組み直し、**元のパスと完全に一致する
ものだけ**を記録する。会話ログの保存先は cwd から決まるため、別の場所へ
作り直しても `--resume` は会話を見つけられないからである。この検証を通らない
古いパス形式（`{リポジトリ名}-issue-{番号}` など、命名規則を変える前のもの）は
理由を添えて飛ばす。
"""

import argparse
import glob
import importlib.util
import os
import re
import subprocess
import sys

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server.py")
SPEC = importlib.util.spec_from_file_location("agent_deck_server", MODULE_PATH)
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)

# 現行の worktree 名: {リポジトリslug}-{common git dirのsha10}-{issue|pull}-{番号}
WORK_ITEM_RE = re.compile(
    r"^(?P<slug>.+)-(?P<key>[0-9a-f]{10})-(?P<label>issue|pull)-(?P<number>\d+)$"
)
# レビュー用 worktree 名: {リポジトリのディレクトリ名}-pr-{番号}
REVIEW_RE = re.compile(r"^(?P<name>.+)-pr-(?P<number>\d+)$")
# sha10 を挟まない、命名規則を変える前の worktree 名
LEGACY_RE = re.compile(r"^(?P<name>.+)-(?P<label>issue|pull)-(?P<number>\d+)$")


def worktree_root(cwd):
    """worktree 置き場の直下まで遡り、worktree 本体のパスを返す。

    会話の途中で worktree 内へ移ると cwd は配下のディレクトリになるが、
    記録も作り直しも worktree 本体が単位になる。
    """
    relative = os.path.relpath(cwd, server.WORKTREES_DIR)
    return os.path.join(server.WORKTREES_DIR, relative.split(os.sep)[0])


def conversation_cwds():
    """会話ログに残る作業ディレクトリを重複なしで返す。"""
    found = {}
    # claude はプロジェクト名が cwd 由来なので、worktree 置き場のものだけ読む
    prefix = server.claude_project_dir(server.WORKTREES_DIR)
    for path in glob.glob(f"{server.HOME}/.claude/projects/{prefix}*/*.jsonl"):
        cwd = server.claude_session_cwd(path)
        if cwd:
            found.setdefault(os.path.realpath(cwd), path)
    for path in glob.glob(f"{server.HOME}/.codex/sessions/**/*.jsonl", recursive=True):
        cwd = server.codex_session_head(path)["cwd"]
        if cwd:
            found.setdefault(os.path.realpath(cwd), path)
    return found


def candidate_repositories():
    """作成元になりうるローカルリポジトリを、GitHub名つきで返す。"""
    repositories = []
    for name, path in server.local_github_repositories().items():
        repositories.append({"repo": os.path.realpath(path), "repository": name})
    return repositories


def resolve_source(path, repositories):
    """worktree のパスから作成元を逆算する。組み直せなければ理由を返す。"""
    name = os.path.basename(path)
    work_item = WORK_ITEM_RE.fullmatch(name)
    review = REVIEW_RE.fullmatch(name)
    if not work_item and not review:
        if LEGACY_RE.fullmatch(name):
            # 作り直すと現行規則の別パスになり、会話ログを引けなくなる
            return None, "命名規則が変わる前のworktreeなので作り直せません"
        return None, "worktree名がIssue / PR用の形式ではありません"
    for candidate in repositories:
        repo = candidate["repo"]
        if work_item:
            kind = "issue" if work_item.group("label") == "issue" else "pull"
            number = int(work_item.group("number"))
            try:
                common_dir = server.git_common_directory(repo)
            except (OSError, subprocess.SubprocessError, RuntimeError):
                continue
            target = {
                "cwd": repo,
                "kind": kind,
                "number": number,
                "repositoryName": candidate["repository"],
            }
            rebuilt = server.github_work_item_worktree_path(target, common_dir)
        else:
            kind = "review"
            number = int(review.group("number"))
            rebuilt = os.path.join(
                server.WORKTREES_DIR,
                f"{os.path.basename(repo.rstrip('/'))}-pr-{number}",
            )
        # 同じ場所へ作り直せることを、パスの組み直しで確かめてから記録する
        if os.path.realpath(rebuilt) == os.path.realpath(path):
            return {
                "path": path,
                "repo": repo,
                "kind": kind,
                "number": number,
                "repository": candidate["repository"],
            }, ""
    return None, "作成元リポジトリを特定できません（命名規則が変わる前のworktree）"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="実際に worktrees.json へ記録する"
    )
    args = parser.parse_args()

    repositories = candidate_repositories()
    if not repositories:
        print("作成元の候補になるリポジトリが設定されていません", file=sys.stderr)
        return 1

    resolved, skipped = {}, {}
    for cwd, log in sorted(conversation_cwds().items()):
        if not server.path_is_within(cwd, server.WORKTREES_DIR):
            continue
        # 配下で終わった会話も、worktree 本体の記録があれば一緒に戻せる
        root = worktree_root(cwd)
        if root in resolved or root in skipped:
            continue
        # 生きている worktree も、次に終了したとき作り直せるよう記録しておく
        if server.worktree_record(root):
            continue
        record, reason = resolve_source(root, repositories)
        if record:
            record["log"] = log
            resolved[root] = record
        else:
            skipped[root] = reason
    resolved, skipped = list(resolved.values()), sorted(skipped.items())

    for record in resolved:
        state = "現存" if os.path.isdir(record["path"]) else "削除済み"
        print(
            f"[記録] {os.path.basename(record['path'])}"
            f" → {record['repository']} {record['kind']}#{record['number']}（{state}）"
        )
    for cwd, reason in skipped:
        print(f"[飛ばす] {os.path.basename(cwd)}: {reason}")

    print(
        f"\n記録できる worktree: {len(resolved)}件 / 飛ばした worktree: {len(skipped)}件"
    )
    if not args.apply:
        print("--apply を付けると worktrees.json へ書き込みます")
        return 0
    for record in resolved:
        server.remember_worktree(
            record["path"],
            record["repo"],
            record["kind"],
            record["number"],
            record["repository"],
        )
    print(f"{server.WORKTREE_REGISTRY_PATH} へ {len(resolved)}件を記録しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
