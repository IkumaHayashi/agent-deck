#!/usr/bin/env python3
"""Agent Deck — AI コーディング CLI（Claude Code / Codex）の Web ランチャー & セッションマネージャ

信頼できるネットワーク（既定では Tailscale 網内と localhost）からのみアクセス可能。
プロジェクトのボタンをタップすると Mac 上の wezterm に新規タブを開いて
エージェント CLI を起動する（同梱の deck スクリプト経由）。

設定は ~/.config/agent-deck/config.json から読む（AGENT_DECK_CONFIG で変更可）。
設定ファイルが無くてもすべて既定値で動く。
"""
import base64
import binascii
import datetime
import hashlib
import html
import glob
import ipaddress
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
import urllib.parse
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.expanduser("~")
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
CONFIG_PATH = os.environ.get("AGENT_DECK_CONFIG") or f"{HOME}/.config/agent-deck/config.json"


def read_version():
    try:
        with open(os.path.join(SCRIPT_DIR, "VERSION"), encoding="utf-8") as source:
            return source.read().strip()
    except OSError:
        return "0.0.0"


def load_config(path=None):
    """設定ファイルを読む。無ければ空 dict（すべて既定値で動く）。"""
    try:
        with open(path or CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


CONFIG = load_config()
VERSION = read_version()
UPDATE_REPO = CONFIG.get("update_repo", "IkumaHayashi/agent-deck")
UPDATE_CACHE = {"at": 0.0, "data": None}
UPDATE_LOCK = threading.Lock()
UPDATE_TTL = 3600


def _expand(path):
    return os.path.expanduser(path)


def find_bin(name, configured=None):
    """CLI の実行パスを解決する。launchd の最小 PATH でも動くよう既定の場所も探す。"""
    if configured:
        return _expand(configured)
    found = shutil.which(name)
    if found:
        return found
    for prefix in (f"{HOME}/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"):
        candidate = os.path.join(prefix, name)
        if os.path.exists(candidate):
            return candidate
    return name


def version_tuple(value):
    """比較用の SemVer 3要素。v0.1.0 以外のタグは更新対象にしない。"""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", value or "")
    return tuple(map(int, match.groups())) if match else None


def latest_release(force=False):
    """GitHub Releases の最新版を1時間キャッシュして返す。取得失敗は非表示扱い。"""
    with UPDATE_LOCK:
        if not force and UPDATE_CACHE["data"] is not None \
                and UPDATE_CACHE["at"] + UPDATE_TTL > time.time():
            return UPDATE_CACHE["data"]
    request = urllib.request.Request(
        f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "agent-deck"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.load(response)
        tag = payload.get("tag_name", "")
        latest = version_tuple(tag)
        current = version_tuple(VERSION)
        data = {
            "current": VERSION,
            "latest": tag.removeprefix("v"),
            "available": bool(latest and current and latest > current),
            "url": payload.get("html_url", ""),
        }
    except (OSError, ValueError, json.JSONDecodeError):
        data = {"current": VERSION, "latest": "", "available": False, "url": ""}
    with UPDATE_LOCK:
        UPDATE_CACHE.update(at=time.time(), data=data)
    return data


def install_release(tag):
    """指定Releaseへfast-forwardする。ローカル変更や履歴の巻き戻しは拒否する。"""
    if not version_tuple(tag):
        raise ValueError("更新先のバージョンが不正です")
    git = find_bin("git")
    status = subprocess.run(
        [git, "status", "--porcelain"], cwd=SCRIPT_DIR,
        capture_output=True, text=True, timeout=10,
    )
    if status.returncode != 0:
        raise RuntimeError(status.stderr.strip() or "Gitの状態を確認できませんでした")
    if status.stdout.strip():
        raise RuntimeError("ローカル変更があるため更新できません。変更をcommitまたは退避してください")
    release_tag = f"v{tag.removeprefix('v')}"
    fetched = subprocess.run(
        [git, "fetch", "--tags", "origin", release_tag], cwd=SCRIPT_DIR,
        capture_output=True, text=True, timeout=60,
    )
    if fetched.returncode != 0:
        raise RuntimeError(fetched.stderr.strip() or "Releaseを取得できませんでした")
    merged = subprocess.run(
        [git, "merge", "--ff-only", f"refs/tags/{release_tag}"], cwd=SCRIPT_DIR,
        capture_output=True, text=True, timeout=30,
    )
    if merged.returncode != 0:
        raise RuntimeError(merged.stderr.strip() or "Releaseへ更新できませんでした")
    if read_version() != tag.removeprefix("v"):
        raise RuntimeError("更新後のバージョンを確認できませんでした")


def restart_server():
    os.execv(sys.executable, [sys.executable, *sys.argv])


# 優先順位: --port フラグ > AGENT_DECK_PORT > 設定ファイルの port > 8787
PORT = int(os.environ.get("AGENT_DECK_PORT") or CONFIG.get("port", 8787))
# サーバ再起動を検知して開きっぱなしのページを自動リロードさせるための ID
BOOT_ID = uuid.uuid4().hex
DATA_DIR = _expand(CONFIG.get("data_dir", "~/.local/share/agent-deck"))
TMUX = find_bin("tmux", CONFIG.get("tmux_bin"))
WEZTERM = find_bin("wezterm", CONFIG.get("wezterm_bin"))
# Chatwork 受信箱（任意機能）: account_id を設定し token ファイルがあるときだけ有効
CW_CONF = CONFIG.get("chatwork") or {}
CW_API = "https://api.chatwork.com/v2"
CW_ACCOUNT_ID = CW_CONF.get("account_id")
CW_TOKEN_PATH = _expand(CW_CONF.get("token_path", "~/.chatwork-token"))
CW_ENABLED = bool(CW_ACCOUNT_ID) and os.path.exists(CW_TOKEN_PATH)
CW_CACHE_PATH = f"{DATA_DIR}/cw_cache.json"
# 過去の会話の画像も表示し続けられるよう、tmp ではなく永続領域に置く
UPLOAD_DIR = f"{DATA_DIR}/uploads"
# 過去ログに残る旧保存先の添付も表示できるよう、パス検出の対象に含める
UPLOAD_PATH_PREFIXES = [DATA_DIR] + [_expand(p) for p in CONFIG.get("legacy_upload_dirs", [])]
# 会話ログ中の「添付画像: <パス>」等を検出する正規表現（Python 側）
UPLOAD_MENTION_RE = re.compile(
    r"(?:添付画像[:：]|\[Image:(?:\s*source:)?)\s*"
    r"(?:" + "|".join(re.escape(p) for p in UPLOAD_PATH_PREFIXES) + r")"
    r"/uploads/[^\s\]]+\]?"
)


def _js_regex_escape(path):
    """JS の正規表現リテラルへ埋め込むためのエスケープ（/ も含む）。"""
    return re.sub(r"([.\\+*?\[\]^$(){}|/])", r"\\\1", path)


# 同じ検出を行うクライアント側正規表現に埋め込むプレフィックスの選択肢
UPLOAD_PREFIX_ALT_JS = "|".join(_js_regex_escape(p) for p in UPLOAD_PATH_PREFIXES)
CW_LOCK = threading.Lock()
SESSION_CACHE_LOCK = threading.Lock()
SESSION_CACHE = {"expires": 0, "items": [], "loading": False, "loaded": False}
SESSION_CACHE_TTL = 5
# ログの要約/最終メッセージを (mtime, size) をキーにメモ化する。
# 会話ログは追記されない限り再パースしない。
LOG_META_LOCK = threading.Lock()
LOG_META_CACHE = {}
LOG_META_LIMIT = 400
# codexログの先頭 session_meta（id/cwd）は書き換わらないのでパス単位で保持する。
CODEX_HEAD_LOCK = threading.Lock()
CODEX_HEAD_CACHE = {}
# claudeログの cwd も書き換わらないのでパス単位で保持する。
CLAUDE_CWD_LOCK = threading.Lock()
CLAUDE_CWD_CACHE = {}
# WezTerm pane → 会話ログの解決は ps + glob を伴うので、1秒ポーリングに備えて短くキャッシュする。
WEZ_VIEW_LOCK = threading.Lock()
WEZ_VIEW_CACHE = {}
WEZ_VIEW_TTL = 5
# 返事待ちセッションの「誰のアクション待ちか」分類。haiku 呼び出しは数秒かかるので
# バックグラウンドで実行し、(mtime, size) キーで結果をメモ化する。
WAIT_CLASS_LOCK = threading.Lock()
WAIT_CLASS_CACHE = {}
WAIT_CLASS_PENDING = set()
WAIT_CLASS_RETRY = 120  # 分類失敗時に再試行するまでの秒数
WAIT_CLASS_MODEL = CONFIG.get("wait_classifier_model", "haiku")
CLAUDE_BIN = find_bin("claude", CONFIG.get("claude_bin"))
CODEX_BIN = find_bin("codex", CONFIG.get("codex_bin"))
CW_INITIAL_ROOM_LIMIT = 20
CW_MESSAGE_LIMIT = 20
DECK_CLI = os.path.join(SCRIPT_DIR, "deck")
# 起動ボタンに並べるプロジェクト。すべて設定ファイルで指定する。
# project_bases: 直下のディレクトリをドロップダウンに列挙する親ディレクトリ
# pinned: 固定ボタンにする [{"label": ..., "path": ...}]
# extra_projects: project_bases の外にある個別プロジェクト（ドロップダウンの末尾）
PROJECT_BASES = [_expand(p) for p in CONFIG.get("project_bases", [])]
# 「最近の会話を再開」に出す会話を cwd で限定するパス（未設定なら home 配下すべて）
RECENT_DIRS = [_expand(p) for p in CONFIG.get("recent_dirs", [])]
PINNED = [(item["label"], _expand(item["path"])) for item in CONFIG.get("pinned", [])]
EXTRA_PROJECTS = [
    (item["label"], _expand(item["path"])) for item in CONFIG.get("extra_projects", [])
]

# ツール名 → deck の起動コマンド。起動する CLI は TAB_BIN で差し替える。
TOOLS = {
    "claude": ["/usr/bin/env", f"TAB_BIN={CLAUDE_BIN}", DECK_CLI],
    "codex": ["/usr/bin/env", f"TAB_BIN={CODEX_BIN}", DECK_CLI],
}
# (value, 表示ラベル) — "default" は --model を付けずに起動
DEFAULT_MODELS = {
    "claude": [
        ("default", "デフォルト"),
        ("fable", "fable"),
        ("opus", "opus"),
        ("sonnet", "sonnet"),
        ("haiku", "haiku"),
    ],
    "codex": [
        ("default", "デフォルト"),
        ("gpt-5.6", "5.6"),
        ("gpt-5.6-luna", "luna"),
        ("gpt-5.6-terra", "terra"),
        ("gpt-5.6-pro", "pro"),
    ],
}
MODELS_BY_TOOL = {
    tool: [tuple(m) for m in CONFIG.get("models", {}).get(tool, defaults)]
    for tool, defaults in DEFAULT_MODELS.items()
}
# 権限バイパス起動時に渡すフラグ。
# Codex は --dangerously-bypass-... だとサンドボックスまで外れるため、
# 確認なし + 書き込みはワークスペース内に限定する組み合わせを使う。
BYPASS_FLAGS = {
    "claude": ["--dangerously-skip-permissions"],
    "codex": ["-a", "never", "-s", "workspace-write"],
}

# 全ページ共通のファビコン（/favicon.svg で配信）。
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    '<defs><linearGradient id="g" x1="12" y1="8" x2="88" y2="92" gradientUnits="userSpaceOnUse">'
    '<stop stop-color="#8B7CF6"/><stop offset="1" stop-color="#5546D7"/>'
    '</linearGradient></defs>'
    '<rect width="100" height="100" rx="23" fill="#171523"/>'
    '<rect x="19" y="16" width="58" height="68" rx="10" fill="#343047" '
    'transform="rotate(-8 48 50)"/>'
    '<rect x="27" y="16" width="58" height="68" rx="10" fill="url(#g)"/>'
    '<path d="M42 39l11 10-11 10" fill="none" stroke="#fff" stroke-width="7" '
    'stroke-linecap="round" stroke-linejoin="round"/>'
    '<path d="M57 60h12" fill="none" stroke="#fff" stroke-width="7" stroke-linecap="round"/>'
    "</svg>"
)

# ツール名の代わりに表示するアイコン。ロゴはライセンス上リポジトリに同梱せず、
# icons/fetch.sh で公式配布元から取得したときだけ起動時に読み込む（icons/README.md）。
# ファイルが無いツールはテキスト表示にフォールバックする。
ICON_DIR = os.path.join(SCRIPT_DIR, "icons")


def load_tool_icons():
    icons = {}
    for tool in ("claude", "codex", "wezterm"):
        try:
            with open(os.path.join(ICON_DIR, f"{tool}.png"), "rb") as fp:
                icons[tool] = fp.read()
        except OSError:
            pass
    return icons


TOOL_ICONS = load_tool_icons()

# アクセスを許可するネットワーク。既定は Tailscale 網内 + localhost のみ。
# 認証は無いので、信頼できる端末しかいない網以外へ広げないこと。
ALLOWED_NETS = [
    ipaddress.ip_network(net)
    for net in CONFIG.get("allowed_networks", [
        "100.64.0.0/10",        # Tailscale CGNAT
        "fd7a:115c:a1e0::/48",  # Tailscale IPv6
        "127.0.0.0/8",
        "::1/128",
    ])
]


def client_allowed(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return False
    return any(ip in net for net in ALLOWED_NETS)


def list_other_projects():
    pinned_paths = {p for _, p in PINNED}
    items = []
    for base in PROJECT_BASES:
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            path = os.path.join(base, name)
            if os.path.isdir(path) and not name.startswith(".") and path not in pinned_paths:
                items.append((name, path))
    for name, path in EXTRA_PROJECTS:
        if os.path.isdir(path) and path not in pinned_paths:
            items.append((name, path))
    return items


def find_wezterm_sock():
    """生きている wezterm GUI の socket を返す（なければ None）"""
    runtime = f"{HOME}/.local/share/wezterm"
    try:
        socks = sorted(
            (f for f in os.listdir(runtime) if f.startswith("gui-sock-")),
            key=lambda f: os.path.getmtime(os.path.join(runtime, f)),
            reverse=True,
        )
    except FileNotFoundError:
        return None
    for s in socks:
        pid = s.replace("gui-sock-", "")
        try:
            os.kill(int(pid), 0)
            return os.path.join(runtime, s)
        except (ProcessLookupError, ValueError, PermissionError):
            continue
    return None


def wezterm_cli(*args, timeout=10):
    sock = find_wezterm_sock()
    if not sock:
        return None
    env = dict(os.environ, WEZTERM_UNIX_SOCKET=sock)
    return subprocess.run(
        [WEZTERM, "cli", *args],
        capture_output=True, text=True, timeout=timeout, env=env,
    )


def wezterm_panes():
    """WezTerm pane の詳細情報を返す。"""
    try:
        out = wezterm_cli("list", "--format", "json")
        if out is None or out.returncode != 0:
            return []
        return json.loads(out.stdout or "[]")
    except (json.JSONDecodeError, OSError, subprocess.SubprocessError):
        return []


def argv_model(argv):
    """起動コマンドの --model 指定を返す。ログにまだ応答が無い間の表示に使う。"""
    for index, arg in enumerate(argv):
        if arg == "--model" and index + 1 < len(argv):
            return argv[index + 1]
        if arg.startswith("--model="):
            return arg.split("=", 1)[1]
    return ""


# TUI として動かない codex のサブコマンド。初期プロンプトの位置引数
# （codex "..."）はサブコマンドではなく TUI 起動なので、既知の名前だけ除外する。
CODEX_NON_TUI_SUBCOMMANDS = {
    "exec", "e", "review", "apply", "a", "cloud", "login", "logout",
    "mcp", "mcp-server", "app-server", "completion", "debug", "sandbox",
    "proto", "features", "help",
}


def pane_agent(pane):
    """pane のTTYで直接動いているClaude/Codexを特定する。"""
    tty = os.path.basename(pane.get("tty_name", ""))
    if not re.fullmatch(r"ttys?\w+", tty):
        return None
    try:
        result = subprocess.run(
            ["/bin/ps", "-t", tty, "-o", "pid=,ppid=,command="],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        try:
            argv = shlex.split(parts[2])
        except ValueError:
            continue
        if not argv:
            continue
        executable = os.path.basename(argv[0])
        if executable not in {"claude", "codex"}:
            continue
        if executable == "codex" and len(argv) > 1 and argv[1] in CODEX_NON_TUI_SUBCOMMANDS:
            continue
        explicit_id = next(
            (arg for arg in argv[1:] if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f-]{27,}", arg)),
            "",
        )
        if executable == "codex" and not explicit_id:
            try:
                opened = subprocess.run(
                    ["/usr/sbin/lsof", "-p", parts[0]], capture_output=True, text=True, timeout=5
                ).stdout
                # Codex本体はメイン会話に加えて、権限審査用guardianのログも
                # 開いている。lsofの先頭を採ると審査結果JSONだけの会話へ
                # 誤って紐づくため、ユーザー起点のログだけを候補にする。
                for session_id in re.findall(
                    r"rollout-[^\s/]+-([0-9a-f-]{36})\.jsonl", opened
                ):
                    path = find_log_by_id("codex", session_id)
                    head = codex_session_head(path) if path else {}
                    if head.get("thread_source") != "subagent" and not head.get("subagent"):
                        explicit_id = session_id
                        break
            except (OSError, subprocess.SubprocessError):
                pass
        return {
            "tool": executable, "pid": int(parts[0]), "command": parts[2],
            "explicit_id": explicit_id, "model": argv_model(argv),
        }
    return None


def pane_for_id(pane_id):
    return next((p for p in wezterm_panes() if str(p.get("pane_id")) == str(pane_id)), None)


def process_start_time(pid):
    """プロセスの起動時刻を epoch 秒で返す。取れなければ 0。"""
    try:
        out = subprocess.run(
            ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return time.mktime(time.strptime(out, "%a %b %d %H:%M:%S %Y"))
    except (OSError, subprocess.SubprocessError, ValueError, OverflowError):
        return 0.0


def wez_capture(pane_id):
    """WezTerm pane の画面をスクロールバック込みで返す。"""
    result = wezterm_cli("get-text", "--pane-id", str(pane_id), "--start-line", "-500")
    if result is None or result.returncode != 0:
        raise RuntimeError("WezTermの画面を取得できませんでした")
    return result.stdout


def wez_view_session(pane_id):
    """WezTerm pane を読み取り専用の擬似セッションとして解決する。

    claude は会話ログを開きっぱなしにしないため lsof では特定できない。
    argv の resume ID → プロセス起動時刻とログ作成時刻の突合 → 最新 mtime
    の順で対応する JSONL を推定する。
    """
    now = time.time()
    with WEZ_VIEW_LOCK:
        cached = WEZ_VIEW_CACHE.get(str(pane_id))
        if cached and cached[0] > now:
            return cached[1]
    pane = pane_for_id(pane_id)
    agent = pane_agent(pane) if pane else None
    info = None
    if pane and agent:
        cwd = urllib.parse.urlparse(pane.get("cwd", "")).path
        candidates = [
            item for item in resume_candidates(agent["tool"], cwd, agent["explicit_id"])
            if item["path"]
        ]
        entry = None
        if agent["explicit_id"]:
            entry = next((item for item in candidates if item["exact"]), None)
        if entry is None and candidates:
            started = process_start_time(agent["pid"])
            if started:
                nearest = min(candidates, key=lambda item: abs(item["created"] - started))
                if abs(nearest["created"] - started) <= 300:
                    entry = nearest
            if entry is None:
                entry = candidates[0]
        if entry:
            info = {
                "pane_id": str(pane.get("pane_id")), "tool": agent["tool"], "cwd": cwd,
                "log_path": entry["path"],
                "model": agent["model"] or entry.get("model", ""),
                "context": entry.get("context"),
                "label": entry.get("last_message") or entry.get("summary") or "",
            }
    with WEZ_VIEW_LOCK:
        WEZ_VIEW_CACHE[str(pane_id)] = (now + WEZ_VIEW_TTL, info)
        for old in list(WEZ_VIEW_CACHE)[: len(WEZ_VIEW_CACHE) - 40]:
            WEZ_VIEW_CACHE.pop(old, None)
    return info


def read_json_lines(path, limit=80):
    try:
        with open(path, encoding="utf-8") as source:
            for index, line in enumerate(source):
                if index >= limit:
                    break
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def user_message_raw(item, tool):
    """ユーザー発言を、ログに書かれたまま取り出す。"""
    text = ""
    if item.get("type") == "queue-operation":
        return ""  # 待ち行列の状態遷移。発言そのものは attachment に残る
    if item.get("type") == "attachment":
        # 処理中に送った入力は user ではなく queued_command として記録される。
        attachment = item.get("attachment") or {}
        if attachment.get("type") != "queued_command":
            return ""
        prompt = attachment.get("prompt")
        if isinstance(prompt, str):
            return prompt.strip()
        if not isinstance(prompt, list):
            return ""
        # 画像を添えて送ると [画像, テキスト] の配列になる。文面だけ拾う。
        return "\n".join(
            part.get("text", "").strip() for part in prompt
            if isinstance(part, dict) and part.get("type") == "text"
            and part.get("text", "").strip()
        )
    if tool == "claude" and item.get("type") == "user":
        content = (item.get("message") or {}).get("content", "")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = next((part.get("text", "") for part in content if part.get("type") == "text"), "")
    elif tool == "codex" and item.get("type") == "response_item":
        payload = item.get("payload", {})
        if payload.get("role") == "user":
            text = next(
                (part.get("text", "") for part in payload.get("content", []) if part.get("type") == "input_text"),
                "",
            )
    text = text.strip()
    if text and not text.startswith((
        "# AGENTS.md instructions", "<environment_context>",
        "The following is the Codex agent history", "<task-notification>",
        "<codex_internal_context",
    )):
        return text
    return ""


def bash_log_text(text, limit=4000):
    """TUI の bash モード（! 実行）のログを読みやすい Markdown にする。"""
    command = re.search(r"<bash-input>(.*?)</bash-input>", text, re.S)
    if command:
        return "```sh\n$ " + command.group(1).strip() + "\n```"
    blocks = [
        body.strip()
        for tag in ("stdout", "stderr")
        for body in re.findall(f"<bash-{tag}>(.*?)</bash-{tag}>", text, re.S)
        if body.strip()
    ]
    if not blocks:
        return ""
    output = "\n".join(blocks)
    if len(output) > limit:
        output = output[:limit] + "\n…（省略）"
    return "```\n" + output + "\n```"


def user_shell_log_text(text, limit=4000):
    """Claude Code の user_shell_command を読みやすい Markdown にする。"""
    command = re.search(r"<command>(.*?)</command>", text, re.S)
    result = re.search(r"<result>(.*?)</result>", text, re.S)
    parts = []
    if command and command.group(1).strip():
        parts.append("```sh\n$ " + command.group(1).strip() + "\n```")
    if result and result.group(1).strip():
        output = result.group(1).strip()
        if len(output) > limit:
            output = output[:limit] + "\n…（省略）"
        parts.append("```\n" + output + "\n```")
    return "\n\n".join(parts)


def slash_log_text(text, limit=2000):
    """スラッシュコマンドの実行ログ（/model など）を読める1行に畳む。"""
    name = re.search(r"<command-name>(.*?)</command-name>", text, re.S)
    if name:
        args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
        command = name.group(1).strip()
        return f"{command} {args.group(1).strip()}".strip() if args else command
    output = re.search(r"<local-command-stdout>(.*?)</local-command-stdout>", text, re.S)
    if output:
        # TUI の出力には色付けのエスケープが混ざる。
        plain = re.sub(r"\x1b\[[0-9;]*m", "", output.group(1)).strip()
        return plain[:limit] + "…（省略）" if len(plain) > limit else plain
    return ""  # caveat 等、読み手には意味のない指示文は落とす


def user_message_entry(item, tool):
    """ユーザー発言を {role, text} で返す。

    ! 実行はコマンドを発言側、その出力を結果側として扱う。表示のとき
    Markdown の構造（見出し・コードブロック等）が要るので改行は潰さない。
    """
    text = user_message_raw(item, tool)
    if not text:
        return None
    if text.startswith(("<command-name>", "<local-command-")):
        body = slash_log_text(text)
        if not body:
            return None
        # コマンド自体は発言側、その出力は結果側に置く。
        return {"role": "user" if text.startswith("<command-name>") else "assistant", "text": body}
    if text.startswith("<user_shell_command>"):
        body = user_shell_log_text(text)
        return {"role": "user", "text": body} if body else None
    if not text.startswith("<bash-"):
        return {"role": "user", "text": text}
    body = bash_log_text(text)
    if not body:
        return None
    return {"role": "user" if text.startswith("<bash-input>") else "assistant", "text": body}


def user_summary_text(item, tool):
    """一覧に出す用に、ユーザー発言を1行へ潰して返す。"""
    text = user_message_raw(item, tool)
    if not text:
        return ""
    if text.startswith("<bash-input>"):
        command = re.search(r"<bash-input>(.*?)</bash-input>", text, re.S)
        text = "$ " + command.group(1).strip() if command else ""
    elif text.startswith("<bash-"):
        return ""  # ! 実行の出力は発言ではない
    elif text.startswith("<command-name>"):
        text = slash_log_text(text)
    elif text.startswith("<local-command-"):
        return ""  # スラッシュコマンドの出力も発言ではない
    elif text.startswith("<user_shell_command>"):
        command = re.search(r"<command>(.*?)</command>", text, re.S)
        text = "$ " + command.group(1).strip() if command else ""
    # 添付画像のフルパスは一覧では邪魔なので目印に置き換える。
    text = UPLOAD_MENTION_RE.sub("📎画像", text)
    return " ".join(text.split())


def assistant_message_text(item, tool):
    content = []
    if tool == "claude" and item.get("type") == "assistant":
        content = (item.get("message") or {}).get("content", [])
        kinds = {"text"}
    elif tool == "codex" and item.get("type") == "response_item":
        payload = item.get("payload", {})
        if payload.get("role") != "assistant":
            return ""
        content = payload.get("content", [])
        kinds = {"output_text"}
    else:
        return ""
    if isinstance(content, str):
        return content.strip()
    return "\n".join(
        part.get("text", "").strip() for part in content
        if part.get("type") in kinds and part.get("text", "").strip()
    )


def codex_tool_images_text(item):
    """Codex のツール結果画像を永続化し、チャット表示用の行を返す。

    Browser の screenshot などは response_item ではなく event_msg の
    mcp_tool_call_end に base64 で記録される。ログの再パースごとに同じ画像を
    増やさないよう、画像内容のハッシュを保存名に使う。
    """
    payload = item.get("payload") or {}
    if item.get("type") == "event_msg" and payload.get("type") == "mcp_tool_call_end":
        result = payload.get("result") or {}
        content = ((result.get("Ok") or {}).get("content") or [])
    elif item.get("type") == "response_item" and payload.get("type") == "custom_tool_call_output":
        # functions.exec の image(...) / view_image はこの形式で保存される。
        content = payload.get("output") or []
    else:
        return ""
    lines = []
    image_types = (
        (b"\x89PNG\r\n\x1a\n", ".png"),
        (b"\xff\xd8\xff", ".jpg"),
        (b"GIF87a", ".gif"),
        (b"GIF89a", ".gif"),
        (b"RIFF", ".webp"),
    )
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "image":
            encoded = part.get("data")
        elif part.get("type") in {"input_image", "output_image"}:
            image_url = part.get("image_url") or ""
            match = re.fullmatch(r"data:image/[a-zA-Z0-9.+-]+;base64,(.+)", image_url, re.S)
            encoded = match.group(1) if match else ""
        else:
            continue
        if not isinstance(encoded, str) or not encoded or len(encoded) > 20 * 1024 * 1024:
            continue
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            continue
        extension = next(
            (suffix for signature, suffix in image_types if data.startswith(signature)),
            "",
        )
        if extension == ".webp" and data[8:12] != b"WEBP":
            extension = ""
        if not extension or len(data) > 15 * 1024 * 1024:
            continue
        digest = hashlib.sha256(data).hexdigest()[:16]
        session_dir = os.path.join(UPLOAD_DIR, "codex-images")
        path = os.path.join(session_dir, f"codex-{digest}{extension}")
        if not os.path.exists(path):
            try:
                os.makedirs(session_dir, mode=0o700, exist_ok=True)
                with open(path, "xb") as output:
                    output.write(data)
                os.chmod(path, 0o600)
            except FileExistsError:
                pass
            except OSError:
                continue
        lines.append(f"添付画像: {path}")
    return "\n".join(lines)


def tool_use_label(name, payload):
    """ツール実行を1行で表す。何をしているかが分かる引数を選ぶ。"""
    payload = payload if isinstance(payload, dict) else {}
    if name == "Bash":
        command = (payload.get("command") or "").strip().splitlines()
        return payload.get("description") or (command[0][:90] if command else "Bash")
    if name in {"Read", "Edit", "Write", "NotebookEdit"}:
        return f"{name}: {os.path.basename(payload.get('file_path') or '')}".strip(": ")
    if name in {"Grep", "Glob"}:
        return f"{name}: {payload.get('pattern') or ''}".strip(": ")
    if name in {"Task", "Agent"}:
        return f"Agent: {payload.get('description') or ''}".strip(": ")
    if name == "Skill":
        return f"Skill: {payload.get('skill') or ''}".strip(": ")
    if name in {"WebFetch", "WebSearch"}:
        return f"{name}: {payload.get('url') or payload.get('query') or ''}".strip(": ")
    if name.startswith("mcp__"):
        return name.split("__")[-1]
    return name


IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp")


def sent_files_text(payload):
    """SendUserFile で送られたファイルを uploads へ退避し、チャット表示用の行を返す。

    送信元は一時ディレクトリのことが多く後で消えるため、初回パース時に
    uploads へコピーして Web UI から配信できるようにする。コピー先は
    送信元パスのハッシュで決まるので、ポーリングのたびの再パースでも冪等。
    """
    payload = payload if isinstance(payload, dict) else {}
    lines = []
    for source in payload.get("files") or []:
        if not isinstance(source, str):
            continue
        digest = hashlib.sha1(source.encode()).hexdigest()[:8]
        basename = re.sub(r"\s+", "_", os.path.basename(source))
        dest = os.path.join(UPLOAD_DIR, f"sent-{digest}-{basename}")
        if not os.path.exists(dest):
            try:
                shutil.copyfile(source, dest)
            except OSError:
                lines.append(f"📎 {basename}（元ファイルは削除済みで表示できません）")
                continue
        prefix = "添付画像" if dest.lower().endswith(IMAGE_EXTENSIONS) else "添付ファイル"
        lines.append(f"{prefix}: {dest}")
    caption = (payload.get("caption") or "").strip()
    if caption and lines:
        lines.append(caption)
    return "\n".join(lines)


def assistant_parts(item, tool):
    """アシスタントの発言とツール使用を、ログの並び順で返す。"""
    if tool == "codex":
        image_text = codex_tool_images_text(item)
        if image_text:
            return [{"role": "assistant", "text": image_text}]
    if tool != "claude" or item.get("type") != "assistant":
        text = assistant_message_text(item, tool)
        return [{"role": "assistant", "text": text}] if text else []
    content = (item.get("message") or {}).get("content", [])
    if isinstance(content, str):
        return [{"role": "assistant", "text": content.strip()}] if content.strip() else []
    parts = []
    for part in content or []:
        kind = part.get("type")
        if kind == "text" and part.get("text", "").strip():
            parts.append({"role": "assistant", "text": part["text"].strip()})
        elif kind == "tool_use":
            if part.get("name") == "SendUserFile":
                text = sent_files_text(part.get("input"))
                if text:
                    parts.append({"role": "assistant", "text": text})
                continue
            label = tool_use_label(part.get("name", ""), part.get("input"))
            if label:
                parts.append({"role": "tool", "text": label})
    return parts


def session_messages(path, tool, limit=300):
    """会話ログの末尾から limit 件を返す。

    ログは数百MBに達することがあるため全行は読まず、末尾から必要な分だけ
    さかのぼってパースする。
    """
    messages = []
    for item in read_json_lines_reverse(path, max_lines=20000, max_bytes=64 * 1024 * 1024):
        entry = user_message_entry(item, tool)
        if entry:
            messages.append(entry)
        else:
            # 逆順に読んでいるので、1エントリ内のパーツも逆順に積む。
            # ツール実行は履歴に残さず、実行中のものだけ session_activity で見せる。
            messages.extend(
                part for part in reversed(assistant_parts(item, tool))
                if part["role"] != "tool"
            )
        if len(messages) >= limit:
            break
    messages.reverse()
    return messages


def queued_inputs(path, limit=400):
    """まだ Claude に読まれていない入力を、送った順に返す。

    処理中に送った入力は enqueue として積まれ、降りるときに remove（実行中の
    ターンへ割り込み）か dequeue（次のターンの入力になる）が記録される。降りた
    発言は attachment や user として別に残るので、ここでは待機分だけを見る。
    """
    events = [
        item for item in read_json_lines_reverse(path, max_lines=limit)
        if item.get("type") == "queue-operation"
    ]
    waiting = []
    for item in reversed(events):
        operation = item.get("operation")
        content = (item.get("content") or "").strip()
        if operation == "enqueue" and content:
            waiting.append(content)
        elif operation == "remove" and content in waiting:
            waiting.remove(content)
        elif operation == "dequeue" and waiting:
            waiting.pop(0)
    return waiting


def screen_is_running(screen, tool=None):
    """画面から、TUI がまだ処理中かを見る。

    処理中は Running… / Hatching… のようなスピナー行が出る（語は毎回変わる）。
    esc to interrupt を出す版もあるので、そちらも拾う。
    Codex は過去のスピナーをスクロール履歴に残すため、最後の回答完了を示す
    水平区切り線より後に現在のスピナーがある場合だけ実行中とする。
    """
    if tool == "codex":
        lines = screen.splitlines()
        boundary = max(
            (index for index, line in enumerate(lines)
             if re.fullmatch(r"\s*─{10,}\s*", line)),
            default=-1,
        )
        return any(
            re.search(r"^\s*•\s+.*\besc to interrupt\b", line, re.I)
            for line in lines[boundary + 1:]
        )
    if "esc to interrupt" in screen.lower():
        return True
    return any(re.fullmatch(r"\s*[A-Za-z]+ing…\s*", line) for line in screen.splitlines())


def screen_background_label(screen):
    """アイドル中のバックグラウンドタスク（Monitor等）を画面下部から検出する。

    Monitor 稼働中は入力欄の下のヒント行に「auto mode on · … · 1 monitor」の
    ような表示が常駐する。転写本文にも monitor という語は現れうるので、
    下数行だけを見る。検出できたらステータスラベルを、なければ空文字を返す。
    """
    lines = screen.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    match = re.search(
        r"\b\d+\s+(monitors?|background tasks?|background terminals?|local agents?)"
        r"(?:\s+running)?\b",
        "\n".join(lines[-8:]),
    )
    if not match:
        return ""
    noun = match.group(1)
    if noun.startswith("monitor"):
        return "監視中"
    if noun.startswith("background"):
        return "タスク実行中"
    return "エージェント実行中"


def session_running(name):
    """TUI がまだ処理中かを返す。"""
    try:
        screen = tmux_run("capture-pane", "-p", "-J", "-S", "-24", "-t", name)
    except Exception:
        return False
    return screen.returncode == 0 and screen_is_running(screen.stdout)


def dialog_is_foreground(lines, prompt_index):
    """プロンプト行が画面最下部にあるか（＝実際に入力待ちのダイアログか）。

    会話に引用された「Enter selection [1-N]」等のテキストは、下に本文や
    入力欄・フッターが続くので、これで本物と区別できる。本物のダイアログの
    下は空行か「Enter to confirm · Esc to cancel」のキー案内だけ。
    """
    for line in lines[prompt_index + 1:]:
        text = line.strip()
        if text and "Enter to confirm" not in text and "Esc to cancel" not in text:
            return False
    return True


def parse_confirm_screen(lines):
    """複数質問の AskUserQuestion 最後にある y/n 確認画面を拾う。

    回答レビュー画面はこの形で、数字でなく y/n キーを待っている:

        Ready to submit your answers?
        y. Submit answers
        n. Cancel
        Enter y/n:
    """
    prompt_index = next(
        (i for i in range(len(lines) - 1, -1, -1)
         if lines[i].strip().startswith("Enter y/n")),
        None,
    )
    if prompt_index is None or not dialog_is_foreground(lines, prompt_index):
        return None
    choices = []
    for row in range(max(0, prompt_index - 6), prompt_index):
        match = re.match(r"^([yn])\.\s+(.+)$", lines[row].strip())
        if match:
            choices.append({
                "number": match.group(1), "label": match.group(2).strip(),
                "description": "",
            })
    if len(choices) != 2:
        return None
    question = next(
        (
            lines[row].strip()
            for row in range(prompt_index - 1, -1, -1)
            if lines[row].strip().endswith("?")
            and not re.match(r"^[yn]\.", lines[row].strip())
        ),
        "Ready to submit your answers?",
    )
    return {"question": question, "choices": choices}


def parse_question_screen(screen):
    """選択プロンプト画面から {question, choices} を組み立てる。失敗時は None。

    AskUserQuestion の tool_use は回答されるまでログに書かれないため、
    選択肢は画面から拾うしかない。画面はこの形をしている:

        <質問文（折り返しあり）>
        1. ラベル — 説明（折り返しあり）
        ...
        N. Chat about this
        Enter selection [1-N], or Escape to cancel:
    """
    lines = screen.splitlines()
    # 引用されたダイアログ風テキストが画面上部に残ることがあるため、
    # 下から探して最下部にあるものだけを本物として扱う。
    prompt_index = next(
        (i for i in range(len(lines) - 1, -1, -1) if "Enter selection [" in lines[i]),
        None,
    )
    if prompt_index is None:
        return parse_confirm_screen(lines)
    if not dialog_is_foreground(lines, prompt_index):
        return parse_confirm_screen(lines)
    match = re.search(r"Enter selection \[1-(\d)\]", lines[prompt_index])
    if not match:
        return None
    count = int(match.group(1))
    # 下から上へ、N. → 1. の順に選択肢を拾う。番号行に挟まれた行は折り返し。
    choices, wrapped = [], []
    expected = count
    row = prompt_index - 1
    while row >= 0 and expected >= 1:
        line = lines[row].strip()
        numbered = re.match(rf"^{expected}\.\s+(.*)$", line)
        if numbered:
            text = " ".join([numbered.group(1).strip()] + wrapped).strip()
            label, _, description = text.partition(" — ")
            choices.append({
                "number": expected, "label": label.strip(),
                "description": description.strip(),
            })
            wrapped = []
            expected -= 1
        elif line:
            wrapped.insert(0, line)
        else:
            wrapped = []
        row -= 1
    if expected != 0:
        return None
    choices.reverse()
    # 選択肢の直上の連続した非空行が質問文。空行を挟まない画面では上の
    # 出力まで際限なく巻き込むので数行に留め、質問マーカー（☐）があれば
    # そこから後ろだけを使う。
    question_lines = []
    while row >= 0 and not lines[row].strip():
        row -= 1
    while row >= 0 and lines[row].strip() and len(question_lines) < 4:
        line = lines[row].strip()
        # 起動時ダイアログ（MCP承認等）の画面先頭に出るモード表示は質問文でない
        if not line.startswith("[Screen Reader Mode"):
            question_lines.insert(0, line)
        row -= 1
    question = " ".join(question_lines)
    if "☐" in question:
        question = question.rsplit("☐", 1)[1].strip()
    return {"question": question, "choices": choices}


def parse_codex_question_screen(screen):
    """Codex TUIの選択画面を {question, choices} に変換する。

    Browser権限確認などは次の形式で表示される。説明は次行へ折り返すことがある。

        Field 1/1
        Allow Browser use to use full CDP access on http://localhost:3005

        › 1. Allow         Run the tool and continue.
          2. Always allow  Run the tool and remember this choice for future tool
                           calls.
          3. Cancel        Cancel this tool call
        enter to submit | esc to cancel
    """
    lines = screen.splitlines()
    prompt_index = next(
        (i for i in range(len(lines) - 1, -1, -1)
         if "enter to submit" in lines[i].lower()),
        None,
    )
    if prompt_index is None:
        return None

    field_index = next(
        (i for i in range(prompt_index - 1, -1, -1)
         if re.fullmatch(r"Field\s+\d+/\d+", lines[i].strip(), re.I)),
        None,
    )
    scan_start = field_index + 1 if field_index is not None else max(0, prompt_index - 30)
    choices = []
    current = None
    first_choice = None
    for row in range(scan_start, prompt_index):
        line = lines[row].strip()
        match = re.match(r"^[›>▸]?\s*(\d+)\.\s+(.+)$", line)
        if match:
            remainder = match.group(2).strip()
            fields = re.split(r"\s{2,}", remainder, maxsplit=1)
            current = {
                "number": int(match.group(1)),
                "label": fields[0].strip(),
                "description": fields[1].strip() if len(fields) > 1 else "",
            }
            choices.append(current)
            if first_choice is None:
                first_choice = row
        elif current and line:
            current["description"] = " ".join(
                part for part in (current["description"], line) if part
            )
    if not choices or first_choice is None:
        return None

    question_lines = [line.strip() for line in lines[scan_start:first_choice] if line.strip()]
    if not question_lines:
        return None
    return {"question": " ".join(question_lines), "choices": choices}


def pending_question(name, tool):
    """選択待ちなら {question, choices} を返す。それ以外は None。

    AskUserQuestion で止まった TUI は数字入力を待っており、通常のテキスト
    送信には反応しない。ログには回答後まで痕跡が残らないため、画面で判定する。
    """
    if tool not in {"claude", "codex"}:
        return None
    try:
        screen = tmux_run("capture-pane", "-p", "-J", "-S", "-40", "-t", name)
    except Exception:
        return None
    if screen.returncode != 0:
        return None
    # 実行中のセッションに本物の選択ダイアログは出ない。会話に引用された
    # 「Enter selection [1-N]」等が画面に残っているだけの誤検出を避ける。
    if screen_is_running(screen.stdout, tool):
        return None
    if tool == "codex":
        return parse_codex_question_screen(screen.stdout)
    return parse_question_screen(screen.stdout)


def parse_shell_auth_screen(screen):
    """実行中の GitHub CLI デバイス認証案内を Markdown へ変換する。"""
    matches = list(re.finditer(
        r"First copy your one-time code:\s*([A-Z0-9-]+).*?"
        r"Open this URL to continue in your web browser:\s*"
        r"(https://github\.com/login/device)",
        screen,
        re.S,
    ))
    if not matches:
        return ""
    match = matches[-1]
    if "Authentication complete" in screen[match.end():]:
        return ""
    code, url = match.groups()
    return (
        "**GitHub認証待ちです**\n\n"
        f"ワンタイムコード: `{code}`\n\n"
        f"[GitHubの認証ページを開く]({url})"
    )


def pending_shell_auth(name, tool):
    """tmux画面にだけ出ている実行中の認証案内を返す。"""
    if tool not in {"claude", "codex"}:
        return ""
    try:
        screen = tmux_run("capture-pane", "-p", "-J", "-S", "-40", "-t", name)
    except Exception:
        return ""
    return parse_shell_auth_screen(screen.stdout) if screen.returncode == 0 else ""


def log_activity(path, tool):
    """会話ログから、いま動いているツールなどの短いラベルを返す。"""
    for item in read_json_lines_reverse(path, max_lines=40):
        if user_message_entry(item, tool):
            return "考え中"
        parts = assistant_parts(item, tool)
        if parts:
            return parts[-1]["text"] if parts[-1]["role"] == "tool" else "考え中"
    return "考え中"


def session_activity(name, path, tool):
    """実行中なら、いま動いているツールなどを短く返す。空文字ならアイドル。"""
    if not session_running(name):
        return ""
    return log_activity(path, tool)


def wait_classifier_context(path, tool):
    """分類用に、直近の会話（依頼主の指示を含む数往復）を返す。

    最後の発言だけだと「待機していて」等の依頼主の指示が落ちて、
    スケジュール待ちのセッションを要対応と誤判定する。
    最後の発言がユーザー側なら分類対象がないので空を返す。
    """
    messages = session_messages(path, tool, limit=8)
    if not messages or messages[-1]["role"] != "assistant":
        return ""
    labels = {"user": "依頼主", "assistant": "アシスタント"}
    lines = []
    for index, item in enumerate(messages):
        # 判定の主対象である最後の発言は厚めに、それ以前は文脈用に短く残す
        budget = 3000 if index == len(messages) - 1 else 500
        lines.append(f"── {labels[item['role']]} ──\n{item['text'][-budget:]}")
    return "\n".join(lines)


def run_wait_classifier(path, tool, key):
    """haiku で最終発言を分類してキャッシュに入れる（バックグラウンド実行）。"""
    label = ""
    try:
        text = wait_classifier_context(path, tool)
        if text:
            prompt = (
                "以下はAIコーディングセッションの直近のやり取りで、最後はアシスタント発言。"
                "セッションは停止しており、次の入力を待っている。"
                "最後の発言がいま誰のアクション待ちかを次の3つから1語だけで答えよ。\n"
                "- 要対応: 作業を進めるために依頼主の回答（質問・確認・選択・判断）が必要。"
                "または、アシスタントが次に行う作業を宣言したまま、その結果を報告せず停止している\n"
                "- 他者待ち: 第三者のレビュー・返信・CI・指定時刻やスケジュール起動など、"
                "依頼主以外の外部イベントを待っている\n"
                "- 完了: 作業完了・結果の報告のみで、追加の入力を明示的に求めていない\n"
                "依頼主の直前の指示（待機依頼・時刻指定など）を踏まえて判定すること。"
                "外部イベント待ちの本筋に添えた「必要なら教えて」程度の補足は要対応にしない。\n"
                "出力は「要対応」「他者待ち」「完了」のいずれか1語のみ。\n"
                "---\n" + text
            )
            # cwd をホーム外にして、-p のログが「最近の会話を再開」に混ざらないようにする
            result = subprocess.run(
                [CLAUDE_BIN, "-p", "--model", WAIT_CLASS_MODEL, prompt],
                capture_output=True, text=True, timeout=90, cwd="/tmp",
            )
            answer = result.stdout.strip() if result.returncode == 0 else ""
            label = next(
                (name for name in ("要対応", "他者待ち", "完了") if name in answer), ""
            )
    except (OSError, subprocess.SubprocessError):
        pass
    with WAIT_CLASS_LOCK:
        WAIT_CLASS_CACHE[path] = {
            "key": key, "label": label,
            "retry": 0 if label else time.time() + WAIT_CLASS_RETRY,
        }
        for old in list(WAIT_CLASS_CACHE)[: len(WAIT_CLASS_CACHE) - LOG_META_LIMIT]:
            WAIT_CLASS_CACHE.pop(old, None)
        WAIT_CLASS_PENDING.discard(path)


def classify_wait(path, tool):
    """返事待ちセッションのラベルを返す。未分類の間は空文字。

    分類には数秒かかるため、この関数は待たずに裏へ投げて即返す。
    結果は次回以降のポーリングで反映される。
    """
    if not path:
        return ""
    try:
        stat = os.stat(path)
    except OSError:
        return ""
    key = (stat.st_mtime, stat.st_size, tool)
    with WAIT_CLASS_LOCK:
        cached = WAIT_CLASS_CACHE.get(path)
        if cached and cached["key"] == key:
            if cached["label"] or not cached["retry"] or time.time() < cached["retry"]:
                return cached["label"]
        if path in WAIT_CLASS_PENDING:
            return cached["label"] if cached else ""
        WAIT_CLASS_PENDING.add(path)
    threading.Thread(
        target=run_wait_classifier, args=(path, tool, key), daemon=True
    ).start()
    return cached["label"] if cached else ""


def sidebar_status(item):
    """サイドバーに出す状態ラベルと css クラスを返す。"""
    if item.get("running"):
        text = "考え中"
        if item.get("log_path"):
            text = session_activity(item["name"], item["log_path"], item["tool"]) or text
        if len(text) > 24:
            text = text[:24] + "…"
        return text, "run"
    if pending_question(item["name"], item["tool"]):
        return "選択待ち", "ask"
    if item.get("background"):
        return item["background"], "watch"
    label = classify_wait(item.get("log_path"), item["tool"])
    if label == "要対応":
        return "要対応", "need"
    if label == "他者待ち":
        return "他者待ち", "blocked"
    if label == "完了":
        return "完了", "done"
    return "返事待ち", "wait"


def session_transcript(path, tool):
    labels = {"user": "あなた", "assistant": tool, "tool": "ツール"}
    return "\n\n".join(
        f'── {labels[item["role"]]} ──\n{item["text"]}'
        for item in session_messages(path, tool)
    )


def session_summary(path, tool):
    for item in read_json_lines(path, 120):
        text = user_summary_text(item, tool)
        if text:
            return text[:70]
    return ""


def read_json_lines_reverse(path, max_lines=500, max_bytes=8 * 1024 * 1024):
    try:
        with open(path, "rb") as source:
            source.seek(0, os.SEEK_END)
            position = source.tell()
            # チャンクを繋ぎ直すのは最後の1回だけにする。毎回連結すると
            # 読む量が増えたときに O(n^2) になる。
            chunks = []
            newlines = 0
            total = 0
            while position > 0 and newlines <= max_lines and total < max_bytes:
                size = min(65536, position)
                position -= size
                source.seek(position)
                chunk = source.read(size)
                chunks.append(chunk)
                newlines += chunk.count(b"\n")
                total += size
            data = b"".join(reversed(chunks))
        for line in reversed(data.splitlines()[-max_lines:]):
            try:
                yield json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
    except OSError:
        return


def session_last_message(path, tool):
    # 直近のユーザー発言はたいてい末尾数十行に見つかる。まず浅く読み、
    # 見つからないときだけ深追いする。
    for max_lines in (60, 500):
        for item in read_json_lines_reverse(path, max_lines=max_lines):
            text = user_summary_text(item, tool)
            if text:
                return text[:140]
    return ""


def parse_timestamp(value):
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0.0


def session_model(path, tool):
    """ログ末尾から実際に動いているモデルを (名前, 記録時刻) で返す。"""
    for max_lines in (80, 600):
        for item in read_json_lines_reverse(path, max_lines=max_lines):
            if tool == "codex":
                if item.get("type") == "turn_context":
                    model = item.get("payload", {}).get("model", "")
                    if model:
                        return model, parse_timestamp(item.get("timestamp", ""))
                continue
            if item.get("type") != "assistant" or item.get("isSidechain"):
                continue
            # エラー時の合成レスポンスは model が "<synthetic>" になる。
            model = (item.get("message") or {}).get("model", "")
            if model and not model.startswith("<"):
                return model, parse_timestamp(item.get("timestamp", ""))
    return "", 0.0


def session_context(path, tool):
    """ログ末尾から現在のコンテキスト使用率(%)を返す。不明なら None。

    claude は直近 assistant レコードの usage 合計（入力 + キャッシュ + 出力）が
    そのターン終了時点のコンテキスト量。codex は token_count イベントに
    直近リクエストの合計とウィンドウサイズがそのまま入っている。
    """
    for max_lines in (80, 600):
        for item in read_json_lines_reverse(path, max_lines=max_lines):
            if tool == "codex":
                if item.get("type") != "event_msg":
                    continue
                payload = item.get("payload") or {}
                if payload.get("type") != "token_count":
                    continue
                info = payload.get("info") or {}
                window = info.get("model_context_window") or 0
                used = (info.get("last_token_usage") or {}).get("total_tokens", 0)
                if window and used:
                    return min(100, round(used * 100 / window))
                continue
            if item.get("type") != "assistant" or item.get("isSidechain"):
                continue
            message = item.get("message") or {}
            usage = message.get("usage") or {}
            used = sum(usage.get(key) or 0 for key in (
                "input_tokens", "cache_read_input_tokens",
                "cache_creation_input_tokens", "output_tokens",
            ))
            # エラー時の合成レスポンス（model が "<synthetic>"）は usage が空
            if not used or message.get("model", "").startswith("<"):
                continue
            window = claude_context_window(message.get("model", ""))
            return min(100, round(used * 100 / window))
    return None


def claude_context_window(model):
    """モデル名からコンテキストウィンドウを推定する。

    Claude 5 ファミリー（fable/mythos/opus-5/sonnet-5）と Opus/Sonnet 4.6 以降は
    1M トークン。Haiku と旧世代（4.5 以前）は 200k。
    """
    if "[1m]" in model:
        return 1_000_000
    if re.search(r"claude-(fable|mythos|opus|sonnet)-\d+$", model):
        return 1_000_000
    match = re.search(r"claude-(?:opus|sonnet)-(\d+)-(\d+)", model)
    if match and (int(match.group(1)), int(match.group(2))) >= (4, 6):
        return 1_000_000
    return 200_000


def short_path(path):
    """ヘッダー表示用に ~/…/親/カレント へ縮める。末尾が切れると何のプロジェクトか分からなくなる。"""
    display = f"~{path[len(HOME):]}" if path.startswith(HOME) else path
    parts = display.split("/")
    if len(parts) > 3:
        display = "/".join([parts[0], "…", *parts[-2:]])
    return display


def switchable_models(tool):
    """起動後に切り替えられるモデル。Codex の /model は引数を取らないので対象外。"""
    if tool != "claude":
        return []
    return [value for value, _ in MODELS_BY_TOOL["claude"] if value != "default"]


def model_label(model, tool):
    """表示用にモデル名を短くする（claude-opus-5 → opus-5）。"""
    if tool == "claude":
        model = model.removeprefix("claude-")
        model = re.sub(r"-\d{8}(?=\[|$)", "", model)
    return model


def context_class(pct):
    """使用率に応じた警告色のクラス。70%からオレンジ、90%から赤。"""
    if pct >= 90:
        return " ctx-high"
    if pct >= 70:
        return " ctx-warn"
    return ""


def context_chip(pct):
    """サイドバー用のコンテキスト使用率チップ。不明なら出さない。"""
    if pct is None:
        return ""
    return f'<span class="ctx{context_class(pct)}">{pct}%</span>'


def context_badge_html(pct):
    """ヘッダー用のバッジ。不明でも要素は出しておき、ポーリングJSの更新先にする。"""
    if pct is None:
        return '<span class="ctx" id="ctx" hidden></span>'
    return (
        f'<span class="ctx{context_class(pct)}" id="ctx" '
        f'title="コンテキスト使用率">{pct}%</span>'
    )


def log_meta(path, tool):
    """ログの要約と最終メッセージを (mtime, size) キーでメモ化して返す。"""
    try:
        stat = os.stat(path)
    except OSError:
        return {"summary": "", "last_message": "", "model": "", "model_at": 0.0,
                "context": None}
    key = (stat.st_mtime, stat.st_size, tool)
    with LOG_META_LOCK:
        cached = LOG_META_CACHE.get(path)
        if cached and cached["key"] == key:
            return cached
    model, model_at = session_model(path, tool)
    meta = {
        "key": key,
        "summary": session_summary(path, tool),
        "last_message": session_last_message(path, tool),
        "model": model,
        "model_at": model_at,
        "context": session_context(path, tool),
    }
    with LOG_META_LOCK:
        LOG_META_CACHE[path] = meta
        for old in list(LOG_META_CACHE)[: len(LOG_META_CACHE) - LOG_META_LIMIT]:
            LOG_META_CACHE.pop(old, None)
    return meta


ARTIFACT_CACHE_LOCK = threading.Lock()
ARTIFACT_CACHE = {}  # log_path -> 増分パース状態（読み取り済みオフセットと抽出結果）
GH_CREATE_MARKERS = (b"gh pr create", b"gh issue create")
# コマンド位置の gh pr/issue create のみ対象にする。grep の検索文字列や
# ドキュメント内の言及（引用符の中など）を「実行した」と誤認しないため。
# `SKIP_REVIEW_GATE=1 gh pr create` のようなコマンド単位の環境変数指定も許可する。
GH_CREATE_RE = re.compile(
    r"(?m)(?:^|[;&|(]|\$\()\s*"
    r"(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*"
    r"gh\s+(pr|issue)\s+create\b"
)
GH_URL_RE = re.compile(r"https://github\.com/[\w.-]+/([\w.-]+)/(pull|issues)/(\d+)")

# PR・issueの状態（open/merged/closed）。チップの色分けに使う。
# gh の呼び出しは数百msかかるため裏スレッドで取得し、結果をURL単位でキャッシュする
GH_BIN = find_bin("gh", CONFIG.get("gh_bin"))
ARTIFACT_STATE_LOCK = threading.Lock()
ARTIFACT_STATE_CACHE = {}  # url -> {"state": "open|merged|closed|''", "checked": ts}
ARTIFACT_STATE_PENDING = set()
ARTIFACT_STATE_TTL = 300  # merged以外は状態が変わりうるので定期的に取り直す


def _fetch_artifact_state(url, number):
    """gh api で PR/issue の状態を取ってキャッシュへ書く（裏スレッド用）。"""
    label = ""
    owner_repo = "/".join(urllib.parse.urlparse(url).path.split("/")[1:3])
    try:
        # PRも issues エンドポイントで引ける。PRのマージ判定は
        # pull_request.merged_at（issueには無いキーなので空になる）
        result = subprocess.run(
            [GH_BIN, "api", f"repos/{owner_repo}/issues/{number}",
             "--jq", '.state + " " + (.pull_request.merged_at // "")'],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            state_word, _, merged_at = result.stdout.strip().partition(" ")
            if merged_at:
                label = "merged"
            elif state_word in ("open", "closed"):
                label = state_word
    except (OSError, subprocess.SubprocessError):
        pass
    with ARTIFACT_STATE_LOCK:
        ARTIFACT_STATE_CACHE[url] = {"state": label, "checked": time.time()}
        ARTIFACT_STATE_PENDING.discard(url)


def artifact_state(url, number):
    """PR/issueの現在の状態を返す。取得できるまでは空文字（チップは種別色のまま）。

    mergedは終端状態なので再取得しない。open/closedと取得失敗はTTL経過後に取り直す。
    """
    with ARTIFACT_STATE_LOCK:
        cached = ARTIFACT_STATE_CACHE.get(url)
        if cached and (
            cached["state"] == "merged"
            or time.time() - cached["checked"] < ARTIFACT_STATE_TTL
        ):
            return cached["state"]
        if url in ARTIFACT_STATE_PENDING:
            return cached["state"] if cached else ""
        ARTIFACT_STATE_PENDING.add(url)
    threading.Thread(
        target=_fetch_artifact_state, args=(url, number), daemon=True
    ).start()
    return cached["state"] if cached else ""


def _artifact_add(state, match, kinds):
    kind = "pr" if match.group(2) == "pull" else "issue"
    if kind not in kinds:
        return False
    url = match.group(0)
    state["items"][url] = {
        "kind": kind, "repo": match.group(1), "number": int(match.group(3)), "url": url,
    }
    return True


def _tool_result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return ""


def _standalone_urls(text):
    """単独行になっているGitHub URLだけ返す。

    gh pr/issue create は作成したURLを単独行で出力する。行の一部に現れるURLは
    PR本文の関連リンクやスクリプト出力の混入なので採用しない。
    """
    for line in text.splitlines():
        match = GH_URL_RE.fullmatch(line.strip())
        if match:
            yield match


def _artifact_scan_claude(state, line):
    """claudeログ1行を見て gh pr/issue create とその結果URLをペアで拾う。"""
    interesting = any(marker in line for marker in GH_CREATE_MARKERS)
    if not interesting and not any(tid.encode() in line for tid in state["pending"]):
        return
    try:
        record = json.loads(line)
    except ValueError:
        return
    content = (record.get("message") or {}).get("content")
    if not isinstance(content, list):
        return
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "tool_use" and part.get("name") == "Bash":
            command = (part.get("input") or {}).get("command") or ""
            kinds = GH_CREATE_RE.findall(command)
            if kinds and part.get("id"):
                state["pending"][part["id"]] = kinds
        elif part.get("type") == "tool_result":
            kinds = state["pending"].pop(part.get("tool_use_id"), None)
            if not kinds:
                continue
            for match in _standalone_urls(_tool_result_text(part.get("content"))):
                _artifact_add(state, match, kinds)


def _codex_command(arguments):
    """function_call の arguments(JSON文字列) から実行コマンド文字列を取り出す。"""
    try:
        parsed = json.loads(arguments)
    except ValueError:
        return ""
    command = parsed.get("cmd") or parsed.get("command") or ""
    if isinstance(command, list):
        command = " ".join(str(part) for part in command)
    return command if isinstance(command, str) else ""


def _codex_output_texts(value, skip_keys=("command", "arguments", "cmd")):
    """payload内の出力系文字列を集める。コマンドのエコーはURL誤検出源なので除く。"""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if key not in skip_keys:
                yield from _codex_output_texts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _codex_output_texts(item)


def _artifact_scan_codex(state, line):
    """codexログ1行を見て gh pr/issue create の実行と後続出力のURLを拾う。

    codexの exec_command は非同期でコマンドの結果が同じレコードに載らないため、
    call_id でのペアリングは使わず「create 実行後、最初に出力へ現れた同種URL」を採用する。
    """
    has_marker = any(marker in line for marker in GH_CREATE_MARKERS)
    has_url = state["kinds"] and (b"/pull/" in line or b"/issues/" in line)
    if not has_marker and not has_url:
        return
    try:
        record = json.loads(line)
    except ValueError:
        return
    payload = record.get("payload") or {}
    kind_type = payload.get("type")
    if kind_type == "function_call":
        command = _codex_command(payload.get("arguments") or "")
        state["kinds"] += GH_CREATE_RE.findall(command)
    elif kind_type in ("function_call_output", "exec_command_end") and state["kinds"]:
        for text in _codex_output_texts(payload):
            for match in _standalone_urls(text):
                if _artifact_add(state, match, state["kinds"]):
                    state["kinds"].remove("pr" if match.group(2) == "pull" else "issue")


def session_artifacts(path, tool):
    """セッション中に gh pr/issue create で作成した PR・issue の一覧を返す。

    JSONLは追記専用なので、読み取り済みオフセットを覚えて追記分だけパースする。
    初回のみ全量スキャンになる（大きなログで数秒かかることがある）。
    """
    if not path:
        return []
    with ARTIFACT_CACHE_LOCK:
        state = ARTIFACT_CACHE.get(path)
        if state is None:
            state = {"lock": threading.Lock(), "pos": 0, "pending": {}, "kinds": [], "items": {}}
            ARTIFACT_CACHE[path] = state
    scan = _artifact_scan_codex if tool == "codex" else _artifact_scan_claude
    with state["lock"]:
        try:
            size = os.path.getsize(path)
            if size < state["pos"]:
                # truncate等でログが縮んだら最初から読み直す
                state.update({"pos": 0, "pending": {}, "kinds": [], "items": {}})
                size = os.path.getsize(path)
            if size > state["pos"]:
                with open(path, "rb") as fh:
                    fh.seek(state["pos"])
                    for line in fh:
                        if not line.endswith(b"\n"):
                            break  # 書き込み途中の行は次回読む
                        state["pos"] += len(line)
                        scan(state, line)
        except OSError:
            pass
        return [
            dict(item, state=artifact_state(item["url"], item["number"]))
            for item in state["items"].values()
        ]


def artifact_chips(items):
    """サイドバー用のPR/issueチップ（<a>内に入るのでリンクにしない）。

    長寿命セッションでカードが溢れないよう最新5件に絞り、残りは +N で示す。
    """
    if not items:
        return ""
    shown = items[-5:]
    extra = len(items) - len(shown)
    chips = f'<span class="art art-more">+{extra}</span>' if extra else ""
    chips += "".join(
        f'<span class="{_art_class(item)}">{html.escape(item["repo"])}#{item["number"]}</span>'
        for item in shown
    )
    return f'<span class="arts">{chips}</span>'


def _art_class(item):
    """チップのcssクラス。状態が取れていれば状態色が種別色を上書きする。"""
    state = item.get("state") or ""
    return f'art art-{item["kind"]}' + (f" art-{state}" if state else "")


def artifact_links(items):
    """ターミナルヘッダー下に出すPR/issueリンク。"""
    return "".join(
        f'<a class="{_art_class(item)}" target="_blank" rel="noopener" '
        f'href="{html.escape(item["url"])}">{html.escape(item["repo"])}#{item["number"]}</a>'
        for item in items
    )


def codex_session_head(path):
    """codexログ先頭の session_meta から会話識別情報を返す。"""
    with CODEX_HEAD_LOCK:
        cached = CODEX_HEAD_CACHE.get(path)
    if cached is not None:
        return cached
    first = next(read_json_lines(path, 1), {})
    payload = first.get("payload", {}) if first.get("type") == "session_meta" else {}
    source = payload.get("source")
    head = {
        "id": payload.get("id", ""),
        "cwd": payload.get("cwd", ""),
        "thread_source": payload.get("thread_source", ""),
        # 新旧ログ形式の両方で、承認判定・サブエージェント会話を除外できるようにする。
        "subagent": isinstance(source, dict) and bool(source.get("subagent")),
    }
    # 起動直後はファイルだけ存在し、先頭行がまだ書かれていないことがある。
    # 空の結果を永続キャッシュすると、その後もguardian判定や会話IDを復元
    # できなくなるため、完全なsession_metaだけをキャッシュする。
    if not head["id"]:
        return head
    with CODEX_HEAD_LOCK:
        CODEX_HEAD_CACHE[path] = head
        for old in list(CODEX_HEAD_CACHE)[: len(CODEX_HEAD_CACHE) - LOG_META_LIMIT]:
            CODEX_HEAD_CACHE.pop(old, None)
    return head


def claude_session_cwd(path):
    """claudeログ冒頭の cwd を返す（パス単位でキャッシュ）。"""
    with CLAUDE_CWD_LOCK:
        cached = CLAUDE_CWD_CACHE.get(path)
    if cached is not None:
        return cached
    cwd = next(
        (item["cwd"] for item in read_json_lines(path, 40) if item.get("cwd")), ""
    )
    with CLAUDE_CWD_LOCK:
        CLAUDE_CWD_CACHE[path] = cwd
        for old in list(CLAUDE_CWD_CACHE)[: len(CLAUDE_CWD_CACHE) - LOG_META_LIMIT]:
            CLAUDE_CWD_CACHE.pop(old, None)
    return cwd


def stat_entry(path, session_id):
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return {
        "id": session_id, "mtime": stat.st_mtime,
        "created": getattr(stat, "st_birthtime", stat.st_ctime), "path": path,
    }


def find_log_by_id(tool, session_id):
    """cwd に依存せず会話IDだけでログを探す。

    `codex resume <id>` は元のプロジェクトと別のディレクトリからでも実行できるため、
    pane の cwd とログの session_meta.cwd が食い違うことがある。
    """
    if not session_id:
        return ""
    if tool == "claude":
        paths = glob.glob(f"{HOME}/.claude/projects/*/{session_id}.jsonl")
    else:
        paths = glob.glob(
            f"{HOME}/.codex/sessions/**/*-{session_id}.jsonl", recursive=True
        )
    return paths[0] if paths else ""


def claude_project_dir(cwd):
    """cwd を Claude Code の会話ログ用プロジェクト名へ変換する。"""
    return "-" + re.sub(r"[^A-Za-z0-9]", "-", cwd.strip("/"))


def resume_candidates(tool, cwd, explicit_id=""):
    """cwdに属する保存済みセッションを新しい順で返す。

    ログ本文のパースは実際に返す10件だけに絞る。プロジェクトによっては
    数百MBのログが100本以上あり、全件読むと1秒以上かかるため。
    """
    entries = []
    if tool == "claude":
        project_dir = claude_project_dir(cwd)
        for path in glob.glob(f"{HOME}/.claude/projects/{project_dir}/*.jsonl"):
            session_id = os.path.basename(path).removesuffix(".jsonl")
            if not re.fullmatch(r"[0-9a-f-]{36}", session_id):
                continue
            entry = stat_entry(path, session_id)
            if entry:
                entries.append(entry)
    elif tool == "codex":
        for path in glob.glob(f"{HOME}/.codex/sessions/**/*.jsonl", recursive=True):
            head = codex_session_head(path)
            if head["thread_source"] == "subagent" or head["subagent"]:
                continue
            if head["cwd"] != cwd or not re.fullmatch(r"[0-9a-f-]{36}", head["id"]):
                continue
            entry = stat_entry(path, head["id"])
            if entry:
                entries.append(entry)
    entries.sort(key=lambda item: item["mtime"], reverse=True)
    if explicit_id and not any(item["id"] == explicit_id for item in entries):
        # cwd フィルターに合致しなくても、IDが分かっていればログ本体を直接探す
        path = find_log_by_id(tool, explicit_id)
        entry = stat_entry(path, explicit_id) if path else None
        entries.append(entry or {
            "id": explicit_id, "mtime": time.time(), "created": time.time(), "path": "",
        })
    if explicit_id:
        entries.sort(key=lambda item: item["id"] != explicit_id)
    candidates = entries[:10]
    for item in candidates:
        meta = log_meta(item["path"], tool) if item["path"] else {}
        item["summary"] = meta.get("summary", "")
        item["last_message"] = meta.get("last_message", "")
        item["model"] = meta.get("model", "")
        item["model_at"] = meta.get("model_at", 0.0)
        item["context"] = meta.get("context")
        item["label"] = time.strftime("%m/%d %H:%M", time.localtime(item["mtime"]))
        item["exact"] = item["id"] == explicit_id
    return candidates


def recent_conversations(limit=24):
    """全プロジェクト横断で最近の会話を新しい順に返す（起動ページのresume用）。

    stat だけで新しい順に並べ、ログ本文を読むのは表示する件数分に留める。
    実行中の tmux セッション・WezTerm タブに紐付いた会話は、二重起動を防ぐため
    除外する（前者はサイドバーから、後者は移行導線から操作できる）。
    """
    active_ids = {
        item["session_id"] for item in managed_sessions() if item["session_id"]
    }
    active_paths = {
        item["log_path"] for item in managed_sessions() if item.get("log_path")
    }
    for pane in wezterm_panes():
        info = wez_view_session(pane.get("pane_id"))
        if info and info.get("log_path"):
            active_paths.add(info["log_path"])
    entries = []
    for path in glob.glob(f"{HOME}/.claude/projects/*/*.jsonl"):
        session_id = os.path.basename(path).removesuffix(".jsonl")
        if not re.fullmatch(r"[0-9a-f-]{36}", session_id):
            continue
        entry = stat_entry(path, session_id)
        if entry:
            entry["tool"] = "claude"
            entries.append(entry)
    for path in glob.glob(f"{HOME}/.codex/sessions/**/*.jsonl", recursive=True):
        head = codex_session_head(path)
        if head["thread_source"] == "subagent" or head["subagent"]:
            continue
        entry = stat_entry(path, "")
        if entry:
            entry["tool"] = "codex"
            entries.append(entry)
    entries.sort(key=lambda item: item["mtime"], reverse=True)
    items = []
    for entry in entries:
        if len(items) >= limit:
            break
        if entry["tool"] == "codex":
            head = codex_session_head(entry["path"])
            entry["id"], cwd = head["id"], head["cwd"]
            if not re.fullmatch(r"[0-9a-f-]{36}", entry["id"] or ""):
                continue
        else:
            cwd = claude_session_cwd(entry["path"])
        if entry["id"] in active_ids or entry["path"] in active_paths:
            continue
        # 起動時の validate_dir と同じ条件。ホーム外（scratchpad等）は起動できない
        if not (cwd == HOME or cwd.startswith(HOME + "/")) or not os.path.isdir(cwd):
            continue
        # recent_dirs 設定時は、その配下の会話だけを出す
        if RECENT_DIRS and not any(
            cwd == base or cwd.startswith(base + "/") for base in RECENT_DIRS
        ):
            continue
        meta = log_meta(entry["path"], entry["tool"])
        summary = meta.get("summary") or meta.get("last_message")
        if not summary:
            # 起動して即終了した空セッションは再開する意味がないので出さない
            continue
        items.append({
            "tool": entry["tool"], "id": entry["id"], "cwd": cwd,
            "summary": summary,
            "label": time.strftime("%m/%d %H:%M", time.localtime(entry["mtime"])),
        })
    return items


def conversation_log_path(tool, cwd, session_id):
    """cwd の会話ログから session_id のファイルを探す。見つからなければ空文字。"""
    if tool == "claude":
        project_dir = claude_project_dir(cwd)
        path = f"{HOME}/.claude/projects/{project_dir}/{session_id}.jsonl"
        return path if os.path.isfile(path) else ""
    for path in glob.glob(f"{HOME}/.codex/sessions/**/*.jsonl", recursive=True):
        head = codex_session_head(path)
        if head["id"] == session_id and head["cwd"] == cwd:
            return path
    return ""


def tmux_run(*args, input_text=None, timeout=10):
    return subprocess.run(
        [TMUX, *args], input=input_text, capture_output=True, text=True, timeout=timeout
    )


def first_prompt_from_screen(output):
    for line in output.splitlines():
        text = line.strip()
        match = re.match(r"^[❯›]\s+(.+)$", text)
        if not match:
            continue
        prompt = " ".join(match.group(1).strip().split())
        if prompt and prompt not in {
            "Explain this codebase", "Improve documentation in @filename",
        } and not prompt.startswith("/"):
            return prompt[:100]
    return ""


def last_prompt_from_screen(output):
    for line in reversed(output.splitlines()):
        text = line.strip()
        match = re.match(r"^[❯›]\s+(.+)$", text)
        if not match:
            continue
        prompt = " ".join(match.group(1).strip().split())
        if prompt and prompt not in {
            "Explain this codebase", "Improve documentation in @filename",
        } and not prompt.startswith("/"):
            return prompt[:140]
    return ""


def requested_model(name):
    """Web から /model で切り替えた直後の暫定値を (名前, 送信時刻) で返す。"""
    raw = tmux_run("show-option", "-qv", "-t", name, "@launcher_model").stdout.strip()
    model, _, sent_at = raw.partition(" ")
    try:
        return model, float(sent_at)
    except ValueError:
        return model, 0.0


def current_model(name, exact, agent):
    """ログ・Webからの切替指示・起動オプションのうち、いちばん新しい情報を採る。"""
    logged, logged_at = (exact.get("model", ""), exact.get("model_at", 0.0)) if exact else ("", 0.0)
    # TUI 側で /model された場合はログの方が新しくなり、暫定値は自然に捨てられる。
    requested, requested_at = requested_model(name)
    if requested and requested_at > logged_at:
        return requested
    return logged or (agent["model"] if agent else "")


def load_managed_sessions():
    """ランチャーが作成した tmux セッションの一覧を返す。"""
    try:
        result = tmux_run(
            "list-panes", "-a",
            "-F", "#{session_name}\t#{pane_id}\t#{pane_current_path}\t#{pane_current_command}\t#{pane_tty}",
        )
        if result.returncode != 0:
            return []
        sessions = []
        seen = set()
        for line in result.stdout.splitlines():
            parts = line.split("\t", 4)
            if len(parts) != 5 or not parts[0].startswith("agent-") or parts[0] in seen:
                continue
            seen.add(parts[0])
            tool = tmux_run("show-option", "-qv", "-t", parts[0], "@launcher_tool").stdout.strip()
            bypass = tmux_run(
                "show-option", "-qv", "-t", parts[0], "@launcher_bypass"
            ).stdout.strip() == "1"
            summary = tmux_run(
                "show-option", "-qv", "-t", parts[0], "@launcher_summary"
            ).stdout.strip()
            note = tmux_run(
                "show-option", "-qv", "-t", parts[0], "@launcher_note"
            ).stdout.strip()
            pinned = tmux_run(
                "show-option", "-qv", "-t", parts[0], "@launcher_pinned"
            ).stdout.strip() == "1"
            agent = pane_agent({"tty_name": parts[4]})
            exact = None
            session_id = tmux_run(
                "show-option", "-qv", "-t", parts[0], "@launcher_session_id"
            ).stdout.strip()
            if not session_id and agent:
                session_id = agent["explicit_id"]
            # プロセスを特定できなくても、起動時に保存した tool と session_id が
            # あればログは解決できる（プロンプト付き起動の codex 等で agent が
            # 取れないことがある）。
            if agent or (tool and session_id):
                candidates = resume_candidates(
                    agent["tool"] if agent else tool, parts[2], session_id
                )
                if not session_id:
                    try:
                        started = time.mktime(time.strptime(
                            parts[0].split("-", 3)[1] + "-" + parts[0].split("-", 3)[2],
                            "%Y%m%d-%H%M%S",
                        ))
                        nearest = min(candidates, key=lambda item: abs(item["created"] - started))
                        if abs(nearest["created"] - started) <= 120:
                            session_id = nearest["id"]
                            tmux_run("set-option", "-t", parts[0], "@launcher_session_id", session_id)
                    except (ValueError, IndexError):
                        pass
                exact = next((item for item in candidates if item["id"] == session_id), None)
            if exact:
                if not summary:
                    summary = exact["summary"]
            try:
                screen = capture_session(parts[0])
            except RuntimeError:
                screen = ""
            if not summary and screen:
                summary = first_prompt_from_screen(screen)
            last_message = exact["last_message"] if exact else ""
            if not last_message and screen:
                last_message = last_prompt_from_screen(screen)
            if not last_message:
                last_message = summary
            log_path = exact.get("path", "") if exact else ""
            running = screen_is_running(screen, tool or parts[3])
            sessions.append({
                "name": parts[0], "pane_id": parts[1], "cwd": parts[2],
                "command": parts[3], "tool": tool or parts[3], "summary": summary,
                "last_message": last_message, "log_path": log_path,
                "note": note,
                "pinned": pinned,
                "session_id": session_id, "bypass": bypass,
                "running": running,
                "background": "" if running else screen_background_label(screen),
                "model": current_model(parts[0], exact, agent),
                "context": exact.get("context") if exact else None,
                "artifacts": session_artifacts(log_path, tool or parts[3]),
            })
        # 動いていないセッションは返事を待っている。新しい順のまま上へ寄せる。
        # バックグラウンド監視中は返事を求めていないので実行中と同じ扱い。
        sessions.sort(key=lambda item: item["name"], reverse=True)
        sessions.sort(key=lambda item: bool(item["running"] or item["background"]))
        sessions.sort(key=lambda item: not item["pinned"])
        return sessions
    except (OSError, subprocess.SubprocessError):
        return []


def fill_session_cache():
    try:
        items = load_managed_sessions()
    except Exception:
        # loading を握ったまま死ぬと以降の更新が止まるので必ず戻す。
        with SESSION_CACHE_LOCK:
            SESSION_CACHE["loading"] = False
        raise
    with SESSION_CACHE_LOCK:
        SESSION_CACHE["items"] = items
        SESSION_CACHE["loaded"] = True
        SESSION_CACHE["expires"] = time.time() + SESSION_CACHE_TTL
        SESSION_CACHE["loading"] = False


def managed_sessions():
    """セッション一覧を返す。期限切れでも古い値を即返し、更新は裏で回す。

    1秒間隔のポーリングと重い再構築がぶつかると画面が固まるため、
    リクエストは待たせない。
    """
    with SESSION_CACHE_LOCK:
        if SESSION_CACHE["expires"] > time.time():
            return SESSION_CACHE["items"]
        loaded = SESSION_CACHE["loaded"]
        start = not SESSION_CACHE["loading"]
        if start:
            SESSION_CACHE["loading"] = True
    if loaded:
        # 返せる値があるので更新はバックグラウンドに逃がす。
        if start:
            threading.Thread(target=fill_session_cache, daemon=True).start()
        with SESSION_CACHE_LOCK:
            return SESSION_CACHE["items"]
    if start:
        fill_session_cache()
    else:
        # 別スレッドが初回ロード中。完了を待つ以外に返せる値がない。
        deadline = time.time() + 15
        while time.time() < deadline:
            with SESSION_CACHE_LOCK:
                if SESSION_CACHE["loaded"]:
                    break
            time.sleep(0.05)
    with SESSION_CACHE_LOCK:
        return SESSION_CACHE["items"]


def invalidate_session_cache():
    # loaded を落として次の取得を同期ロードにする。
    # 起動/終了直後は古い一覧を返さず、確実に反映させたい。
    with SESSION_CACHE_LOCK:
        SESSION_CACHE["expires"] = 0
        SESSION_CACHE["loaded"] = False


def valid_session(name):
    if not re.fullmatch(r"agent-[A-Za-z0-9_.-]+", name or ""):
        return False
    return any(item["name"] == name for item in managed_sessions())


def dir_label(cwd):
    # セッション一覧にはフルパスでなくディレクトリ名だけを見せる
    return os.path.basename((cwd or "").rstrip("/")) or cwd or ""


def tool_label(tool):
    """ツール名の表示HTML。公式アイコンがあれば画像、無ければテキスト。"""
    escaped = html.escape(tool)
    if tool in TOOL_ICONS:
        return (
            f'<img class="tool" src="/tool-icon/{escaped}.png" '
            f'alt="{escaped}" title="{escaped}">'
        )
    return f'<span class="tool">{escaped}</span>'


def build_sidebar(active):
    """2ペイン表示のサイドバーHTMLを組み立てる。activeは選択中のセッション名。"""
    sidebar = '<a class="new-link" href="/new">＋ 新規起動</a>'
    sidebar += (
        '<label class="filter-toggle"><input type="checkbox" id="filter-need">'
        "要対応のみ表示</label>"
    )
    sidebar += '<div id="side-sessions">'
    # ピン留めだけを最優先し、各グループ内では一覧本来の順序を保つ。
    sessions = sorted(
        managed_sessions(),
        key=lambda item: not item.get("pinned"),
    )
    for other in sessions:
        status_text, status_class = sidebar_status(other)
        keep = " f-keep" if status_class in ("need", "ask", "wait") else ""
        # 最初のプロンプトでセッションを識別し、最終メッセージは同じでなければ添える。
        first = other["summary"]
        last = other["last_message"]
        lines = f'<small class="first">{html.escape(first)}</small>' if first else ""
        if last and last != first:
            lines += f"<small>{html.escape(last)}</small>"
        if other.get("note"):
            lines += f'<small class="note">📝 {html.escape(other["note"])}</small>'
        lines += artifact_chips(other.get("artifacts", []))
        sidebar += (
            f'<a class="{"active" if other["name"] == active else ""}{keep}" '
            f'href="/terminal?session={urllib.parse.quote(other["name"])}">'
            f'<strong>{tool_label(other["tool"])}'
            f'<span class="dir">{html.escape(dir_label(other["cwd"]))}</span>'
            f'{"<span class=\"pin\" title=\"ピン留め中\">📌</span>" if other.get("pinned") else ""}'
            f'<span class="st st-{status_class}">{html.escape(status_text)}</span>'
            f'{context_chip(other.get("context"))}</strong>'
            f'{lines}</a>'
        )
    sidebar += "</div>"
    # WezTermタブで動いているCLIも一覧に出す。tmux管理外なので操作は
    # できないが、会話ログを特定できれば読み取り専用ページへリンクする。
    for pane in wezterm_panes():
        agent = pane_agent(pane)
        if not agent:
            continue
        pane_id = str(pane.get("pane_id"))
        info = wez_view_session(pane_id)
        if info:
            name = f"wez-{pane_id}"
            label_html = f'<small>{html.escape(info["label"])}</small>' if info["label"] else ""
            label_html += artifact_chips(session_artifacts(info["log_path"], info["tool"]))
            sidebar += (
                f'<a class="wez{" active" if name == active else ""}" '
                f'href="/terminal?session={name}">'
                f'<strong>{tool_label(info["tool"])}'
                f'<span class="dir">{html.escape(dir_label(info["cwd"]))}</span>'
                f'<span class="wez-badge">WezTerm</span>'
                f'{context_chip(info.get("context"))}</strong>'
                f'{label_html}</a>'
            )
        else:
            cwd = urllib.parse.urlparse(pane.get("cwd", "")).path
            sidebar += (
                f'<div class="wez"><strong>{tool_label(agent["tool"])}'
                f'<span class="dir">{html.escape(dir_label(cwd))}</span>'
                f'<span class="wez-badge">WezTerm</span></strong></div>'
            )
    # バージョンとAI使用量は一覧が短いときもサイドバー最下部へ置く。
    sidebar += (
        '<div id="sidebar-footer"><div id="app-meta">'
        f'<span>Agent Deck v{html.escape(VERSION)}</span>'
        '<button type="button" id="app-update" hidden>アップデート</button>'
        '<small id="update-status"></small></div>'
        '<div id="ai-usage" hidden></div></div>'
    )
    return sidebar


# AI使用量の表示（任意機能）。config の usage_command に、使用量JSONを標準出力へ
# 出すコマンドを設定すると有効になる。期待する形式は
# {"providers": [{"name", "ok", "rows": [{"label", "percent", "reset_label",
# "level"}], "extra", "stale", "message"}]}（tools/ai-usage の --json 互換）。
USAGE_COMMAND = CONFIG.get("usage_command", "")
USAGE_TTL_SEC = 300  # 使用量APIは非公開なので叩きすぎない
USAGE_CACHE = {"data": None, "at": 0.0}
USAGE_LOCK = threading.Lock()


def usage_data():
    """usage_command の実行結果をTTL付きで返す。未設定・失敗時は None か古い値。"""
    if not USAGE_COMMAND:
        return None
    with USAGE_LOCK:
        if USAGE_CACHE["data"] is not None and time.time() - USAGE_CACHE["at"] < USAGE_TTL_SEC:
            return USAGE_CACHE["data"]
        try:
            result = subprocess.run(
                USAGE_COMMAND, shell=True, capture_output=True, text=True, timeout=25
            )
            data = json.loads(result.stdout)
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            return USAGE_CACHE["data"]
        USAGE_CACHE.update(data=data, at=time.time())
        return data


def capture_session(name):
    result = tmux_run("capture-pane", "-p", "-J", "-S", "-", "-t", name)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "画面を取得できませんでした")
    return result.stdout


def fast_mode_enabled(name):
    tail = "\n".join(capture_session(name).splitlines()[-20:])
    return bool(re.search(r"\bfast\s+·", tail))


def send_session_text(name, value, enter=True):
    fast_command = re.fullmatch(r"/fast(?:\s+(on|off|status))?", value.strip())
    if fast_command:
        action = fast_command.group(1) or "toggle"
        enabled = fast_mode_enabled(name)
        if action == "status":
            return f"Fast mode: {'ON' if enabled else 'OFF'}"
        if action in {"on", "off"} and enabled == (action == "on"):
            return f"Fast modeは既に{'ON' if enabled else 'OFF'}です"
        value = "/fast"
    # Codex/Claudeのスラッシュコマンドはペーストだと通常プロンプト扱いに
    # なるため、1行のコマンドだけは実際のキー入力として送る。
    if value.startswith("/") and "\n" not in value:
        # TUIに残った入力を消し、各文字を個別のキーイベントとして送る。
        tmux_run("send-keys", "-t", name, "C-u")
        time.sleep(0.1)
        for character in value:
            typed = tmux_run("send-keys", "-l", "-t", name, character)
            if typed.returncode != 0:
                raise RuntimeError(typed.stderr.strip() or "入力を送信できませんでした")
            time.sleep(0.035)
        if enter:
            time.sleep(0.2)
            tmux_run("send-keys", "-t", name, "Enter")
            if fast_command:
                time.sleep(0.2)
                tmux_run("send-keys", "-t", name, "Enter")
        return "Fast modeを切り替えました" if fast_command else None
    bash_mode = value.startswith("!") and "\n" not in value
    if bash_mode:
        # ! はキー入力でないと TUI の bash モードに切り替わらない。モードだけ
        # キーで開き、コマンド本体は下のペースト経路に流す（1文字ずつより速い）。
        tmux_run("send-keys", "-t", name, "C-u")
        time.sleep(0.1)
        opened = tmux_run("send-keys", "-l", "-t", name, "!")
        if opened.returncode != 0:
            raise RuntimeError(opened.stderr.strip() or "入力を送信できませんでした")
        time.sleep(0.2)
        value = value[1:].lstrip()
        if not value:
            return None
    if enter and not bash_mode:
        # Esc割り込みでTUIの入力行に復元されたメッセージ等が残っていると、
        # 貼り付けた本文と連結されて送信されてしまう。送信時は先にクリアする。
        tmux_run("send-keys", "-t", name, "C-u")
        time.sleep(0.1)
    buffer_name = "web-" + uuid.uuid4().hex
    loaded = tmux_run("load-buffer", "-b", buffer_name, "-", input_text=value)
    if loaded.returncode != 0:
        raise RuntimeError(loaded.stderr.strip() or "入力を送信できませんでした")
    try:
        # -p: TUI が bracketed paste を要求していれば制御コード付きで貼る。
        # これが無いと複数行テキストの改行が TUI 側で落ちる。
        pasted = tmux_run("paste-buffer", "-d", "-p", "-b", buffer_name, "-t", name)
        if pasted.returncode != 0:
            raise RuntimeError(pasted.stderr.strip() or "入力を送信できませんでした")
        if enter:
            # Codex/Claude の TUI が貼り付けイベントを処理してから Enter を送る。
            # 直後に送ると Enter が先に処理され、文字だけ入力欄に残ることがある。
            # bash モードは切り替え直後で描画が重なるため、少し長めに待つ。
            time.sleep(0.3 if bash_mode else 0.15)
            tmux_run("send-keys", "-t", name, "Enter")
    finally:
        tmux_run("delete-buffer", "-b", buffer_name)
    return None


def cleanup_uploads(max_age=90 * 24 * 60 * 60):
    cutoff = time.time() - max_age
    try:
        for session_name in os.listdir(UPLOAD_DIR):
            session_dir = os.path.join(UPLOAD_DIR, session_name)
            if not os.path.isdir(session_dir):
                continue
            for filename in os.listdir(session_dir):
                path = os.path.join(session_dir, filename)
                try:
                    if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                        os.unlink(path)
                except OSError:
                    pass
            try:
                os.rmdir(session_dir)
            except OSError:
                pass
    except OSError:
        pass


def save_uploaded_image(data, content_type, session_name):
    image_types = {
        "image/png": (".png", (b"\x89PNG\r\n\x1a\n",)),
        "image/jpeg": (".jpg", (b"\xff\xd8\xff",)),
        "image/gif": (".gif", (b"GIF87a", b"GIF89a")),
        "image/webp": (".webp", (b"RIFF",)),
    }
    media_type = content_type.split(";", 1)[0].lower()
    if media_type not in image_types:
        raise ValueError("PNG・JPEG・GIF・WebP画像のみ添付できます")
    if not data or len(data) > 15 * 1024 * 1024:
        raise ValueError("画像は15MBまでです")
    extension, signatures = image_types[media_type]
    if not any(data.startswith(signature) for signature in signatures):
        raise ValueError("画像データを確認できませんでした")
    if media_type == "image/webp" and data[8:12] != b"WEBP":
        raise ValueError("画像データを確認できませんでした")
    cleanup_uploads()
    session_dir = os.path.join(UPLOAD_DIR, session_name)
    os.makedirs(session_dir, mode=0o700, exist_ok=True)
    filename = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}{extension}"
    path = os.path.join(session_dir, filename)
    with open(path, "xb") as output:
        output.write(data)
    os.chmod(path, 0o600)
    return path


def save_uploaded_file(data, filename, session_name):
    """画像以外も含む任意ファイルを保存し、TUI に読ませるパスを返す。"""
    if not data or len(data) > 15 * 1024 * 1024:
        raise ValueError("ファイルは15MBまでです")
    base = os.path.basename(filename or "").strip()
    # パス区切り・制御文字・空白を潰し、先頭ドットを除いて隠しファイル化を防ぐ
    base = re.sub(r"[\\/\x00-\x1f\x7f\s]+", "_", base).lstrip(".")[:80]
    if not base:
        raise ValueError("ファイル名を確認できませんでした")
    cleanup_uploads()
    session_dir = os.path.join(UPLOAD_DIR, session_name)
    os.makedirs(session_dir, mode=0o700, exist_ok=True)
    unique = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    path = os.path.join(session_dir, f"{unique}-{base}")
    with open(path, "xb") as output:
        output.write(data)
    os.chmod(path, 0o600)
    return path


def save_handoff(item):
    """別ツールへ渡す会話記録をローカルの Markdown として保存する。"""
    cleanup_uploads()
    session_dir = os.path.join(UPLOAD_DIR, item["name"])
    os.makedirs(session_dir, mode=0o700, exist_ok=True)
    filename = f"{time.strftime('%Y%m%d-%H%M%S')}-handoff-{uuid.uuid4().hex[:8]}.md"
    path = os.path.join(session_dir, filename)
    transcript = session_transcript(item.get("log_path", ""), item["tool"])
    content = (
        "# AI セッション引き継ぎ\n\n"
        f"- 引き継ぎ元: {item['tool']}\n"
        f"- 作業ディレクトリ: {item['cwd']}\n"
        f"- 作成日時: {datetime.datetime.now().astimezone().isoformat(timespec='seconds')}\n\n"
        "## 会話履歴\n\n"
        f"{transcript or '会話履歴を取得できませんでした。'}\n"
    )
    with open(path, "x", encoding="utf-8") as output:
        output.write(content)
    os.chmod(path, 0o600)
    return path


def launcher_session_name(output):
    match = re.search(r"\bsession (agent-[A-Za-z0-9_.-]+)\)?", output or "")
    return match.group(1) if match else ""


def set_session_metadata(
    name, summary="", session_id="", bypass=False, note="", pinned=False
):
    if not name:
        return
    if summary:
        tmux_run("set-option", "-t", name, "@launcher_summary", summary[:200])
    if session_id:
        tmux_run("set-option", "-t", name, "@launcher_session_id", session_id)
    if bypass:
        # restart（resume）でも同じ権限モードを引き継げるよう記録する
        tmux_run("set-option", "-t", name, "@launcher_bypass", "1")
    if note:
        tmux_run("set-option", "-t", name, "@launcher_note", note[:1000])
    if pinned:
        tmux_run("set-option", "-t", name, "@launcher_pinned", "1")


def wait_for_new_session_id(tool, cwd, started_at, timeout=4):
    deadline = time.time() + timeout
    while time.time() < deadline:
        candidates = resume_candidates(tool, cwd)
        recent = [item for item in candidates if item["created"] >= started_at - 2]
        if recent:
            return min(recent, key=lambda item: abs(item["created"] - started_at))["id"]
        time.sleep(0.2)
    return ""


def validate_dir(path: str):
    path = os.path.realpath(os.path.expanduser(path))
    if not path.startswith(HOME + "/") and path != HOME:
        return None, "ホームディレクトリ配下のみ指定できます"
    if not os.path.isdir(path):
        return None, f"ディレクトリが存在しません: {path}"
    return path, None


def load_cw_cache():
    try:
        with open(CW_CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def save_cw_cache(data):
    tmp = CW_CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, CW_CACHE_PATH)


def cw_get(endpoint):
    with open(CW_TOKEN_PATH, encoding="utf-8") as f:
        token = f.read().strip()
    if not token:
        raise RuntimeError("Chatwork token が空です")
    req = urllib.request.Request(
        CW_API + endpoint, headers={"X-ChatWorkToken": token}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def room_updated_at(room):
    return int(room.get("last_update_time") or room.get("update_time") or 0)


def message_item(message, room):
    account = message.get("account") or {}
    room_id = room.get("room_id")
    message_id = str(message.get("message_id") or "")
    return {
        "room_id": room_id,
        "room_name": room.get("name", ""),
        "message_id": message_id,
        "sender": account.get("name", ""),
        "sender_account_id": account.get("account_id"),
        "send_time": message.get("send_time"),
        "body": message.get("body", "") or "",
        "url": f"https://www.chatwork.com/#!rid{room_id}-{message_id}",
    }


def refresh_chatwork(force=False):
    """ルーム一覧1回と、更新されたルームだけを取得してキャッシュする。"""
    with CW_LOCK:
        cache = load_cw_cache()
        now = int(time.time())
        if not force and cache.get("rooms") and now - cache.get("refreshed_at", 0) < 60:
            return cache
        rooms = cw_get("/rooms")
        rooms = sorted(rooms, key=room_updated_at, reverse=True)
        old_updates = cache.get("room_updates", {})
        cached_messages = cache.get("messages", {})
        if old_updates:
            targets = [
                room for room in rooms
                if room_updated_at(room) > int(old_updates.get(str(room["room_id"]), -1))
            ]
        else:
            targets = rooms[:CW_INITIAL_ROOM_LIMIT]
        errors = []
        new_updates = {str(r["room_id"]): room_updated_at(r) for r in rooms}
        for room in targets:
            room_id = str(room["room_id"])
            try:
                messages = cw_get(f"/rooms/{room_id}/messages?force=1")
                cached_messages[room_id] = [
                    message_item(message, room) for message in messages[-CW_MESSAGE_LIMIT:]
                ]
            except Exception as exc:
                errors.append(f"room {room_id}: {type(exc).__name__}")
                if room_id in old_updates:
                    new_updates[room_id] = old_updates[room_id]
        cache = {
            "refreshed_at": now,
            "rooms": rooms[:20],
            "room_updates": new_updates,
            "messages": cached_messages,
            "errors": errors,
        }
        save_cw_cache(cache)
        return cache


def recent_mentions(cache):
    to_marker = f"[To:{CW_ACCOUNT_ID}]"
    reply_marker = f"[rp aid={CW_ACCOUNT_ID}"
    items = []
    for messages in cache.get("messages", {}).values():
        for item in messages:
            if item.get("sender_account_id") == CW_ACCOUNT_ID:
                continue
            body = item.get("body", "")
            if to_marker in body or reply_marker in body:
                items.append(item)
    return sorted(items, key=lambda x: int(x.get("send_time") or 0), reverse=True)[:10]


PAGE = r"""<!doctype html>
<html lang="ja"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>Agent Deck</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg?v={favicon_version}">
<link rel="apple-touch-icon" href="/favicon.svg?v={favicon_version}">
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, sans-serif; max-width: 620px; margin: 0 auto; padding: 20px; font-size: 18px; }}
  h1 {{ font-size: 1.65rem; }}
  h2 {{ font-size: 1.3rem; margin-top: 30px; color: #999; }}
  form.launch {{ margin: 0; }}
  button.proj {{ display: block; width: 100%; padding: 14px; margin: 8px 0; font-size: 1rem;
    border: 1px solid #8884; border-radius: 10px; background: #6c5ce71a; text-align: left; cursor: pointer; }}
  button.proj:active {{ background: #6c5ce74d; }}
  button.proj .resume-summary {{ display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  button.proj small {{ color: #888; }}
  .msg {{ padding: 12px; border-radius: 10px; margin: 12px 0; }}
  .ok {{ background: #00b8941a; border: 1px solid #00b894; }}
  .err {{ background: #d635451a; border: 1px solid #d63545; }}
  input[type=text] {{ width: 100%; padding: 10px; font-size: 1rem; border: 1px solid #8884;
    border-radius: 8px; box-sizing: border-box; margin: 8px 0; }}
  .models {{ display: flex; gap: 4px; margin: 8px 0 4px; }}
  .models label {{ flex: 1; text-align: center; padding: 8px 2px; font-size: .85rem;
    border: 1px solid #8884; border-radius: 8px; cursor: pointer; }}
  .models input {{ display: none; }}
  .models input:checked + span {{ font-weight: bold; }}
  .models label:has(input:checked) {{ background: #6c5ce74d; border-color: #6c5ce7; }}
  .bypass-modes label:has(input[value="bypass"]:checked) {{ background: #d635452e; border-color: #d63545; }}
  select {{ width: 100%; padding: 12px; font-size: 1rem; border: 1px solid #8884;
    border-radius: 8px; margin: 8px 0; -webkit-appearance: none; background: transparent; }}
  textarea {{ width: 100%; padding: 10px; font-size: 1rem; border: 1px solid #8884;
    border-radius: 8px; box-sizing: border-box; margin: 8px 0; font-family: inherit; resize: vertical; }}
  .cw-head {{ display: flex; align-items: center; justify-content: space-between; }}
  .cw-head h2 {{ margin-bottom: 8px; }}
  .cw-refresh, .cw-room, .cw-set {{ padding: 8px 10px; border: 1px solid #8884;
    border-radius: 8px; background: #6c5ce71a; cursor: pointer; }}
  .cw-room {{ display: block; width: 100%; margin: 6px 0; text-align: left; }}
  .cw-message {{ margin: 8px 0; padding: 10px; border: 1px solid #8883; border-radius: 8px; }}
  .cw-meta {{ color: #888; font-size: .8rem; margin-bottom: 6px; }}
  .cw-body {{ white-space: pre-wrap; overflow-wrap: anywhere; font-size: .9rem; }}
  .cw-set {{ margin-top: 8px; }}
  .cw-loading, .cw-empty {{ color: #888; font-size: .9rem; }}
  .bookmarklet {{ display: inline-block; text-decoration: none; color: inherit; }}
  .resume-more {{ display: none; }}
  .resume-more.open {{ display: block; }}
  nav {{ display: flex; gap: 8px; margin: 12px 0 20px; }}
  nav a {{ flex: 1; padding: 11px; text-align: center; color: inherit; text-decoration: none;
    border: 1px solid #8884; border-radius: 9px; }}
</style></head><body>
<h1>🚀 Agent Deck</h1>
<nav><a href="/">‹ セッションへ</a></nav>
{message}
<h2>ツール</h2>
<div class="models tools">
  <label><input type="radio" name="tool" value="claude" checked><span>Claude Code</span></label>
  <label><input type="radio" name="tool" value="codex"><span>Codex</span></label>
</div>
<h2>起動方法</h2>
<div class="models launch-modes">
  <label><input type="radio" name="launch-mode" value="web" checked><span>Web操作（tmux）</span></label>
  <label><input type="radio" name="launch-mode" value="wezterm"><span>WezTermのみ</span></label>
</div>
<h2>権限</h2>
<div class="models bypass-modes">
  <label><input type="radio" name="bypass" value="normal" checked><span>通常（確認あり）</span></label>
  <label><input type="radio" name="bypass" value="bypass"><span>⚠️ バイパス</span></label>
</div>
<h2>モデル</h2>
<div class="models" id="models-claude">{models_claude}</div>
<div class="models" id="models-codex" style="display:none">{models_codex}</div>
<h2>最初のプロンプト（任意）</h2>
<textarea id="prompt" rows="3" placeholder="起動時に渡す指示。空欄なら通常起動（画像もペースト可）"></textarea>
<div id="prompt-status" class="cw-empty"></div>
<h2>プロジェクトを選んで起動</h2>
{buttons}
<h2>その他のプロジェクト</h2>
<form class="launch" method="post" action="/launch">
  <select name="dir">{options}</select>
  <button class="proj" type="submit">🚀 選択したプロジェクトで起動</button>
</form>
<h2>任意のディレクトリ</h2>
<form class="launch" method="post" action="/launch">
  <input type="text" name="dir" placeholder="~/projects/..." autocapitalize="off" autocorrect="off">
  <button class="proj" type="submit">🚀 このディレクトリで起動</button>
</form>
<h2>最近の会話を再開</h2>
<div class="cw-empty">タップすると同じ会話の続きから起動する。起動方法・権限の選択は適用され、モデル・プロンプトは無視される。</div>
{resume_items}
{chatwork_panel}
<h2>ブックマークレット</h2>
<div class="cw-empty">下のリンクをブックマークバーへドラッグすると登録できる。閲覧中のページ（GitHub issue / Chatwork 等）をこのランチャーに送る。アドレスバーへの貼り付けは javascript: が削られて動かないので不可。</div>
<a class="cw-set bookmarklet" href="{bookmarklet}">📎 Agent Deckに送る</a>
<script>
  // ツール選択に応じてモデル選択肢を切り替える
  function currentTool() {{
    var t = document.querySelector(".tools input:checked");
    return t ? t.value : "claude";
  }}
  function syncModels() {{
    ["claude", "codex"].forEach(function (t) {{
      document.getElementById("models-" + t).style.display = t === currentTool() ? "" : "none";
    }});
  }}
  document.querySelectorAll(".tools input").forEach(function (r) {{
    r.addEventListener("change", syncModels);
  }});
  syncModels();
  // 選択中のツール・モデルを各起動フォームに hidden input として付与する。
  // resume フォームはツールが会話側で決まるため、起動方法と権限だけを引き継ぐ。
  document.querySelectorAll("form.launch").forEach(function (f) {{
    f.addEventListener("submit", function () {{
      var launchMode = document.querySelector('.launch-modes input:checked');
      var bypass = document.querySelector('.bypass-modes input:checked');
      var fields = [["launch_mode", launchMode ? launchMode.value : "web"],
       ["bypass", bypass && bypass.value === "bypass" ? "1" : "0"]];
      if (!f.dataset.resume) {{
        var t = currentTool();
        var m = document.querySelector("#models-" + t + " input:checked");
        var p = document.getElementById("prompt").value;
        fields.push(["model", m ? m.value : "default"], ["tool", t], ["prompt", p]);
      }}
      fields.forEach(function (kv) {{
        var h = document.createElement("input");
        h.type = "hidden"; h.name = kv[0]; h.value = kv[1];
        f.appendChild(h);
      }});
    }});
  }});
  // 折りたたまれた9件目以降の再開候補の表示切り替え
  var resumeToggle = document.getElementById("resume-toggle");
  if (resumeToggle) {{
    resumeToggle.addEventListener("click", function () {{
      var open = document.getElementById("resume-more").classList.toggle("open");
      resumeToggle.textContent = open ? "▴ 折りたたむ" : resumeToggle.dataset.label;
    }});
  }}
  // 最初のプロンプト欄への画像ペースト。アップロードしてパスを本文に差し込む
  var promptBox = document.getElementById("prompt");
  var promptStatus = document.getElementById("prompt-status");
  // ブックマークレット等からの ?prompt=... でプロンプト欄をプリフィルする
  var prefill = new URLSearchParams(location.search).get("prompt");
  if (prefill) {{
    promptBox.value = prefill.slice(0, 8000);
    promptBox.scrollIntoView({{behavior: "smooth", block: "center"}});
    promptBox.focus();
  }}
  async function uploadLaunchImage(file) {{
    if (file.size > 15 * 1024 * 1024) throw new Error("画像は15MBまでです");
    promptStatus.textContent = "画像をアップロード中...";
    var response = await fetch("/api/launch/image", {{
      method: "POST",
      headers: {{"Content-Type": file.type || "application/octet-stream"}},
      body: file,
    }});
    var data = await response.json();
    if (!response.ok) throw new Error(data.error || "画像のアップロードに失敗しました");
    var prefix = promptBox.value && !promptBox.value.endsWith("\n") ? "\n" : "";
    promptBox.value += prefix + "添付画像: " + data.path + "\n";
    promptStatus.textContent = "画像を添付しました";
  }}
  promptBox.addEventListener("paste", async function (event) {{
    var images = Array.from((event.clipboardData || {{}}).items || [])
      .filter(function (item) {{ return item.kind === "file" && item.type.indexOf("image/") === 0; }})
      .map(function (item) {{ return item.getAsFile(); }}).filter(Boolean);
    if (!images.length) return;
    event.preventDefault();
    try {{
      for (var i = 0; i < images.length; i++) await uploadLaunchImage(images[i]);
    }} catch (error) {{ promptStatus.textContent = "❌ " + error.message; }}
  }});
  function cleanChatwork(body) {{
    return (body || "")
      .replace(/\[To:\d+\]/g, "")
      .replace(/\[rp aid=\d+[^\]]*\]/g, "")
      .replace(/\[picon:\d+\]/g, "")
      .replace(/\[qtmeta[^\]]*\]/g, "")
      .replace(/\[hr\]/g, "────────")
      .replace(/\[(?:info|\/info|title|\/title|qt|\/qt|code|\/code)\]/g, "")
      .trim();
  }}
  function cwMessage(item) {{
    var box = document.createElement("div"); box.className = "cw-message";
    var meta = document.createElement("div"); meta.className = "cw-meta";
    var date = item.send_time ? new Date(item.send_time * 1000).toLocaleString("ja-JP") : "";
    meta.textContent = item.room_name + " · " + item.sender + (date ? " · " + date : "");
    var body = document.createElement("div"); body.className = "cw-body";
    body.textContent = cleanChatwork(item.body);
    var set = document.createElement("button"); set.type = "button"; set.className = "cw-set";
    set.textContent = "📝 プロンプトにセット";
    set.addEventListener("click", function () {{
      var prompt = "以下の Chatwork メッセージに対応してください。\n" + item.url
        + "\n（room_id: " + item.room_id + " / message_id: " + item.message_id
        + "。本文は Chatwork MCP の get_room_message で取得してください）";
      var textarea = document.getElementById("prompt"); textarea.value = prompt;
      textarea.scrollIntoView({{behavior: "smooth", block: "center"}}); textarea.focus();
    }});
    box.append(meta, body, set); return box;
  }}
  function showError(target, error) {{
    target.className = "msg err"; target.textContent = "❌ " + error;
  }}
  async function loadMentions(force) {{
    var target = document.getElementById("cw-mentions");
    target.className = "cw-loading"; target.textContent = "読み込み中...";
    try {{
      var response = await fetch("/api/mentions" + (force ? "?refresh=1" : ""));
      var data = await response.json(); if (!response.ok) throw new Error(data.error || "取得に失敗しました");
      target.className = ""; target.replaceChildren();
      if (!data.items.length) {{ target.className = "cw-empty"; target.textContent = "メンションはありません"; }}
      data.items.forEach(function (item) {{ target.appendChild(cwMessage(item)); }});
    }} catch (error) {{ showError(target, error.message); }}
  }}
  async function loadRooms(force) {{
    var target = document.getElementById("cw-rooms");
    target.className = "cw-loading"; target.textContent = "読み込み中...";
    try {{
      var response = await fetch("/api/rooms" + (force ? "?refresh=1" : ""));
      var data = await response.json(); if (!response.ok) throw new Error(data.error || "取得に失敗しました");
      target.className = ""; target.replaceChildren();
      data.items.forEach(function (room) {{
        var button = document.createElement("button"); button.type = "button"; button.className = "cw-room";
        button.textContent = room.name; button.addEventListener("click", function () {{ loadRoom(room.room_id, room.name); }});
        target.appendChild(button);
      }});
    }} catch (error) {{ showError(target, error.message); }}
  }}
  async function loadRoom(roomId, roomName) {{
    var target = document.getElementById("cw-room-messages");
    target.className = "cw-loading"; target.textContent = "「" + roomName + "」を読み込み中...";
    try {{
      var response = await fetch("/api/rooms/" + encodeURIComponent(roomId) + "/messages");
      var data = await response.json(); if (!response.ok) throw new Error(data.error || "取得に失敗しました");
      target.className = ""; target.replaceChildren();
      var heading = document.createElement("h2"); heading.textContent = roomName + " の直近メッセージ"; target.appendChild(heading);
      if (!data.items.length) {{ var empty = document.createElement("div"); empty.className = "cw-empty"; empty.textContent = "メッセージはありません"; target.appendChild(empty); }}
      data.items.forEach(function (item) {{ target.appendChild(cwMessage(item)); }});
      target.scrollIntoView({{behavior: "smooth", block: "start"}});
    }} catch (error) {{ showError(target, error.message); }}
  }}
  // Chatwork 連携が無効な場合はパネルごと描画されない
  var cwRefresh = document.getElementById("cw-refresh");
  if (cwRefresh) {{
    cwRefresh.addEventListener("click", function () {{ loadMentions(true); loadRooms(false); }});
    loadMentions(false); loadRooms(false);
  }}
</script>
</body></html>"""


# セッション一覧サイドバーのCSS。一覧ページ（LIST_PAGE）と2ペイン表示
# （TERMINAL_PAGE）で共有する。format() の値として挿入するので brace は素のまま。
SIDEBAR_CSS = r"""
  aside { width: 320px; flex: 0 0 320px; overflow-y: auto; --aside-pad-b: 12px;
    padding: 12px 12px var(--aside-pad-b); display: flex; flex-direction: column;
    border-right: 1px solid #30363d; background: #161b22; }
  /* flex化してもリストは潰さずasideのスクロールに任せる */
  aside > * { flex-shrink: 0; }
  /* AI使用量フッター。usage_command 設定時のみ表示。リストが短くても
     margin-top:auto で最下端に落とし、あふれたら sticky で張り付かせる */
  #sidebar-footer { position: sticky; bottom: calc(-1 * var(--aside-pad-b));
    margin: auto -12px calc(-1 * var(--aside-pad-b)); padding: 8px 12px var(--aside-pad-b);
    background: #161b22; border-top: 1px solid #30363d; }
  #app-meta { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 10px;
    color: #8b949e; font-size: .72rem; }
  #app-meta button { flex: 0 0 auto; width: auto; padding: 4px 8px; font-size: .72rem;
    color: #fff; background: #238636; border-color: #2ea043; }
  #app-meta small { flex-basis: 100%; margin: 0; color: #d29922; }
  #ai-usage { margin-top: 5px; font-size: .74rem; color: #8b949e; }
  #ai-usage .usage-row { display: flex; align-items: baseline; flex-wrap: wrap;
    gap: 2px 9px; margin: 3px 0; }
  #ai-usage .usage-name { font-weight: 600; color: #cdd9e5; }
  #ai-usage .usage-warning { color: #d9884f; }
  #ai-usage .usage-critical { color: #f85149; }
  #ai-usage .usage-err { color: #f85149; }
  aside h2 { margin: 4px 4px 12px; font-size: 1.05rem; }
  aside a { display: block; margin: 7px 0; padding: 10px; color: inherit; text-decoration: none;
    border: 1px solid #30363d; border-radius: 8px; overflow-wrap: anywhere; }
  aside a.active { border-color: #58a6ff; background: #1f6feb22; }
  aside strong, aside small { display: block; }
  aside small { margin-top: 5px; color: #8b949e; font-size: .78rem;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
    overflow: hidden; overflow-wrap: anywhere; }
  aside small.first { color: #cdd9e5; }
  aside small.note { color: #d29922; -webkit-line-clamp: 2; }
  aside #side-sessions a small:not(.first) { -webkit-line-clamp: 1; }
  aside .new-link { text-align: center; color: #8ab4f8; border-style: dashed; }
  aside .wez { margin: 7px 0; padding: 10px; border: 1px dashed #30363d; border-radius: 8px;
    overflow-wrap: anywhere; opacity: .75; }
  aside a.wez.active { opacity: 1; }
  aside .wez strong { display: flex; align-items: center; gap: 7px; min-width: 0; }
  aside .wez strong .dir { margin-left: 0; }
  .wez-badge { flex: 0 0 auto; padding: 1px 6px; font-size: .68rem; border: 1px solid #8b949e;
    border-radius: 6px; color: #8b949e; font-weight: 600; }
  .st { margin-left: 8px; padding: 1px 8px; font-size: .68rem; font-weight: 600;
    border-radius: 999px; border: 1px solid currentColor; }
  /* コンテキスト使用率。70%からオレンジ、90%から赤で圧迫を知らせる */
  .ctx { flex: 0 0 auto; margin-left: 6px; padding: 1px 7px; font-size: .68rem;
    font-weight: 600; border: 1px solid #30363d; border-radius: 999px; color: #8b949e; }
  .ctx-warn { color: #d9884f; border-color: #d9884f88; }
  .ctx-high { color: #f85149; border-color: #f8514988; }
  /* セッション中に作成したPR・issueのチップ。openは緑、mergedは紫、closedは
     グレー。GitHubの状態を取得できるまでは種別色（PR紫・issue緑）で出す */
  .arts { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 6px; }
  .art { padding: 1px 7px; font-size: .68rem; font-weight: 600; border-radius: 999px;
    border: 1px solid currentColor; text-decoration: none; white-space: nowrap; }
  .art-pr { color: #a371f7; }
  .art-issue { color: #3fb950; }
  .art-open { color: #3fb950; }
  .art-merged { color: #a371f7; }
  .art-closed { color: #8b949e; }
  .art-more { color: #8b949e; }
  a.art:hover { filter: brightness(1.25); }
  .st-run { color: #d9884f; animation: activity-pulse 1.3s ease-in-out infinite; }
  .st-ask { color: #d29922; }
  .st-wait { color: #3fb950; }
  .st-watch { color: #58a6ff; }
  .st-need { color: #f85149; }
  .st-blocked { color: #8b949e; }
  .st-done { color: #3fb950; }
  @keyframes activity-pulse { 0%, 100% { opacity: .3; } 50% { opacity: 1; } }
  aside .filter-toggle { display: flex; align-items: center; gap: 6px; margin: 2px 0 8px;
    color: #8b949e; font-size: .85rem; cursor: pointer; user-select: none; }
  aside .filter-toggle input { accent-color: #f85149; }
  /* 要対応のみ表示: 自分のアクションが要るもの（要対応・選択待ち・未分類の返事待ち）だけ残す */
  body.filter-need #side-sessions a:not(.f-keep) { display: none; }
  aside #side-sessions strong { display: flex; align-items: center; min-width: 0; }
  /* ツール名は潰さず、長いステータスやディレクトリ名は…で切る（縦書き化防止） */
  aside strong .tool { flex: 0 0 auto; }
  /* ツールの公式アイコン。テキスト表記の代わりに出す */
  img.tool { width: 21px; height: 21px; border-radius: 5px; flex: 0 0 auto; }
  aside strong .dir { flex: 0 1 auto; min-width: 0; margin-left: 7px; color: #8b949e;
    font-weight: 400; font-size: .78rem; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; }
  aside .pin { flex: 0 0 auto; font-size: .72rem; }
  aside #side-sessions .st { flex: 0 1 auto; min-width: 0; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
"""

# サイドバーの定期更新と「要対応のみ表示」トグル。両ページ共通。
# 呼び出し側のページで const session（一覧ページは null）と const bootId を
# 先に宣言しておくこと。
SIDEBAR_JS = r"""
  // 「要対応のみ表示」トグル。選択は端末ごとに覚える
  const filterNeed = document.getElementById("filter-need");
  if (filterNeed) {
    filterNeed.checked = localStorage.getItem("filterNeed") === "1";
    document.body.classList.toggle("filter-need", filterNeed.checked);
    filterNeed.addEventListener("change", () => {
      localStorage.setItem("filterNeed", filterNeed.checked ? "1" : "0");
      document.body.classList.toggle("filter-need", filterNeed.checked);
    });
  }
  // サイドバーのセッション一覧を定期更新する（状態バッジと最終メッセージ）
  const sideSessions = document.getElementById("side-sessions");
  async function refreshSidebar() {
    try {
      const response = await fetch("/api/sidebar");
      const data = await response.json();
      if (!response.ok || !sideSessions) return;
      // サーバー再起動後は描画済みHTMLが古いので読み込み直す
      if (data.boot && data.boot !== bootId) { location.reload(); return; }
      // ピン留めだけを最優先し、各グループ内ではAPIの順序を保つ。
      const items = data.items.slice().sort((a, b) =>
        Number(b.pinned) - Number(a.pinned));
      sideSessions.replaceChildren(...items.map(item => {
        const link = document.createElement("a");
        link.href = "/terminal?session=" + encodeURIComponent(item.name);
        if (item.name === session) link.classList.add("active");
        if (["need", "ask", "wait"].includes(item.status_class)) link.classList.add("f-keep");
        const title = document.createElement("strong");
        // 公式アイコンのあるツールは画像、それ以外はテキストで表示する
        let tool;
        if (item.tool_icon) {
          tool = document.createElement("img");
          tool.src = item.tool_icon;
          tool.alt = item.tool;
          tool.title = item.tool;
        } else {
          tool = document.createElement("span");
          tool.textContent = item.tool;
        }
        tool.className = "tool";
        const dir = document.createElement("span");
        dir.className = "dir";
        dir.textContent = item.dir || "";
        if (item.pinned) {
          const pin = document.createElement("span");
          pin.className = "pin";
          pin.title = "ピン留め中";
          pin.textContent = "📌";
          title.append(tool, dir, pin);
        } else {
          title.append(tool, dir);
        }
        const badge = document.createElement("span");
        badge.className = "st st-" + item.status_class;
        badge.textContent = item.status;
        title.append(badge);
        if (item.context !== null && item.context !== undefined) {
          const ctx = document.createElement("span");
          ctx.className = "ctx" +
            (item.context >= 90 ? " ctx-high" : item.context >= 70 ? " ctx-warn" : "");
          ctx.textContent = item.context + "%";
          title.append(ctx);
        }
        link.append(title);
        if (item.summary) {
          const first = document.createElement("small");
          first.className = "first";
          first.textContent = item.summary;
          link.append(first);
        }
        if (item.last_message && item.last_message !== item.summary) {
          const detail = document.createElement("small");
          detail.textContent = item.last_message;
          link.append(detail);
        }
        if (item.note) {
          const note = document.createElement("small");
          note.className = "note";
          note.textContent = "📝 " + item.note;
          link.append(note);
        }
        if (item.artifacts && item.artifacts.length) {
          const arts = document.createElement("span");
          arts.className = "arts";
          const shown = item.artifacts.slice(-5);
          if (item.artifacts.length > shown.length) {
            const more = document.createElement("span");
            more.className = "art art-more";
            more.textContent = "+" + (item.artifacts.length - shown.length);
            arts.append(more);
          }
          for (const art of shown) {
            const chip = document.createElement("span");
            chip.className = "art art-" + art.kind + (art.state ? " art-" + art.state : "");
            chip.textContent = art.repo + "#" + art.number;
            arts.append(chip);
          }
          link.append(arts);
        }
        return link;
      }));
    } catch (error) { /* サイドバーは更新失敗しても本体に影響させない */ }
  }
  setInterval(refreshSidebar, 5000);
  // 最新Releaseがある場合だけ更新ボタンを表示する。
  const updateButton = document.getElementById("app-update");
  const updateStatus = document.getElementById("update-status");
  async function checkVersion() {
    if (!updateButton) return;
    try {
      const response = await fetch("/api/version");
      const data = await response.json();
      updateButton.hidden = !data.available;
      if (data.available) {
        updateButton.textContent = "v" + data.latest + "へ更新";
        updateButton.dataset.version = data.latest;
      }
    } catch (error) { /* 更新確認の失敗は通常利用に影響させない */ }
  }
  if (updateButton) updateButton.addEventListener("click", async () => {
    const version = updateButton.dataset.version;
    if (!version || !confirm("Agent Deckをv" + version + "へ更新しますか？")) return;
    updateButton.disabled = true;
    updateStatus.textContent = "更新中…";
    try {
      const body = new URLSearchParams({version});
      const response = await fetch("/api/update", {
        method: "POST", headers: {"Content-Type": "application/x-www-form-urlencoded"}, body
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "更新できませんでした");
      updateStatus.textContent = "更新しました。再起動中…";
      setTimeout(() => location.reload(), 1800);
    } catch (error) {
      updateStatus.textContent = error.message;
      updateButton.disabled = false;
    }
  });
  checkVersion();
  // AI使用量フッター。サーバー側で5分キャッシュされるので同じ周期で見に行く
  const aiUsage = document.getElementById("ai-usage");
  async function refreshUsage() {
    if (!aiUsage) return;
    try {
      const response = await fetch("/api/usage");
      if (!response.ok) return;
      const data = await response.json();
      if (!data.providers || !data.providers.length) return;
      if (data.updated_at) aiUsage.title = "取得 " + data.updated_at;
      aiUsage.replaceChildren(...data.providers.map(provider => {
        const row = document.createElement("div");
        row.className = "usage-row";
        const name = document.createElement("span");
        name.className = "usage-name";
        name.textContent = provider.name;
        row.append(name);
        if (!provider.ok) {
          const err = document.createElement("span");
          err.className = "usage-err";
          err.textContent = "取得失敗";
          err.title = provider.message || "";
          row.append(err);
          return row;
        }
        const items = provider.rows.concat(provider.extra ? [provider.extra] : []);
        for (const item of items) {
          const span = document.createElement("span");
          span.className = "usage-item usage-" + item.level;
          span.textContent = item.label.replace("枠", "") + " " + Math.round(item.percent) + "%";
          if (item.reset_label) span.title = item.reset_label;
          row.append(span);
        }
        if (provider.stale) {
          const stale = document.createElement("span");
          stale.textContent = "⚠";
          stale.title = provider.stale;
          row.append(stale);
        }
        return row;
      }));
      aiUsage.hidden = false;
    } catch (error) { /* 使用量は取れなくても本体に影響させない */ }
  }
  refreshUsage();
  setInterval(refreshUsage, 300000);
"""


# デフォルト（/）のセッション一覧ページ。PCは左に一覧・右は選択か新規起動を
# 促すプレースホルダ、SPは一覧のみを全画面で表示する。
LIST_PAGE = r"""<!doctype html>
<html lang="ja"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>セッション一覧 - Agent Deck</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg?v={favicon_version}">
<link rel="apple-touch-icon" href="/favicon.svg?v={favicon_version}">
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; height: 100dvh; overflow: hidden;
    background: #0d1117; color: #e6edf3; font-family: -apple-system, sans-serif; font-size: 18px; }}
  .app {{ height: 100%; display: flex; }}
{sidebar_css}
  .placeholder {{ min-width: 0; flex: 1; display: grid; place-items: center; padding: 20px; }}
  .placeholder .inner {{ text-align: center; color: #8b949e; }}
  .placeholder p {{ margin: 0 0 18px; font-size: 1.05rem; }}
  .placeholder a {{ display: inline-block; padding: 13px 26px; border: 1px solid #2ea043;
    border-radius: 10px; background: #238636; color: #fff; text-decoration: none;
    font-weight: 600; }}
  @media (max-width: 799px) {{
    /* SPは一覧を全画面にし、右ペインは出さない */
    aside {{ width: 100%; flex: 1; border-right: none;
      --aside-pad-b: max(12px, env(safe-area-inset-bottom)); }}
    .placeholder {{ display: none; }}
  }}
</style></head><body>
<div class="app"><aside><h2>セッション</h2>{sessions_sidebar}</aside>
<main class="placeholder"><div class="inner">
  <p>左の一覧からセッションを選択してください</p>
  <a href="/new">＋ 新規セッションを開始</a>
</div></main></div>
<script>
  const session = null;
  const bootId = {boot_json};
{sidebar_js}
</script>
</body></html>"""


TERMINAL_PAGE = r"""<!doctype html>
<html lang="ja"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>{title}</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg?v={favicon_version}">
<link rel="apple-touch-icon" href="/favicon.svg?v={favicon_version}">
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; height: 100dvh; overflow: hidden;
    background: #0d1117; color: #e6edf3; font-family: -apple-system, sans-serif; font-size: 18px; }}
  .app {{ height: 100%; display: flex; }}
{sidebar_css}
  header a.wez-migrate {{ flex: 0 0 auto; padding: 7px 10px; font-size: .9rem;
    border: 1px solid #484f58; border-radius: 8px; }}
  /* WezTermセッションの閲覧は読み取り専用: 入力欄と操作系を出さない */
  body.readonly .controls {{ display: none; }}
  body.readonly .message.user .bubble {{ cursor: default; }}
  body.readonly .message.user .bubble:hover {{ border-color: #3d444d; }}
  header .ctx {{ font-size: .72rem; padding: 2px 7px; }}
  #artifacts {{ display: flex; flex-wrap: wrap; gap: 6px; padding: 8px 12px;
    border-bottom: 1px solid #30363d; background: #161b22; flex-shrink: 0; }}
  #artifacts:empty {{ display: none; }}
  #artifacts .art {{ font-size: .78rem; }}
  .message.user .bubble {{ cursor: pointer; }}
  .message.user .bubble:hover {{ border-color: #58a6ff; }}
  .bubble img.thumb {{ display: block; max-width: min(220px, 100%); max-height: 180px;
    margin: 6px 0; border: 1px solid #30363d; border-radius: 9px; cursor: zoom-in; }}
  .bubble a.file-chip {{ display: inline-block; max-width: 100%; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; margin: 6px 0; padding: 7px 12px;
    border: 1px solid #30363d; border-radius: 9px; background: #21262d;
    color: #e6edf3; text-decoration: none; font-size: .92em; }}
  .bubble a.file-chip:hover {{ border-color: #58a6ff; }}
  #lightbox {{ position: fixed; inset: 0; z-index: 200; display: grid; place-items: center;
    padding: 14px; background: #000d; cursor: zoom-out; }}
  #lightbox[hidden] {{ display: none; }}
  #lightbox img {{ max-width: 96vw; max-height: 94vh; border-radius: 8px; }}
  /* 一覧（/）へ戻るリンク。PCはサイドバー常設なので出さない */
  #back-link {{ display: none; flex: 0 0 auto; padding: 7px 10px;
    border: 1px solid #484f58; border-radius: 8px; }}
  .terminal {{ min-width: 0; flex: 1; display: flex; flex-direction: column; }}
  header {{ position: relative; padding: 10px 12px; display: flex; align-items: center; gap: 10px;
    border-bottom: 1px solid #30363d; background: #161b22; flex-shrink: 0; z-index: 2; }}
  header a {{ color: #8ab4f8; text-decoration: none; font-size: 1rem; }}
  header button {{ flex: 0 0 auto; padding: 7px 10px; }}
  /* 操作ボタン群: PCは従来どおり横並び（contentsで包みを消す）、SPはハンバーガーに畳む */
  header .actions {{ display: contents; }}
  #menu-toggle {{ display: none; }}
  header button.warn {{ border-color: #d63545; color: #ff9c9c; }}
  header button.note-button {{ color: #d29922; }}
  header div {{ min-width: 0; flex: 1; }}
  /* 幅が足りないときはツール名から削り、モデル名は最後まで残す。 */
  header strong {{ display: flex; align-items: center; min-width: 0; white-space: nowrap; }}
  header strong .name {{ overflow: hidden; text-overflow: ellipsis; }}
  header .model {{ flex: 0 0 auto; margin-left: 7px; padding: 2px 8px;
    border: 1px solid #30363d; border-radius: 999px; font-family: inherit;
    background: #1f6feb1f; color: #8ab4f8; font-size: .72rem; font-weight: 600; }}
  header button.model {{ cursor: pointer; }}
  header button.model:hover {{ border-color: #58a6ff; }}
  .model-choices {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 20px; }}
  .model-choices button {{ flex: 1 0 auto; padding: 12px 16px; border: 1px solid #3d444d;
    border-radius: 9px; background: #21262d; color: #e6edf3; cursor: pointer; }}
  .model-choices button:hover {{ border-color: #58a6ff; background: #1f6feb22; }}
  .model-choices button[disabled] {{ opacity: .5; cursor: default; }}
  header .icon {{ display: none; }}
  /* SPのハンバーガーメニュー内だけで使う説明的ラベル */
  header .menu-label {{ display: none; }}
  header img.icon {{ width: 19px; height: 19px; border-radius: 4px; vertical-align: -4px; }}
  header small {{ display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    color: #8b949e; margin-top: 2px; }}
  #screen {{ flex: 1; min-height: 0; margin: 0; padding: 12px; overflow: auto; white-space: pre-wrap;
    overflow-wrap: anywhere; font: 18px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace; }}
  #chat {{ flex: 1; min-height: 0; overflow-y: auto; padding: 22px max(16px, 6vw); }}
  .message {{ max-width: 860px; margin: 0 auto 22px; }}
  .message.user {{ display: flex; flex-direction: column; align-items: flex-end; }}
  .bubble {{ overflow-wrap: anywhere; line-height: 1.65; }}
  .message.user .bubble {{ max-width: min(82%, 720px); padding: 11px 15px;
    border: 1px solid #3d444d; border-radius: 18px 18px 5px 18px; background: #21262d; }}
  .message.assistant .bubble {{ padding: 4px 2px; }}
  .message.pending .bubble {{ opacity: .55; }}
  .bubble.collapsed {{ max-height: 320px; overflow: hidden; position: relative; }}
  .bubble.collapsed::after {{ content: ""; position: absolute; inset: auto 0 0 0; height: 72px;
    background: linear-gradient(transparent, #0d1117); pointer-events: none; }}
  .message.user .bubble.collapsed::after {{ background: linear-gradient(transparent, #21262d); }}
  .expand-toggle {{ flex: none; width: auto; align-self: flex-end; margin-top: 7px;
    padding: 4px 13px; font-size: .8rem; color: #8b949e; background: #161b22;
    border: 1px solid #3d444d; border-radius: 999px; cursor: pointer; }}
  .message.assistant .expand-toggle {{ display: inline-block; margin-left: 2px; }}
  .expand-toggle:hover {{ color: #e6edf3; border-color: #58a6ff; }}
  .message.activity {{ display: flex; align-items: center; gap: 9px; color: #d9884f;
    font-size: .92rem; }}
  .message.auth .bubble {{ padding: 14px 16px; border: 1px solid #d29922;
    border-radius: 10px; background: #d2992212; }}
  .message.question {{ padding: 14px; border: 1px solid #d29922; border-radius: 12px; }}
  .question-title {{ margin: 0 0 10px; font-weight: 700; }}
  .question .choice {{ display: block; width: 100%; margin: 6px 0; padding: 10px 13px;
    text-align: left; border: 1px solid #3d444d; border-radius: 9px; background: #21262d;
    color: #e6edf3; cursor: pointer; }}
  .question .choice:hover {{ border-color: #d29922; background: #2a2f37; }}
  .question .choice small {{ display: block; margin-top: 3px; color: #8b949e; }}
  .message.activity::before {{ content: "✻"; animation: activity-pulse 1.3s ease-in-out infinite; }}
  .bubble p {{ margin: 0 0 12px; white-space: pre-wrap; }}
  .bubble p:last-child {{ margin-bottom: 0; }}
  .bubble h1, .bubble h2, .bubble h3 {{ margin: 20px 0 10px; line-height: 1.35; }}
  .bubble h1:first-child, .bubble h2:first-child, .bubble h3:first-child {{ margin-top: 0; }}
  .bubble h1 {{ font-size: 1.45rem; }} .bubble h2 {{ font-size: 1.25rem; }}
  .bubble h3 {{ font-size: 1.1rem; }}
  .bubble ul, .bubble ol {{ margin: 8px 0 14px; padding-left: 1.6em; }}
  .bubble li {{ margin: 4px 0; }}
  .bubble li > ul, .bubble li > ol {{ margin: 4px 0; padding-left: 1.5em; }}
  .bubble code {{ padding: 2px 5px; border-radius: 5px; background: #6e768133;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .9em; }}
  .bubble .inline-run {{ display: inline-block; width: auto; flex: none; margin-left: 6px;
    padding: 2px 8px; border: 1px solid #3d444d; border-radius: 6px; background: #21262d;
    color: #adbac7; font-size: .75rem; vertical-align: 1px; cursor: pointer; }}
  .bubble .inline-run:hover {{ background: #30363d; border-color: #6e7681; color: #e6edf3; }}
  .bubble pre {{ margin: 12px 0; padding: 13px; overflow-x: auto; border: 1px solid #30363d;
    border-radius: 9px; background: #010409; white-space: pre; }}
  .bubble pre code {{ padding: 0; background: none; font-size: .88rem; }}
  .bubble .code-run {{ position: relative; }}
  .bubble .code-run pre {{ padding-right: 150px; }}
  .bubble .code-actions {{ position: absolute; top: 21px; right: 9px; display: flex; gap: 6px; }}
  .bubble .code-run .run {{ padding: 4px 10px;
    font-size: .78rem; border-radius: 7px; border: 1px solid #3d444d; background: #21262d;
    color: #adbac7; cursor: pointer; }}
  .bubble .code-run .run:hover {{ background: #30363d; border-color: #6e7681; color: #e6edf3; }}
  .bubble blockquote {{ margin: 10px 0; padding: 2px 0 2px 12px; color: #8b949e;
    border-left: 3px solid #484f58; }}
  .bubble .table-wrap {{ margin: 12px 0; overflow-x: auto; }}
  .bubble table {{ width: 100%; border-collapse: collapse; font-size: .94rem; }}
  .bubble th, .bubble td {{ padding: 8px 10px; border: 1px solid #30363d; text-align: left; }}
  .bubble th {{ background: #161b22; font-weight: 700; }}
  .bubble tr:nth-child(even) td {{ background: #161b2266; }}
  .bubble hr {{ margin: 18px 0; border: 0; border-top: 1px solid #30363d; }}
  .bubble strong {{ font-weight: 700; }}
  .bubble a {{ color: #58a6ff; text-decoration: underline; text-underline-offset: 3px; }}
  .chat-empty {{ color: #8b949e; text-align: center; margin-top: 20vh; }}
  .controls {{ flex-shrink: 0; padding: 8px max(8px, env(safe-area-inset-right))
    max(8px, env(safe-area-inset-bottom)) max(8px, env(safe-area-inset-left));
    border-top: 1px solid #30363d; background: #161b22; }}
  textarea {{ width: 100%; min-height: 80px; resize: vertical; padding: 12px; color: #e6edf3;
    background: #0d1117; border: 1px solid #484f58; border-radius: 8px;
    font-family: inherit; font-size: 17px; line-height: 1.5; }}
  .buttons {{ display: flex; gap: 6px; margin-top: 7px; }}
  button {{ flex: 1; padding: 10px 6px; color: #e6edf3; background: #21262d;
    border: 1px solid #484f58; border-radius: 8px; font-size: 1rem; }}
  button.primary {{ background: #238636; border-color: #2ea043; font-weight: 600; }}
  button.danger {{ color: #ff7b72; }}
  #status {{ min-height: 20px; color: #8b949e; font-size: .9rem; margin-top: 6px; }}
  .modal {{ position: fixed; inset: 0; z-index: 100; display: grid; place-items: center; padding: 20px; background: #000b; }}
  .modal[hidden] {{ display: none; }}
  .modal-card {{ width: min(100%, 440px); padding: 22px; border: 1px solid #484f58; border-radius: 14px; background: #161b22; }}
  .modal-card p {{ margin: 0 0 20px; font-size: 1.1rem; line-height: 1.5; }}
  .modal-card textarea {{ min-height: 120px; margin-bottom: 12px; }}
  .modal-actions {{ display: flex; gap: 10px; }}
  .modal-actions button {{ padding: 13px; }}
  @media (max-width: 799px) {{
    /* SPはサイドバーを出さず、ヘッダーの ← で一覧（/）へ戻る。 */
    aside {{ display: none; }}
    #back-link {{ display: block; }}
    .terminal {{ width: 100%; }}
    /* 狭い画面ではボタンを記号だけにして、ツール名とモデルを読める幅を残す。 */
    header {{ gap: 7px; padding: 9px 10px; }}
    header a {{ font-size: .95rem; }}
    header button {{ padding: 7px 11px; font-size: 1.05rem; }}
    header .label {{ display: none; }}
    header .icon {{ display: inline; }}
    header strong {{ font-size: .98rem; }}
    header small {{ font-size: .8rem; }}
    /* 低頻度の操作（再起動・handoff・バイパス・WezTermへ・メモ）はハンバーガーに畳み、
       頻繁に使うターミナル切替だけヘッダーに残す。誤タップ防止も兼ねる。 */
    #menu-toggle {{ display: block; }}
    header .actions {{ display: none; }}
    header .actions.open {{ display: flex; flex-direction: column; align-items: stretch; gap: 8px;
      position: absolute; top: calc(100% + 6px); right: 10px; z-index: 60;
      min-width: 190px; padding: 10px; background: #161b22;
      border: 1px solid #484f58; border-radius: 12px; box-shadow: 0 10px 28px #000c; }}
    header .actions.open .menu-label {{ display: inline; }}
    header .actions.open .icon {{ display: none; }}
    header .actions.open button, header .actions.open a {{ text-align: left; padding: 11px 14px;
      font-size: .95rem; }}
  }}
</style></head><body{body_class}>
<div class="app"><aside><h2>セッション</h2>{sessions_sidebar}</aside><main class="terminal">
<header><a id="back-link" href="/">←<span class="label"> 一覧</span></a><div><strong>{tool_html}{model_badge}{context_badge}</strong>
<small title="{cwd_full}">{cwd}</small></div><div class="actions" id="header-actions">{pin_button}{restart_button}{note_button}</div>
<button type="button" id="history"><span class="label">ターミナル</span><span class="icon">▤</span></button>
<button type="button" id="menu-toggle" aria-label="メニュー">☰</button></header>
<div id="artifacts">{artifacts_html}</div>
<div id="chat"><div class="chat-empty">会話を読み込み中...</div></div>
<pre id="screen" hidden>接続中...</pre>
<div class="controls">
  <textarea id="input" placeholder="メッセージを入力（! でコマンド実行、画像ペースト・ファイルD&amp;D可）"></textarea>
  <div class="buttons">
    <button type="button" data-key="Escape">Esc</button>
    <button type="button" data-key="C-c">Ctrl+C</button>
    <button type="button" id="enter">Enter</button>
    <button type="button" class="primary" id="send">送信</button>
    <button type="button" class="danger" id="kill">終了</button>
  </div>
  <div id="status"></div>
</div>
</main></div>
<div id="lightbox" hidden><img alt="添付画像"></div>
<div class="modal" id="confirm-modal" hidden><div class="modal-card" role="dialog" aria-modal="true">
  <p id="confirm-message">このセッションを終了しますか？</p><div class="modal-actions"><button type="button" id="confirm-cancel">キャンセル</button><button type="button" class="danger" id="confirm-ok">実行する</button></div>
</div></div>
<div class="modal" id="model-modal" hidden><div class="modal-card" role="dialog" aria-modal="true">
  <p>モデルを変更する</p>
  <div class="model-choices">{model_choices}</div>
  <div class="modal-actions"><button type="button" id="model-cancel">キャンセル</button></div>
</div></div>
<div class="modal" id="note-modal" hidden><div class="modal-card" role="dialog" aria-modal="true">
  <p>セッションメモ</p>
  <textarea id="note-input" maxlength="1000" placeholder="このセッションに関する任意のメモ"></textarea>
  <div class="modal-actions"><button type="button" id="note-cancel">キャンセル</button><button type="button" class="primary" id="note-save">保存</button></div>
</div></div>
<script>
  const session = {session_json};
  const bootId = {boot_json};
  let sessionNote = {note_json};
  let sessionPinned = {pinned_json};
  // 添付画像の拡大表示
  const lightbox = document.getElementById("lightbox");
  function showLightbox(src) {{
    lightbox.querySelector("img").src = src;
    lightbox.hidden = false;
  }}
  lightbox.addEventListener("click", () => {{ lightbox.hidden = true; }});
{sidebar_js}
  const screen = document.getElementById("screen");
  const chat = document.getElementById("chat");
  const input = document.getElementById("input");
  const status = document.getElementById("status");
  const noteButton = document.getElementById("note");
  const noteModal = document.getElementById("note-modal");
  const noteInput = document.getElementById("note-input");
  const pinButton = document.getElementById("pin");
  function renderPinButton() {{
    if (!pinButton) return;
    pinButton.classList.toggle("pinned", sessionPinned);
    pinButton.title = sessionPinned ? "ピン留めを解除" : "ピン留め";
    pinButton.querySelector(".label").textContent = sessionPinned ? "ピン解除" : "ピン留め";
    pinButton.querySelector(".icon").textContent = sessionPinned ? "📌" : "📍";
    pinButton.querySelector(".menu-label").textContent = sessionPinned ? "📌 ピン留めを解除" : "📍 ピン留め";
  }}
  renderPinButton();
  if (pinButton) pinButton.addEventListener("click", async () => {{
    try {{
      const data = await post("/api/sessions/" + encodeURIComponent(session) + "/pin",
        {{pinned: sessionPinned ? "0" : "1"}});
      sessionPinned = !!data.pinned;
      renderPinButton();
      await refreshSidebar();
    }} catch (error) {{ status.textContent = error.message; }}
  }});
  if (noteButton) noteButton.addEventListener("click", () => {{
    noteInput.value = sessionNote;
    noteModal.hidden = false;
    noteInput.focus();
  }});
  document.getElementById("note-cancel").addEventListener("click", () => {{ noteModal.hidden = true; }});
  document.getElementById("note-save").addEventListener("click", async () => {{
    const save = document.getElementById("note-save");
    save.disabled = true;
    try {{
      const data = await post("/api/sessions/" + encodeURIComponent(session) + "/note", {{note: noteInput.value}});
      sessionNote = data.note || "";
      noteButton.title = sessionNote || "メモを追加";
      noteModal.hidden = true;
      await refreshSidebar();
    }} catch (error) {{ status.textContent = error.message; }}
    finally {{ save.disabled = false; }}
  }});
  // 下書きはセッション単位で localStorage に保存し、BOOT_ID 食い違いの
  // 自動リロードやブラウザ再読み込みでも入力途中の文面が消えないようにする
  const draftKey = "draft:" + session;
  // PC は入力量に応じて最大5行まで入力欄を自動で伸ばす（超過分は内部スクロール）。
  // タッチ端末はソフトウェアキーボードで画面が狭くなるため固定高のまま
  const autoGrowEnabled = !matchMedia("(pointer: coarse)").matches;
  if (autoGrowEnabled) input.style.resize = "none";
  function autoGrow() {{
    if (!autoGrowEnabled) return;
    const style = getComputedStyle(input);
    const border = parseFloat(style.borderTopWidth) + parseFloat(style.borderBottomWidth);
    const maxHeight = parseFloat(style.lineHeight) * 5
      + parseFloat(style.paddingTop) + parseFloat(style.paddingBottom) + border;
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight + border, maxHeight) + "px";
  }}
  // input.value を触ったら必ず呼ぶ（下書き保存と高さ調整をまとめて行う）
  function syncInput() {{
    if (input.value) localStorage.setItem(draftKey, input.value);
    else localStorage.removeItem(draftKey);
    autoGrow();
  }}
  if (!document.body.classList.contains("readonly") && localStorage.getItem(draftKey)) {{
    input.value = localStorage.getItem(draftKey);
    autoGrow();
  }}
  input.addEventListener("input", syncInput);
  let lastOutput = "";
  let lastMessages = "";
  let followOutput = true;
  let followChat = true;
  let showingHistory = true;
  let statusMessageUntil = 0;
  let serverMessages = [];
  let serverActivity = "";
  let serverQueued = [];
  let serverQuestion = null;
  let serverAuth = "";
  // 送信直後はまだログに書かれていないので、確定するまで自前で吹き出しを出す。
  let pendingMessages = [];
  // 長文をユーザーが展開したら、再描画後も開いたままにするためのキー集合
  const expandedBubbles = new Set();
  let pendingTimer = null;
  function scrollToLatest() {{
    requestAnimationFrame(() => requestAnimationFrame(() => {{
      screen.scrollTop = screen.scrollHeight;
      followOutput = true;
    }}));
  }}
  screen.addEventListener("scroll", () => {{
    const distanceFromBottom = screen.scrollHeight - screen.scrollTop - screen.clientHeight;
    followOutput = distanceFromBottom < 80;
  }});
  chat.addEventListener("scroll", () => {{
    const distanceFromBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight;
    followChat = distanceFromBottom < 100;
  }});
  function appendInlineMarkdown(target, text) {{
    // 素の URL は RFC 3986 の ASCII 文字だけで止める。直後に続く全角の
    // 「」や句読点、地の文をリンクへ巻き込まないため。全角を含む URL は
    // [表示名](URL) 形式なら丸ごとリンクにできる。
    const tokenPattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\(https?:\/\/[^)\s]+\)|https?:\/\/[A-Za-z0-9\-._~:/?#\[\]@!$&()*+,;=%]+)/g;
    let cursor = 0;
    for (const match of text.matchAll(tokenPattern)) {{
      if (match.index > cursor) target.append(document.createTextNode(text.slice(cursor, match.index)));
      const token = match[0];
      if (token.startsWith("`")) {{
        const value = token.slice(1, -1);
        const code = document.createElement("code"); code.textContent = value; target.append(code);
        // 文章中でも `! command` / `$ command` と明示されたものは、その場で実行できる。
        if (/^[!$]\s+\S/.test(value) && !document.body.classList.contains("readonly")) {{
          const run = document.createElement("button");
          run.type = "button"; run.className = "inline-run"; run.textContent = "▶ 実行";
          run.addEventListener("click", event => {{
            event.stopPropagation(); runCommand(value.replace(/^[!$]\s+/, ""));
          }});
          target.append(run);
        }}
      }} else if (token.startsWith("**")) {{
        // 太字の中の URL やインラインコードもリンク化・装飾したいので再帰する
        const strong = document.createElement("strong"); appendInlineMarkdown(strong, token.slice(2, -2)); target.append(strong);
      }} else if (token.startsWith("[")) {{
        const parts = token.match(/^\[([^\]]+)\]\((https?:\/\/[^)]+)\)$/);
        const link = document.createElement("a"); link.href = parts[2]; link.textContent = parts[1];
        link.target = "_blank"; link.rel = "noopener noreferrer"; target.append(link);
      }} else {{
        let url = token, suffix = "";
        while (/[\\),.;:!?、。】）]$/.test(url)) {{ suffix = url.slice(-1) + suffix; url = url.slice(0, -1); }}
        const link = document.createElement("a"); link.href = url; link.textContent = url;
        link.target = "_blank"; link.rel = "noopener noreferrer"; target.append(link);
        if (suffix) target.append(document.createTextNode(suffix));
      }}
      cursor = match.index + token.length;
    }}
    if (cursor < text.length) target.append(document.createTextNode(text.slice(cursor)));
  }}
  function shellCommand(language, lines) {{
    // 実行できるのは TUI の bash モードに渡せる1行だけ。出力の貼り付けを
    // 誤って実行しないよう、シェルだと明示されたブロックか、! / $ で
    // コマンドだと分かる行に限る。
    const body = lines.filter(line => line.trim());
    if (body.length !== 1) return "";
    const line = body[0].trim();
    if (/^[!$]\s/.test(line)) return line.replace(/^[!$]\s+/, "");
    if (!/^(sh|bash|zsh|shell)$/i.test(language || "")) return "";
    return line;
  }}
  function copyText(text, button) {{
    // http 配信では navigator.clipboard が使えないので textarea 経由で書く
    const scratch = document.createElement("textarea");
    scratch.value = text; scratch.style.position = "fixed"; scratch.style.opacity = "0";
    document.body.append(scratch); scratch.focus(); scratch.select();
    let copied = false;
    try {{ copied = document.execCommand("copy"); }} catch (error) {{ copied = false; }}
    scratch.remove();
    button.textContent = copied ? "✓ コピー済" : "コピー失敗";
    setTimeout(() => {{ button.textContent = "コピー"; }}, 1600);
  }}
  function decorateCode(pre, language, lines) {{
    const wrap = document.createElement("div"); wrap.className = "code-run";
    const actions = document.createElement("div"); actions.className = "code-actions";
    const copy = document.createElement("button");
    copy.type = "button"; copy.className = "run"; copy.textContent = "コピー";
    copy.addEventListener("click", () => copyText(lines.join("\n"), copy));
    actions.append(copy);
    const command = shellCommand(language, lines);
    if (command && !document.body.classList.contains("readonly")) {{
      const run = document.createElement("button");
      run.type = "button"; run.className = "run"; run.textContent = "▶ 実行";
      run.addEventListener("click", () => runCommand(command));
      actions.append(run);
    }}
    wrap.append(pre, actions); return wrap;
  }}
  function renderMarkdown(target, text) {{
    const lines = text.replace(/\r\n/g, "\n").split("\n");
    let paragraph = [], listStack = [], codeLines = [], inCode = false, codeLanguage = "", codeIndent = 0,
      codeTarget = target;
    const flushParagraph = () => {{
      if (!paragraph.length) return;
      const p = document.createElement("p"); appendInlineMarkdown(p, paragraph.join("\n"));
      target.append(p); paragraph = [];
    }};
    // リストは作成時にDOMへ追加する。空行や別ブロックではスタックだけを閉じる。
    const flushList = () => {{ listStack = []; }};
    const appendListItem = (indent, tag, content, start = 1) => {{
      while (listStack.length && indent < listStack[listStack.length - 1].indent) listStack.pop();
      if (listStack.length && indent === listStack[listStack.length - 1].indent
          && tag !== listStack[listStack.length - 1].tag) listStack.pop();

      let level = listStack[listStack.length - 1];
      if (!level || indent > level.indent || tag !== level.tag) {{
        const list = document.createElement(tag.toLowerCase());
        // コードブロックや補足段落を挟んで ol が分割されても、Markdown に
        // 明記された `2.` などの番号を維持する。
        if (tag === "OL" && start !== 1) list.start = start;
        if (level && indent > level.indent && level.lastItem) level.lastItem.append(list);
        else target.append(list);
        level = {{indent, tag, list, lastItem: null}};
        listStack.push(level);
      }}
      const li = document.createElement("li");
      appendInlineMarkdown(li, content); level.list.append(li); level.lastItem = li;
      return li;
    }};
    const appendCodeBlock = () => {{
      const pre = document.createElement("pre"), code = document.createElement("code");
      if (codeLanguage) code.dataset.language = codeLanguage;
      code.textContent = codeLines.join("\n"); pre.append(code);
      codeTarget.append(decorateCode(pre, codeLanguage, codeLines));
      inCode = false; codeTarget = target;
    }};
    const tableCells = line => line.trim().replace(/^\|/, "").replace(/\|$/, "")
      .split("|").map(cell => cell.trim());
    for (let lineIndex = 0; lineIndex < lines.length; lineIndex++) {{
      const line = lines[lineIndex];
      // `2. ```sh` のように、リスト項目そのものから始まるコードフェンス。
      // 通常の numbered item として処理すると ``` が文字で表示され、閉じフェンスから
      // 後続の文章すべてがコードブロックになるため、code block を li の子にする。
      const listFence = !inCode && line.match(/^(\s*)(?:([-*])|(\d+)\.)\s+```(.*)$/);
      if (listFence) {{
        flushParagraph();
        const indent = listFence[1].replace(/\t/g, "    ").length;
        const li = appendListItem(
          indent, listFence[2] ? "UL" : "OL", "", listFence[3] ? Number(listFence[3]) : 1
        );
        inCode = true; codeLanguage = listFence[4].trim();
        codeIndent = indent + (listFence[2] ? 2 : listFence[3].length + 2);
        codeLines = []; codeTarget = li;
        continue;
      }}
      // リスト内などでインデントされたフェンスもコードブロックとして扱う
      const fence = line.match(/^(\s{{0,6}})```(.*)$/);
      if (fence) {{
        flushParagraph();
        if (!inCode) {{
          flushList(); inCode = true; codeLanguage = fence[2].trim();
          codeIndent = fence[1].length; codeLines = []; codeTarget = target;
        }} else appendCodeBlock();
        continue;
      }}
      if (inCode) {{
        codeLines.push(codeIndent ? line.replace(new RegExp("^\\s{{0," + codeIndent + "}}"), "") : line);
        continue;
      }}
      // アップロード画像への言及行はサムネイル表示にする（クリックで拡大）。
      // 送信時は「添付画像: <パス>」、TUI が読み込んだ後のログでは
      // 「[Image: source: <パス>]」の形になる。保存先の新旧パス両方を拾う。
      const imageLine = line.match(
        /^(?:添付画像[:：]|\[Image:(?:\s*source:)?)\s*((?:{upload_prefix_alt})\/uploads\/[^\s\]]+?)\]?$/);
      if (imageLine) {{
        flushParagraph(); flushList();
        const img = document.createElement("img");
        img.className = "thumb"; img.alt = "添付画像"; img.loading = "lazy";
        img.src = "/uploads/" + imageLine[1].split("/uploads/")[1];
        img.addEventListener("click", event => {{ event.stopPropagation(); showLightbox(img.src); }});
        target.append(img);
        continue;
      }}
      // アップロードファイルへの言及行はチップ表示にする（タップで内容表示/ダウンロード）
      const fileLine = line.match(
        /^添付ファイル[:：]\s*((?:{upload_prefix_alt})\/uploads\/[^\s\]]+)$/);
      if (fileLine) {{
        flushParagraph(); flushList();
        const rel = fileLine[1].split("/uploads/")[1];
        const chip = document.createElement("a");
        chip.className = "file-chip";
        chip.href = "/uploads/" + rel.split("/").map(encodeURIComponent).join("/");
        chip.target = "_blank"; chip.rel = "noopener";
        // 保存名の「日時-乱数-」プレフィックスを外して元のファイル名を出す
        chip.textContent = "📄 " + rel.split("/").pop().replace(/^(?:\d{{8}}-\d{{6}}-[0-9a-f]{{8}}|sent-[0-9a-f]{{8}})-/, "");
        chip.addEventListener("click", event => event.stopPropagation());
        target.append(chip);
        continue;
      }}
      const nextLine = lines[lineIndex + 1] || "";
      if (line.includes("|") && /^\s*\|?\s*:?-{{3,}}/.test(nextLine)
          && nextLine.includes("|")) {{
        flushParagraph(); flushList();
        const tableWrap = document.createElement("div"); tableWrap.className = "table-wrap";
        const table = document.createElement("table"), thead = document.createElement("thead");
        const headerRow = document.createElement("tr");
        for (const cell of tableCells(line)) {{
          const th = document.createElement("th"); appendInlineMarkdown(th, cell); headerRow.append(th);
        }}
        thead.append(headerRow); table.append(thead);
        const tbody = document.createElement("tbody"); lineIndex += 2;
        while (lineIndex < lines.length && lines[lineIndex].includes("|") && lines[lineIndex].trim()) {{
          const row = document.createElement("tr");
          for (const cell of tableCells(lines[lineIndex])) {{
            const td = document.createElement("td"); appendInlineMarkdown(td, cell); row.append(td);
          }}
          tbody.append(row); lineIndex++;
        }}
        lineIndex--; table.append(tbody); tableWrap.append(table); target.append(tableWrap); continue;
      }}
      const heading = line.match(/^(#{{1,3}})\s+(.+)$/);
      const bullet = line.match(/^(\s*)[-*]\s+(.+)$/);
      const numbered = line.match(/^(\s*)\d+\.\s+(.+)$/);
      if (heading) {{
        flushParagraph(); flushList(); const h = document.createElement("h" + heading[1].length);
        appendInlineMarkdown(h, heading[2]); target.append(h);
      }} else if (bullet || numbered) {{
        flushParagraph(); const match = bullet || numbered;
        const indent = match[1].replace(/\t/g, "    ").length;
        appendListItem(indent, bullet ? "UL" : "OL", match[2], numbered ? Number(line.match(/^\s*(\d+)\./)[1]) : 1);
      }} else if (/^\s*>\s?/.test(line)) {{
        flushParagraph(); flushList(); const quote = document.createElement("blockquote");
        appendInlineMarkdown(quote, line.replace(/^\s*>\s?/, "")); target.append(quote);
      }} else if (/^\s*(---+|\*\*\*+)\s*$/.test(line)) {{
        flushParagraph(); flushList(); target.append(document.createElement("hr"));
      }} else if (!line.trim()) {{
        flushParagraph(); flushList();
      }} else {{
        flushList(); paragraph.push(line);
      }}
    }}
    if (inCode) {{
      const pre = document.createElement("pre"), code = document.createElement("code");
      code.textContent = codeLines.join("\n"); pre.append(code); codeTarget.append(pre);
    }}
    flushParagraph(); flushList();
  }}
  function userCount(messages) {{ return messages.filter(item => item.role === "user").length; }}
  function pendingText(text) {{
    // ! 実行はログに残る形（$ コマンド）へ寄せ、確定時に見た目が変わらないようにする。
    return text.startsWith("!") ? "```sh\n$ " + text.slice(1).trim() + "\n```" : text;
  }}
  function messageMatchesPending(entry, pending) {{
    if (entry.role !== "user") return false;
    if (entry.text === pending) return true;
    // user_shell_command は完了後に結果ブロックが加わるため、送信時の !command と
    // 完全一致しない。先頭のコマンドブロックが一致すれば確定済みとして扱う。
    return pending.startsWith("!") && entry.text.startsWith(pendingText(pending) + "\n");
  }}
  function withPending(messages, queued) {{
    // 送信してからログに載るまでの数百ミリ秒だけ、こちらで吹き出しを出す。
    // 待ち行列に入ったか会話に現れたら、以降はログ側の情報に任せる。
    // スラッシュコマンドは展開されて文面が変わるので、発言数でも打ち切る。
    const count = userCount(messages), now = Date.now();
    pendingMessages = pendingMessages.filter(item =>
      !queued.includes(item.text)
      && !messages.some(entry => messageMatchesPending(entry, item.text))
      && count < item.threshold && now - item.since <= 60000
    );
    if (!pendingMessages.length) stopPendingTimer();
    const waiting = queued.concat(pendingMessages.map(item => item.text));
    if (!waiting.length) return messages;
    return messages.concat(waiting.map(
      text => ({{role: "user", text: pendingText(text), pending: true}})
    ));
  }}
  function stopPendingTimer() {{
    if (pendingTimer) {{ clearInterval(pendingTimer); pendingTimer = null; }}
  }}
  function renderMessages(messages, activity, question, auth) {{
    activity = activity || ""; question = question || null; auth = auth || "";
    const serialized = JSON.stringify([messages, activity, question, auth]);
    if (serialized === lastMessages) return;
    const firstLoad = !lastMessages;
    lastMessages = serialized; chat.replaceChildren();
    if (!messages.length && !activity) {{
      const empty = document.createElement("div"); empty.className = "chat-empty";
      empty.textContent = "まだ会話はありません"; chat.append(empty); return;
    }}
    const lastItem = messages[messages.length - 1];
    const collapsible = [];
    for (const [index, item] of messages.entries()) {{
      const row = document.createElement("div");
      row.className = "message " + item.role + (item.pending ? " pending" : "");
      const bubble = document.createElement("div"); bubble.className = "bubble";
      renderMarkdown(bubble, item.text);
      if (item.role === "user" && !document.body.classList.contains("readonly")) {{
        // Esc で止めたあとの編集用に、タップで文面を入力欄へ戻せるようにする
        bubble.title = "タップで入力欄にコピーして編集";
        bubble.addEventListener("click", event => {{
          if (event.target.closest("a, button, img")) return;
          input.value = item.text; syncInput(); input.focus();
          status.textContent = "前のメッセージを入力欄に入れました。編集して送信できます";
          statusMessageUntil = Date.now() + 5000;
        }});
      }}
      row.append(bubble); chat.append(row);
      // 最新の回答は読みに来た本文なので畳まない。それ以外の長文は折りたたみ候補
      if (!(item === lastItem && item.role === "assistant")) {{
        collapsible.push({{row, bubble, key: item.role + "|" + index + "|" + item.text.length}});
      }}
    }}
    // スキル展開や長い貼り付けで縦に伸びすぎないよう、高さ超過分だけ畳む
    //（先に全件測ってから畳み、reflow が往復しないようにする）
    const heights = collapsible.map(entry => entry.bubble.scrollHeight);
    collapsible.forEach((entry, i) => {{
      if (heights[i] <= 520) return;
      const toggle = document.createElement("button");
      toggle.type = "button"; toggle.className = "expand-toggle";
      const setLabel = collapsed => {{
        toggle.textContent = collapsed ? "▾ 全文を表示" : "▴ 折りたたむ";
      }};
      const collapsed = !expandedBubbles.has(entry.key);
      if (collapsed) entry.bubble.classList.add("collapsed");
      setLabel(collapsed);
      toggle.addEventListener("click", () => {{
        const nowCollapsed = entry.bubble.classList.toggle("collapsed");
        setLabel(nowCollapsed);
        if (nowCollapsed) {{
          expandedBubbles.delete(entry.key);
          entry.row.scrollIntoView({{block: "nearest"}});
        }} else {{
          expandedBubbles.add(entry.key);
        }}
      }});
      entry.row.append(toggle);
    }});
    if (auth) {{
      const row = document.createElement("div"); row.className = "message assistant auth";
      const bubble = document.createElement("div"); bubble.className = "bubble";
      renderMarkdown(bubble, auth); row.append(bubble); chat.append(row);
    }}
    if (activity) {{
      const row = document.createElement("div"); row.className = "message activity";
      row.textContent = activity; chat.append(row);
    }}
    if (question) {{
      const panel = document.createElement("div"); panel.className = "message question";
      const title = document.createElement("p"); title.className = "question-title";
      title.textContent = question.question; panel.append(title);
      for (const choice of question.choices) {{
        const button = document.createElement("button"); button.type = "button";
        button.className = "choice";
        const label = document.createElement("strong");
        label.textContent = choice.number + ". " + choice.label; button.append(label);
        if (choice.description) {{
          const detail = document.createElement("small");
          detail.textContent = choice.description; button.append(detail);
        }}
        button.addEventListener("click", () => answerQuestion(choice));
        panel.append(button);
      }}
      chat.append(panel);
    }}
    if (firstLoad || followChat) requestAnimationFrame(() => {{ chat.scrollTop = chat.scrollHeight; }});
  }}
  async function answerQuestion(choice) {{
    if (!await askConfirm("「" + choice.label + "」を選択しますか？")) return;
    try {{
      await post("/api/sessions/" + encodeURIComponent(session) + "/answer",
                 {{number: String(choice.number)}});
      loadChat();
    }} catch (error) {{ status.textContent = error.message; }}
  }}
  async function loadChat() {{
    try {{
      const response = await fetch("/api/sessions/" + encodeURIComponent(session) + "/transcript");
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "会話履歴を取得できませんでした");
      serverMessages = data.messages || [];
      if (data.boot && data.boot !== bootId) {{ location.reload(); return; }}
      serverActivity = data.activity || "";
      serverQueued = data.queued || [];
      serverQuestion = data.question || null;
      serverAuth = data.auth || "";
      if (modelBadge && data.model && data.model !== modelBadge.textContent) {{
        modelBadge.textContent = data.model;
      }}
      if (ctxBadge) {{
        if (data.context === null || data.context === undefined) {{
          ctxBadge.hidden = true;
        }} else {{
          ctxBadge.hidden = false;
          ctxBadge.textContent = data.context + "%";
          ctxBadge.classList.toggle("ctx-warn", data.context >= 70 && data.context < 90);
          ctxBadge.classList.toggle("ctx-high", data.context >= 90);
        }}
      }}
      renderArtifacts(data.artifacts || []);
      renderMessages(withPending(serverMessages, serverQueued), serverActivity, serverQuestion, serverAuth);
      if (Date.now() >= statusMessageUntil) status.textContent = "接続中";
    }} catch (error) {{
      status.textContent = error.message;
      if (!lastMessages) {{
        // 一度も描画できていないと「会話を読み込み中...」が残り続けるので、
        // エラー本文に置き換える。復旧すれば次のポーリングで通常描画に戻る。
        const failed = document.createElement("div");
        failed.className = "chat-empty";
        failed.textContent = error.message;
        chat.replaceChildren(failed);
      }}
    }}
  }}
  // セッション中に作成したPR・issueのリンクバー（ヘッダー直下）
  const artifactsBar = document.getElementById("artifacts");
  function renderArtifacts(items) {{
    if (!artifactsBar) return;
    const key = JSON.stringify(items);
    if (key === artifactsBar.dataset.key) return;
    artifactsBar.dataset.key = key;
    artifactsBar.replaceChildren(...items.map(item => {{
      const link = document.createElement("a");
      link.href = item.url;
      link.target = "_blank";
      link.rel = "noopener";
      link.className = "art art-" + item.kind + (item.state ? " art-" + item.state : "");
      link.textContent = item.repo + "#" + item.number;
      return link;
    }}));
  }}
  const modelBadge = document.querySelector("header .model");
  const ctxBadge = document.getElementById("ctx");
  const modelModal = document.getElementById("model-modal");
  const modelButton = document.getElementById("model");
  if (modelButton) {{
    const closeModels = () => {{ modelModal.hidden = true; }};
    modelButton.addEventListener("click", () => {{ modelModal.hidden = false; }});
    document.getElementById("model-cancel").addEventListener("click", closeModels);
    modelModal.addEventListener("click", event => {{ if (event.target === modelModal) closeModels(); }});
    document.querySelectorAll(".model-choice").forEach(choice =>
      choice.addEventListener("click", async () => {{
        closeModels();
        if (!await askConfirm("実行中の処理を終了し、モデルを " + choice.dataset.model +
                              " に変えて同じ会話をresumeしますか？")) return;
        status.textContent = "モデルを変更して再起動中...";
        statusMessageUntil = Date.now() + 5000;
        try {{
          const data = await post("/api/sessions/" + encodeURIComponent(session) + "/model",
                                  {{model: choice.dataset.model}});
          location.href = "/terminal?session=" + encodeURIComponent(data.session);
        }} catch (error) {{
          status.textContent = error.message;
          statusMessageUntil = Date.now() + 5000;
        }}
      }})
    );
  }}
  function askConfirm(message) {{
    return new Promise(resolve => {{
      const modal = document.getElementById("confirm-modal"); modal.hidden = false;
      document.getElementById("confirm-message").textContent = message;
      const finish = value => {{ modal.hidden = true; resolve(value); }};
      document.getElementById("confirm-ok").onclick = () => finish(true);
      document.getElementById("confirm-cancel").onclick = () => finish(false);
    }});
  }}
  async function post(path, params) {{
    const response = await fetch(path, {{method: "POST", headers: {{"Content-Type": "application/x-www-form-urlencoded"}}, body: new URLSearchParams(params)}});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "操作に失敗しました");
    return data;
  }}
  async function uploadImage(file) {{
    if (file.size > 15 * 1024 * 1024) throw new Error("画像は15MBまでです");
    status.textContent = "画像をアップロード中...";
    const response = await fetch(
      "/api/sessions/" + encodeURIComponent(session) + "/image",
      {{method: "POST", headers: {{"Content-Type": file.type || "application/octet-stream"}}, body: file}}
    );
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "画像のアップロードに失敗しました");
    const prefix = input.value && !input.value.endsWith("\n") ? "\n" : "";
    input.value += prefix + "添付画像: " + data.path + "\n";
    syncInput(); input.focus(); status.textContent = "画像を添付しました";
  }}
  async function uploadFile(file) {{
    // 画像は既存のサムネイル表示に乗せる。それ以外はパスに変換して入力欄へ入れる
    if ((file.type || "").startsWith("image/")) return uploadImage(file);
    if (file.size > 15 * 1024 * 1024) throw new Error("ファイルは15MBまでです");
    status.textContent = "ファイルをアップロード中...";
    const response = await fetch(
      "/api/sessions/" + encodeURIComponent(session) + "/file",
      {{method: "POST", headers: {{
        "Content-Type": file.type || "application/octet-stream",
        "X-Filename": encodeURIComponent(file.name || ""),
      }}, body: file}}
    );
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "ファイルのアップロードに失敗しました");
    const prefix = input.value && !input.value.endsWith("\n") ? "\n" : "";
    input.value += prefix + "添付ファイル: " + data.path + "\n";
    syncInput(); input.focus(); status.textContent = "ファイルを添付しました";
  }}
  document.addEventListener("dragover", event => {{
    if (event.dataTransfer && Array.from(event.dataTransfer.types).includes("Files")) event.preventDefault();
  }});
  document.addEventListener("drop", async event => {{
    const files = Array.from(event.dataTransfer?.files || []);
    if (!files.length) return;
    event.preventDefault();
    if (document.body.classList.contains("readonly")) return;
    try {{ for (const file of files) await uploadFile(file); }}
    catch (error) {{ status.textContent = error.message; }}
  }});
  async function refresh() {{
    if (showingHistory) return;
    try {{
      const response = await fetch("/api/sessions/" + encodeURIComponent(session) + "/screen");
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "取得に失敗しました");
      if (data.output !== lastOutput) {{
        const firstLoad = !lastOutput;
        lastOutput = data.output; screen.textContent = data.output;
        if (firstLoad || followOutput) scrollToLatest();
      }}
      if (Date.now() >= statusMessageUntil) status.textContent = "接続中";
    }} catch (error) {{ status.textContent = error.message; }}
  }}
  const menuToggle = document.getElementById("menu-toggle");
  const headerActions = document.getElementById("header-actions");
  menuToggle.addEventListener("click", () => headerActions.classList.toggle("open"));
  // メニュー外タップで閉じる。メニュー内ボタンのクリックもバブリングで届くので実行後に自動で閉じる
  document.addEventListener("click", event => {{
    if (menuToggle.contains(event.target)) return;
    headerActions.classList.remove("open");
  }});
  document.getElementById("history").addEventListener("click", async event => {{
    const historyButton = event.currentTarget;
    if (showingHistory) {{
      showingHistory = false; historyButton.textContent = "チャット";
      chat.hidden = true; screen.hidden = false;
      lastOutput = ""; await refresh(); return;
    }}
    showingHistory = true; historyButton.textContent = "ターミナル";
    screen.hidden = true; chat.hidden = false; await loadChat();
  }});
  async function sendText(sent) {{
    followOutput = true;
    const result = await post("/api/sessions/" + encodeURIComponent(session) + "/input", {{text: sent, enter: "1"}});
    {{
      if (showingHistory) {{
        followChat = true;
        // 送信した吹き出しをその場で出し、ログに現れるまで短い間隔で見に行く。
        pendingMessages.push({{
          text: sent, since: Date.now(),
          // 先に積んだ分が確定したあと、自分の番でログの発言数がここに届く。
          threshold: userCount(serverMessages) + pendingMessages.length + 1,
        }});
        renderMessages(withPending(serverMessages, serverQueued), serverActivity, serverQuestion);
        // 反映されるまでの数秒だけ細かく見に行き、あとは通常のポーリングに任せる。
        const boostUntil = Date.now() + 20000;
        stopPendingTimer();
        pendingTimer = setInterval(() => {{
          if (!pendingMessages.length || Date.now() > boostUntil) {{
            stopPendingTimer(); return;
          }}
          loadChat();
        }}, 300);
        loadChat();
      }}
      else {{ await refresh(); scrollToLatest(); }}
      if (result.message) {{
        status.textContent = result.message;
        statusMessageUntil = Date.now() + 5000;
      }}
    }}
  }}
  document.getElementById("send").addEventListener("click", async () => {{
    if (!input.value) return;
    const sent = input.value; input.value = ""; syncInput();
    try {{ await sendText(sent); }}
    catch (error) {{ input.value = sent; syncInput(); status.textContent = error.message; }}
  }});
  async function runCommand(command) {{
    if (!await askConfirm("このコマンドを実行しますか？　" + command)) return;
    try {{ await sendText("!" + command); }}
    catch (error) {{ status.textContent = error.message; }}
  }}
  const enterButton = document.getElementById("enter");
  // pointerdown の preventDefault でテキストボックスのフォーカスを奪わない。
  // 入力中に押されたら TUI へ送らず改行を挿入する（スマホには Shift+Enter がないため）
  enterButton.addEventListener("pointerdown", event => event.preventDefault());
  enterButton.addEventListener("click", () => {{
    if (document.activeElement === input) {{
      input.setRangeText("\n", input.selectionStart, input.selectionEnd, "end");
      syncInput();
      return;
    }}
    post("/api/sessions/" + encodeURIComponent(session) + "/key", {{key: "Enter"}}).then(refresh).catch(e => status.textContent = e.message);
  }});
  document.querySelectorAll("[data-key]").forEach(button => button.addEventListener("click", () => post("/api/sessions/" + encodeURIComponent(session) + "/key", {{key: button.dataset.key}}).then(refresh).catch(e => status.textContent = e.message)));
  document.getElementById("kill").addEventListener("click", async () => {{
    if (!await askConfirm("このセッションを終了しますか？")) return;
    try {{ await post("/api/sessions/" + encodeURIComponent(session) + "/kill", {{}}); location.href = "/"; }}
    catch (error) {{ status.textContent = error.message; }}
  }});
  document.querySelectorAll("[data-restart]").forEach(button => button.addEventListener("click", async () => {{
    const bypass = button.dataset.restart === "bypass";
    if (!await askConfirm(bypass
      ? "実行中の処理を終了し、権限バイパスで同じ会話をresumeしますか？"
      : "実行中の処理を終了し、同じ会話をresumeしますか？")) return;
    status.textContent = "セッションを再起動中...";
    try {{
      const data = await post(
        "/api/sessions/" + encodeURIComponent(session) + "/restart",
        bypass ? {{bypass: "1"}} : {{}}
      );
      location.href = "/terminal?session=" + encodeURIComponent(data.session);
    }} catch (error) {{ status.textContent = error.message; }}
  }}));
  const handoff = document.getElementById("handoff");
  if (handoff) handoff.addEventListener("click", async () => {{
    const target = handoff.dataset.target;
    if (!await askConfirm("この会話と作業状況を " + target + " に引き継ぎますか？")) return;
    status.textContent = target + " へ引き継ぎ中...";
    try {{
      const data = await post("/api/sessions/" + encodeURIComponent(session) + "/handoff", {{}});
      location.href = "/terminal?session=" + encodeURIComponent(data.session);
    }} catch (error) {{ status.textContent = error.message; }}
  }});
  const toTerminal = document.getElementById("to-terminal");
  if (toTerminal) toTerminal.addEventListener("click", async () => {{
    if (!await askConfirm("実行中の処理を終了し、WezTermタブで同じ会話をresumeしますか？")) return;
    status.textContent = "WezTermタブへ移行中...";
    try {{
      const data = await post("/api/sessions/" + encodeURIComponent(session) + "/terminal", {{}});
      location.href = data.pane ? "/terminal?session=wez-" + data.pane : "/";
    }} catch (error) {{ status.textContent = error.message; }}
  }});
  input.addEventListener("keydown", event => {{
    if (event.key === "Escape" && !event.isComposing) {{
      event.preventDefault();
      post("/api/sessions/" + encodeURIComponent(session) + "/key", {{key: "Escape"}})
        .then(refresh).catch(e => status.textContent = e.message);
      return;
    }}
    // タッチ端末はソフトウェアキーボードにShiftがないため、Enterは改行のまま
    // 残し、送信は送信ボタンのみとする
    if (matchMedia("(pointer: coarse)").matches) return;
    if (!event.shiftKey && event.key === "Enter" && !event.isComposing) {{
      event.preventDefault(); document.getElementById("send").click();
    }}
  }});
  input.addEventListener("paste", async event => {{
    const images = Array.from(event.clipboardData?.items || [])
      .filter(item => item.kind === "file" && item.type.startsWith("image/"))
      .map(item => item.getAsFile()).filter(Boolean);
    if (!images.length) return;
    event.preventDefault();
    try {{ for (const image of images) await uploadImage(image); }}
    catch (error) {{ status.textContent = error.message; }}
  }});
  loadChat(); setInterval(() => showingHistory ? loadChat() : refresh(), 1000);
  if (!document.body.classList.contains("readonly")) input.focus();
</script></body></html>"""


MIGRATE_PAGE = r"""<!doctype html>
<html lang="ja"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Web操作へ移行</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg?v={favicon_version}">
<link rel="apple-touch-icon" href="/favicon.svg?v={favicon_version}">
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, sans-serif; max-width: 620px; margin: 0 auto; padding: 20px; font-size: 18px; }}
  h1 {{ font-size: 1.65rem; }}
  a {{ color: #6c5ce7; }}
  .card {{ padding: 14px; margin: 14px 0; border: 1px solid #8884; border-radius: 10px; }}
  small {{ color: #888; overflow-wrap: anywhere; }}
  select {{ width: 100%; padding: 14px; margin: 12px 0; font-size: 1.05rem; border-radius: 8px; }}
  button {{ width: 100%; padding: 15px; font-size: 1.05rem; border: 1px solid #6c5ce7;
    border-radius: 8px; background: #6c5ce72a; cursor: pointer; }}
  .warn {{ padding: 12px; background: #f39c121a; border: 1px solid #f39c12; border-radius: 8px; }}
  .modal {{ position: fixed; inset: 0; z-index: 100; display: grid; place-items: center; padding: 20px; background: #000a; }}
  .modal[hidden] {{ display: none; }}
  .modal-card {{ width: min(100%, 440px); padding: 22px; border: 1px solid #8885; border-radius: 14px; background: Canvas; }}
  .modal-card p {{ margin: 0 0 20px; font-size: 1.1rem; line-height: 1.5; }}
  .modal-actions {{ display: flex; gap: 10px; }}
  .modal-actions button {{ flex: 1; }}
  .danger {{ color: #fff; background: #d63545; border-color: #d63545; }}
</style></head><body>
<p><a href="/">‹ 戻る</a></p>
<h1>Web操作へ移行</h1>
<div class="card"><strong>{title}</strong><br><small>{cwd}</small><br><small>{tool} / pane {pane_id}</small></div>
<p class="warn">移行すると現在のCLIを終了し、選択した会話をtmux内で再開します。実行中の処理と入力途中の文字は失われるため、応答待ちではない状態で実行してください。</p>
<form id="migrate-form" method="post" action="/migrate">
  <input type="hidden" name="pane_id" value="{pane_id}">
  <label for="session_id">再開する会話</label>
  <select id="session_id" name="session_id" required>{options}</select>
  <button type="submit">Web操作へ移行する</button>
</form>
<div class="modal" id="confirm-modal" hidden><div class="modal-card" role="dialog" aria-modal="true">
  <p>現在のCLIを終了してWeb操作へ移行しますか？</p>
  <div class="modal-actions"><button type="button" id="confirm-cancel">キャンセル</button><button type="button" class="danger" id="confirm-ok">移行する</button></div>
</div></div>
<script>
  const form = document.getElementById("migrate-form"); const modal = document.getElementById("confirm-modal");
  form.addEventListener("submit", event => {{ event.preventDefault(); modal.hidden = false; }});
  document.getElementById("confirm-cancel").addEventListener("click", () => {{ modal.hidden = true; }});
  document.getElementById("confirm-ok").addEventListener("click", () => {{ modal.hidden = true; form.submit(); }});
</script>
</body></html>"""


# 閲覧中ページの URL・タイトル・選択テキストを /new のプロンプト欄に送る。
# bookmarklet.js と同一内容。アドレスバー貼り付けでは javascript: が削られるため、
# /new ページからブックマークバーへドラッグして登録させる。
def bookmarklet_js(origin):
    """閲覧中ページを /new に送るブックマークレット。origin はリクエストの Host から決める。"""
    return (
        'javascript:(function(){var s=String(getSelection()||"").trim();'
        'var p="以下のページを確認して対応してください。\\n"+location.href'
        '+"\\nタイトル: "+document.title;'
        'if(s){p+="\\n\\n選択テキスト:\\n"+s.slice(0,6000);}'
        f'window.open("{origin}/new?prompt="'
        '+encodeURIComponent(p),"_blank");})();'
    )


# Chatwork 連携が有効なときだけ /new に差し込む受信箱パネル
CHATWORK_PANEL = """<div class="cw-head"><h2>Chatwork 受信箱</h2><button class="cw-refresh" id="cw-refresh" type="button">🔄 更新</button></div>
<h2>自分宛てメンション</h2>
<div id="cw-mentions" class="cw-loading">読み込み中...</div>
<h2>ルームから探す</h2>
<div id="cw-rooms" class="cw-loading">読み込み中...</div>
<div id="cw-room-messages"></div>"""


def render(message="", view="new", host=None):
    # view は旧・一覧ページ時代の名残。呼び出し側の互換のため残している。
    del view
    origin = f"http://{host}" if host else f"http://localhost:{PORT}"
    buttons = "\n".join(
        f'<form class="launch" method="post" action="/launch">'
        f'<input type="hidden" name="dir" value="{html.escape(path)}">'
        f'<button class="proj" type="submit">🚀 {html.escape(name)}</button></form>'
        for name, path in PINNED
    )
    options = "\n".join(
        f'<option value="{html.escape(path)}">{html.escape(name)}</option>'
        for name, path in list_other_projects()
    )
    def model_radios(tool):
        return "\n".join(
            f'<label><input type="radio" name="model-{tool}" value="{v}"'
            f'{" checked" if v == "default" else ""}><span>{label}</span></label>'
            for v, label in MODELS_BY_TOOL[tool]
        )
    resume_forms = [
        f'<form class="launch" method="post" action="/launch" data-resume="1">'
        f'<input type="hidden" name="dir" value="{html.escape(item["cwd"])}">'
        f'<input type="hidden" name="tool" value="{item["tool"]}">'
        f'<input type="hidden" name="resume" value="{item["id"]}">'
        f'<button class="proj" type="submit">'
        f'<span class="resume-summary">🕘 {html.escape(item["summary"])}</span>'
        f'<small>{html.escape(short_path(item["cwd"]))} · {item["tool"]}'
        f' · {item["label"]}</small></button></form>'
        for item in recent_conversations()
    ]
    # 最初の8件だけ見せて残りは折りたたみ、ページが縦に伸びすぎないようにする
    resume_items = "\n".join(resume_forms[:8]) \
        or '<div class="cw-empty">再開できる会話が見つかりません</div>'
    if len(resume_forms) > 8:
        more_label = f"▾ さらに{len(resume_forms) - 8}件表示"
        resume_items += (
            '<div class="resume-more" id="resume-more">'
            + "\n".join(resume_forms[8:]) + "</div>"
            f'<button class="cw-set" id="resume-toggle" type="button"'
            f' data-label="{more_label}">{more_label}</button>'
        )
    return PAGE.format(
        favicon_version=urllib.parse.quote(VERSION),
        message=message, buttons=buttons, options=options, resume_items=resume_items,
        models_claude=model_radios("claude"), models_codex=model_radios("codex"),
        bookmarklet=html.escape(bookmarklet_js(origin)),
        chatwork_panel=CHATWORK_PANEL if CW_ENABLED else "",
    )


class Handler(BaseHTTPRequestHandler):
    def _deny(self):
        self.send_response(403)
        self.end_headers()
        self.wfile.write(b"forbidden")

    def _page(self, body: str, status=200):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, body, status=200):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _icon(self):
        # 各ページから同じURLを参照させる。ico で要求されても SVG を返す。
        data = FAVICON_SVG.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml")
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _tool_icon(self, tool):
        data = TOOL_ICONS.get(tool)
        if not data:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def _chatwork_error(self, exc):
        if isinstance(exc, urllib.error.HTTPError):
            message = f"Chatwork API エラー (HTTP {exc.code})"
        elif isinstance(exc, FileNotFoundError):
            message = "Chatwork token が見つかりません"
        else:
            message = "Chatwork の取得に失敗しました"
        return self._json({"error": message}, 502)

    def do_GET(self):
        if not client_allowed(self.client_address[0]):
            return self._deny()
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in {"/favicon.svg", "/favicon.ico"}:
            return self._icon()
        icon_match = re.fullmatch(r"/tool-icon/([a-z]+)\.png", parsed.path)
        if icon_match:
            return self._tool_icon(icon_match.group(1))
        if parsed.path == "/new":
            return self._page(render(view="new", host=self.headers.get("Host")))
        if parsed.path in {"/", "/sessions"}:
            # デフォルトはセッション一覧。PCは右ペインで選択か新規起動を促し、
            # SPは一覧のみを全画面表示する。見せるものが何も無ければランチャーへ。
            has_sessions = bool(managed_sessions()) or any(
                wez_view_session(str(pane.get("pane_id")))
                for pane in wezterm_panes()
            )
            if not has_sessions:
                return self._redirect("/new")
            return self._page(LIST_PAGE.format(
                favicon_version=urllib.parse.quote(VERSION),
                sessions_sidebar=build_sidebar(None),
                boot_json=json.dumps(BOOT_ID),
                sidebar_css=SIDEBAR_CSS,
                sidebar_js=SIDEBAR_JS,
            ))
        if parsed.path == "/migrate":
            pane_id = urllib.parse.parse_qs(parsed.query).get("pane_id", [""])[0]
            if not pane_id.isdigit():
                return self._page(render('<div class="msg err">❌ 不正なpane IDです</div>'), 400)
            pane = pane_for_id(pane_id)
            agent = pane_agent(pane) if pane else None
            if not pane or not agent:
                return self._page(render('<div class="msg err">❌ 移行できるCLIが見つかりません</div>'), 404)
            cwd = urllib.parse.urlparse(pane.get("cwd", "")).path
            candidates = resume_candidates(agent["tool"], cwd, agent["explicit_id"])
            if not candidates:
                return self._page(render('<div class="msg err">❌ 再開できる会話が見つかりません</div>'), 404)
            options = "".join(
                f'<option value="{html.escape(item["id"])}">'
                f'{"現在の会話 · " if item["exact"] else ""}{html.escape(item["label"])} · '
                f'{html.escape(item["summary"] or item["id"])}</option>'
                for item in candidates
            )
            return self._page(MIGRATE_PAGE.format(
                favicon_version=urllib.parse.quote(VERSION),
                title=html.escape(pane.get("title") or "(無題)"), cwd=html.escape(cwd),
                tool=html.escape(agent["tool"]), pane_id=pane_id, options=options,
            ))
        if parsed.path == "/terminal":
            session = urllib.parse.parse_qs(parsed.query).get("session", [""])[0]
            # WezTermタブで動いているCLIは読み取り専用の擬似セッション
            # （wez-<pane_id>）として同じ2ペイン表示で閲覧できる。
            wez_match = re.fullmatch(r"wez-(\d+)", session or "")
            if wez_match:
                info = wez_view_session(wez_match.group(1))
                if not info:
                    return self._page(render('<div class="msg err">❌ セッションが見つかりません</div>'), 404)
                model = model_label(info["model"], info["tool"])
                return self._page(TERMINAL_PAGE.format(
                    favicon_version=urllib.parse.quote(VERSION),
                    title=html.escape(f'{info["tool"]} - WezTerm'),
                    tool_html=tool_label(info["tool"]),
                    cwd=html.escape(short_path(info["cwd"])), cwd_full=html.escape(info["cwd"]),
                    model_badge=f'<span class="model">{html.escape(model)}</span>' if model else "",
                    context_badge=context_badge_html(info.get("context")),
                    model_choices="",
                    session_json=json.dumps(session), boot_json=json.dumps(BOOT_ID),
                    note_json=json.dumps(""), note_button="", pinned_json="false", pin_button="",
                    sessions_sidebar=build_sidebar(session),
                    sidebar_css=SIDEBAR_CSS, sidebar_js=SIDEBAR_JS,
                    upload_prefix_alt=UPLOAD_PREFIX_ALT_JS,
                    restart_button=(
                        f'<a class="wez-migrate" href="/migrate?pane_id={info["pane_id"]}">'
                        f'Web操作へ移行</a>'
                    ),
                    body_class=' class="readonly"',
                    artifacts_html=artifact_links(
                        session_artifacts(info["log_path"], info["tool"])
                    ),
                ))
            if not valid_session(session):
                return self._page(render('<div class="msg err">❌ セッションが見つかりません</div>'), 404)
            item = next(item for item in managed_sessions() if item["name"] == session)
            model = model_label(item.get("model", ""), item["tool"])
            choices = switchable_models(item["tool"])
            if choices:
                badge = (
                    '<button type="button" class="model" id="model">'
                    f'{html.escape(model) or "モデル"}</button>'
                )
            else:
                badge = f'<span class="model">{html.escape(model)}</span>' if model else ""
            return self._page(TERMINAL_PAGE.format(
                favicon_version=urllib.parse.quote(VERSION),
                title=html.escape(f'{item["tool"]} - {item["name"]}'),
                tool_html=tool_label(item["tool"]),
                cwd=html.escape(short_path(item["cwd"])), cwd_full=html.escape(item["cwd"]),
                model_badge=badge,
                context_badge=context_badge_html(item.get("context")),
                model_choices="".join(
                    f'<button type="button" class="model-choice" data-model="{html.escape(value)}">'
                    f'{html.escape(label)}</button>'
                    for value, label in MODELS_BY_TOOL.get(item["tool"], [])
                    if value in choices
                ),
                session_json=json.dumps(session), boot_json=json.dumps(BOOT_ID),
                upload_prefix_alt=UPLOAD_PREFIX_ALT_JS,
                note_json=json.dumps(item.get("note", "")),
                pinned_json=json.dumps(bool(item.get("pinned"))),
                pin_button=(
                    '<button type="button" id="pin"><span class="label">ピン留め</span>'
                    '<span class="icon">📍</span><span class="menu-label">📍 ピン留め</span></button>'
                ),
                note_button=(
                    '<button type="button" class="note-button" id="note" title="'
                    f'{html.escape(item.get("note") or "メモを追加")}">'
                    '<span class="label">メモ</span><span class="icon">📝</span>'
                    '<span class="menu-label">📝 メモを編集</span></button>'
                ),
                sessions_sidebar=build_sidebar(session),
                sidebar_css=SIDEBAR_CSS, sidebar_js=SIDEBAR_JS,
                restart_button=(
                    (
                        '<button type="button" data-restart="keep">'
                        '<span class="label">再起動</span><span class="icon">↻</span>'
                        '<span class="menu-label">↻ セッションを再起動</span></button>'
                        if item["session_id"] else ""
                    )
                    +
                    f'<button type="button" id="handoff" data-target="'
                    f'{"Codex" if item["tool"] == "claude" else "Claude"}">'
                    f'<span class="label">→ {"Codex" if item["tool"] == "claude" else "Claude"}</span>'
                    '<span class="icon">⇄</span>'
                    f'<span class="menu-label">⇄ {"Codex" if item["tool"] == "claude" else "Claude"}'
                    'へ切り替え</span></button>'
                    + (
                        '<button type="button" class="warn" data-restart="bypass">'
                        '<span class="label">⚠️ バイパス</span><span class="icon">⚠️</span>'
                        '<span class="menu-label">⚠️ バイパスで再起動</span></button>'
                        '<button type="button" id="to-terminal">'
                        '<span class="label">WezTermへ</span>'
                        '<img class="icon" src="/tool-icon/wezterm.png" alt="WezTerm">'
                        '<span class="menu-label">WezTermターミナルへ移行</span></button>'
                        if item["session_id"] else ""
                    )
                ),
                body_class="",
                artifacts_html=artifact_links(item.get("artifacts", [])),
            ))
        if parsed.path == "/api/usage":
            return self._json(usage_data() or {"providers": []})
        if parsed.path == "/api/version":
            return self._json(latest_release())
        if parsed.path == "/api/sidebar":
            items = []
            for entry in managed_sessions():
                status_text, status_class = sidebar_status(entry)
                items.append({
                    "name": entry["name"], "tool": entry["tool"],
                    "tool_icon": (
                        f'/tool-icon/{entry["tool"]}.png'
                        if entry["tool"] in TOOL_ICONS else ""
                    ),
                    "dir": dir_label(entry["cwd"]),
                    "status": status_text, "status_class": status_class,
                    "context": entry.get("context"),
                    "summary": entry["summary"],
                    "last_message": entry["last_message"],
                    "note": entry.get("note", ""),
                    "pinned": bool(entry.get("pinned")),
                    "artifacts": entry.get("artifacts", []),
                })
            return self._json({"items": items, "boot": BOOT_ID})
        if parsed.path.startswith("/uploads/"):
            return self._upload_file(parsed.path)
        match = re.fullmatch(
            r"/api/sessions/(agent-[A-Za-z0-9_.-]+|wez-\d+)/(screen|transcript)", parsed.path
        )
        if match:
            session, view = match.groups()
            if session.startswith("wez-"):
                info = wez_view_session(session[4:])
                if not info:
                    return self._json({"error": "WezTermセッションが見つかりません"}, 404)
                try:
                    screen_text = wez_capture(info["pane_id"])
                    if view == "screen":
                        return self._json({"output": screen_text})
                    return self._json({
                        "messages": session_messages(info["log_path"], info["tool"]),
                        "queued": queued_inputs(info["log_path"]),
                        "question": None,
                        "boot": BOOT_ID,
                        "model": model_label(info["model"], info["tool"]),
                        "context": info.get("context"),
                        "activity": (
                            log_activity(info["log_path"], info["tool"])
                            if screen_is_running(screen_text, info["tool"]) else ""
                        ),
                        "output": session_transcript(info["log_path"], info["tool"]),
                        "artifacts": session_artifacts(info["log_path"], info["tool"]),
                    })
                except Exception as exc:
                    return self._json({"error": str(exc)}, 500)
            item = next(
                (entry for entry in managed_sessions() if entry["name"] == session), None
            )
            if not item:
                return self._json({"error": "セッションが見つかりません"}, 404)
            try:
                if view == "transcript":
                    if not item["log_path"]:
                        # 起動直後はJSONLがまだ無いが、MCP承認や信頼確認などの
                        # 起動時ダイアログはこの段階で出る。画面から拾った選択肢
                        # だけでも返し、チャット画面から回答できるようにする。
                        return self._json({
                            "messages": [],
                            "queued": [],
                            "question": pending_question(session, item["tool"]),
                            "auth": pending_shell_auth(session, item["tool"]),
                            "boot": BOOT_ID,
                            "model": model_label(item.get("model", ""), item["tool"]),
                            "context": item.get("context"),
                            "activity": "",
                            "output": "",
                            "artifacts": [],
                        })
                    return self._json({
                        "messages": session_messages(item["log_path"], item["tool"]),
                        "queued": queued_inputs(item["log_path"]),
                        "question": pending_question(session, item["tool"]),
                        "auth": pending_shell_auth(session, item["tool"]),
                        "boot": BOOT_ID,
                        "model": model_label(item.get("model", ""), item["tool"]),
                        "context": item.get("context"),
                        "activity": session_activity(session, item["log_path"], item["tool"]),
                        "output": session_transcript(item["log_path"], item["tool"]),
                        "artifacts": session_artifacts(item["log_path"], item["tool"]),
                    })
                return self._json({"output": capture_session(session)})
            except Exception as exc:
                return self._json({"error": str(exc)}, 500)
        if parsed.path == "/api/mentions" or parsed.path == "/api/rooms" \
                or re.fullmatch(r"/api/rooms/(\d+)/messages", parsed.path):
            if not CW_ENABLED:
                return self._json({"error": "Chatwork連携が設定されていません"}, 404)
        if parsed.path == "/api/mentions":
            try:
                force = urllib.parse.parse_qs(parsed.query).get("refresh") == ["1"]
                cache = refresh_chatwork(force=force)
                return self._json({"items": recent_mentions(cache), "errors": cache.get("errors", [])})
            except Exception as exc:
                return self._chatwork_error(exc)
        if parsed.path == "/api/rooms":
            try:
                force = urllib.parse.parse_qs(parsed.query).get("refresh") == ["1"]
                cache = refresh_chatwork(force=force)
                rooms = [
                    {"room_id": room.get("room_id"), "name": room.get("name", ""),
                     "last_update_time": room_updated_at(room)}
                    for room in cache.get("rooms", [])
                ]
                return self._json({"items": rooms})
            except Exception as exc:
                return self._chatwork_error(exc)
        match = re.fullmatch(r"/api/rooms/(\d+)/messages", parsed.path)
        if match:
            try:
                room_id = match.group(1)
                with CW_LOCK:
                    cache = load_cw_cache()
                    room = next(
                        (r for r in cache.get("rooms", []) if str(r.get("room_id")) == room_id),
                        {"room_id": int(room_id), "name": ""},
                    )
                    messages = cw_get(f"/rooms/{room_id}/messages?force=1")
                    items = [message_item(message, room) for message in messages[-CW_MESSAGE_LIMIT:]]
                    cache.setdefault("messages", {})[room_id] = items
                    save_cw_cache(cache)
                return self._json({"items": list(reversed(items))})
            except Exception as exc:
                return self._chatwork_error(exc)
        if self.path.startswith("/launch"):
            # ショートカット用: GET /launch?dir=...&go=1 でも起動可能
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if qs.get("go") == ["1"] and qs.get("dir"):
                return self._launch(
                    qs["dir"][0],
                    qs.get("model", ["default"])[0],
                    qs.get("tool", ["claude"])[0],
                    qs.get("prompt", [""])[0],
                    qs.get("launch_mode", ["web"])[0],
                    qs.get("bypass", ["0"])[0],
                    qs.get("resume", [""])[0],
                )
            return self._page(render(view="new"), 200)
        self._page(render(view="sessions"))

    def do_POST(self):
        if not client_allowed(self.client_address[0]):
            return self._deny()
        length = int(self.headers.get("Content-Length", 0))
        if length > 16 * 1024 * 1024:
            return self._json({"error": "送信データが大きすぎます"}, 413)
        body = self.rfile.read(length)
        if self.path == "/api/launch/image":
            # 起動前なのでセッション別ディレクトリではなく launch 用に保存する。
            # 24時間経過後の掃除は save_uploaded_image 内の cleanup_uploads に任せる。
            try:
                path = save_uploaded_image(
                    body, self.headers.get("Content-Type", ""), "launch"
                )
                return self._json({"ok": True, "path": path})
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
            except OSError as exc:
                return self._json({"error": f"画像を保存できませんでした: {exc}"}, 500)
        image_match = re.fullmatch(
            r"/api/sessions/(agent-[A-Za-z0-9_.-]+)/image", self.path
        )
        if image_match:
            session = image_match.group(1)
            if not valid_session(session):
                return self._json({"error": "セッションが見つかりません"}, 404)
            try:
                path = save_uploaded_image(
                    body, self.headers.get("Content-Type", ""), session
                )
                return self._json({"ok": True, "path": path})
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
            except OSError as exc:
                return self._json({"error": f"画像を保存できませんでした: {exc}"}, 500)
        file_match = re.fullmatch(
            r"/api/sessions/(agent-[A-Za-z0-9_.-]+)/file", self.path
        )
        if file_match:
            session = file_match.group(1)
            if not valid_session(session):
                return self._json({"error": "セッションが見つかりません"}, 404)
            try:
                filename = urllib.parse.unquote(self.headers.get("X-Filename", ""))
                path = save_uploaded_file(body, filename, session)
                return self._json({"ok": True, "path": path})
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
            except OSError as exc:
                return self._json({"error": f"ファイルを保存できませんでした: {exc}"}, 500)
        try:
            qs = urllib.parse.parse_qs(body.decode("utf-8"))
        except UnicodeDecodeError:
            return self._json({"error": "送信データを読み取れませんでした"}, 400)
        if self.path == "/api/update":
            try:
                version = qs.get("version", [""])[0]
                release = latest_release(force=True)
                if not release["available"] or version != release["latest"]:
                    return self._json({"error": "利用できるReleaseではありません"}, 400)
                install_release(version)
                self._json({"ok": True, "version": version})
                # レスポンスをブラウザへ返してから、更新後のコードで再起動する。
                timer = threading.Timer(0.5, restart_server)
                timer.daemon = True
                timer.start()
                return
            except Exception as exc:
                return self._json({"error": str(exc)}, 500)
        match = re.fullmatch(
            r"/api/sessions/(agent-[A-Za-z0-9_.-]+)/(input|key|kill|restart|handoff|answer|model|terminal|note|pin)",
            self.path,
        )
        if match:
            session, action = match.groups()
            if not valid_session(session):
                return self._json({"error": "セッションが見つかりません"}, 404)
            try:
                if action == "input":
                    value = qs.get("text", [""])[0]
                    if len(value) > 20000:
                        return self._json({"error": "入力が長すぎます（20000文字まで）"}, 400)
                    message = send_session_text(
                        session, value, qs.get("enter", ["1"])[0] == "1"
                    )
                    return self._json({"ok": True, "message": message})
                elif action == "note":
                    value = qs.get("note", [""])[0].strip()
                    if len(value) > 1000:
                        return self._json({"error": "メモが長すぎます（1000文字まで）"}, 400)
                    # 空文字も明示的に保存し、既存メモを削除できるようにする。
                    result = (
                        tmux_run("set-option", "-t", session, "@launcher_note", value)
                        if value else tmux_run("set-option", "-u", "-t", session, "@launcher_note")
                    )
                    if result.returncode != 0:
                        raise RuntimeError(result.stderr.strip() or "メモを保存できませんでした")
                    invalidate_session_cache()
                    return self._json({"ok": True, "note": value})
                elif action == "pin":
                    pinned = qs.get("pinned", ["0"])[0] == "1"
                    result = (
                        tmux_run("set-option", "-t", session, "@launcher_pinned", "1")
                        if pinned else tmux_run(
                            "set-option", "-u", "-t", session, "@launcher_pinned"
                        )
                    )
                    if result.returncode != 0:
                        raise RuntimeError(result.stderr.strip() or "ピン留めを変更できませんでした")
                    invalidate_session_cache()
                    return self._json({"ok": True, "pinned": pinned})
                elif action == "model":
                    value = qs.get("model", [""])[0]
                    item = next(item for item in managed_sessions() if item["name"] == session)
                    if value not in switchable_models(item["tool"]):
                        return self._json({"error": "指定できないモデルです"}, 400)
                    if not item["session_id"]:
                        return self._json({"error": "resumeできる会話IDが見つかりません"}, 400)
                    # /model <名前> の打ち込みはグローバルデフォルトまで書き換えてしまう
                    # （saved as your default for new sessions）ため、--model 付きの
                    # resume 再起動でセッション限定の切り替えにする。
                    new_session = self._restart_session(
                        session, item, bool(item.get("bypass")), ["--model", value],
                    )
                    # ログに新しいモデルが現れるのは次の応答時なので、それまでの
                    # 表示用に送信時刻とセットで覚えておく。
                    tmux_run(
                        "set-option", "-t", new_session, "@launcher_model",
                        f"{value} {time.time():.3f}",
                    )
                    return self._json({
                        "ok": True,
                        "model": model_label(value, item["tool"]),
                        "session": new_session,
                    })
                elif action == "answer":
                    number = qs.get("number", [""])[0]
                    if not re.fullmatch(r"[1-9yn]", number):
                        return self._json({"error": "選択番号が不正です"}, 400)
                    # メニュー版の TUI は数字/文字キーで即確定する。プレーン版
                    # （Enter selection [1-N] / Enter y/n:）は Enter が要るので、
                    # プロンプトが残っていたら追送する。
                    result = tmux_run("send-keys", "-t", session, number)
                    if result.returncode != 0:
                        raise RuntimeError(result.stderr.strip() or "キーを送信できませんでした")
                    time.sleep(0.5)
                    screen = capture_session(session)
                    if (
                        "Enter selection [" in screen or "Enter y/n" in screen
                        or "enter to submit" in screen.lower()
                    ):
                        tmux_run("send-keys", "-t", session, "Enter")
                elif action == "key":
                    key = qs.get("key", [""])[0]
                    if key not in {"Enter", "Escape", "C-c", "Up", "Down", "Left", "Right"}:
                        return self._json({"error": "許可されていないキーです"}, 400)
                    result = tmux_run("send-keys", "-t", session, key)
                    if result.returncode != 0:
                        raise RuntimeError(result.stderr.strip() or "キーを送信できませんでした")
                elif action == "kill":
                    result = tmux_run("kill-session", "-t", session)
                    if result.returncode != 0:
                        raise RuntimeError(result.stderr.strip() or "終了できませんでした")
                    invalidate_session_cache()
                elif action == "terminal":
                    item = next(item for item in managed_sessions() if item["name"] == session)
                    if not item["session_id"] or item["tool"] not in TOOLS:
                        return self._json({"error": "resumeできる会話IDが見つかりません"}, 400)
                    pane = self._to_terminal_pane(session, item)
                    return self._json({"ok": True, "pane": pane})
                elif action == "handoff":
                    item = next(item for item in managed_sessions() if item["name"] == session)
                    new_session = self._handoff_session(session, item)
                    return self._json({"ok": True, "session": new_session})
                else:
                    item = next(item for item in managed_sessions() if item["name"] == session)
                    if not item["session_id"] or item["tool"] not in TOOLS:
                        return self._json({"error": "resumeできる会話IDが見つかりません"}, 400)
                    # 指定がなければ元の権限モードのまま resume する
                    bypass = item.get("bypass") or qs.get("bypass", ["0"])[0] == "1"
                    new_session = self._restart_session(session, item, bypass)
                    return self._json({"ok": True, "session": new_session})
                return self._json({"ok": True})
            except Exception as exc:
                return self._json({"error": str(exc)}, 500)
        if self.path == "/launch":
            return self._launch(
                qs.get("dir", [""])[0],
                qs.get("model", ["default"])[0],
                qs.get("tool", ["claude"])[0],
                qs.get("prompt", [""])[0],
                qs.get("launch_mode", ["web"])[0],
                qs.get("bypass", ["0"])[0],
                qs.get("resume", [""])[0],
            )
        if self.path == "/migrate":
            return self._migrate(
                qs.get("pane_id", [""])[0], qs.get("session_id", [""])[0]
            )
        self._page(render(), 404)

    def _upload_file(self, url_path):
        """アップロード済みファイルをサムネイル/プレビュー用に配信する。"""
        inline_types = {".png": "image/png", ".jpg": "image/jpeg",
                        ".gif": "image/gif", ".webp": "image/webp",
                        ".pdf": "application/pdf"}
        # テキスト系はスクリプト実行され得ない text/plain 固定でインライン表示する
        text_types = {".json", ".txt", ".md", ".log", ".csv", ".tsv", ".xml",
                      ".yaml", ".yml", ".toml", ".ini", ".diff", ".patch"}
        rel = urllib.parse.unquote(url_path[len("/uploads/"):])
        path = os.path.realpath(os.path.join(UPLOAD_DIR, rel))
        if not path.startswith(UPLOAD_DIR + "/") or not os.path.isfile(path):
            return self._json({"error": "ファイルが見つかりません"}, 404)
        extension = os.path.splitext(path)[1].lower()
        attachment = False
        content_type = inline_types.get(extension)
        if not content_type:
            if extension in text_types:
                content_type = "text/plain; charset=utf-8"
            else:
                content_type = "application/octet-stream"
                attachment = True
        try:
            with open(path, "rb") as source:
                data = source.read()
        except OSError:
            return self._json({"error": "ファイルを読み込めませんでした"}, 500)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        if attachment:
            quoted = urllib.parse.quote(os.path.basename(path))
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{quoted}")
        self.send_header("Cache-Control", "private, max-age=86400")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _migrate(self, raw_pane_id, session_id):
        if not raw_pane_id.isdigit() or not re.fullmatch(r"[0-9a-f-]{36}", session_id):
            return self._page(render('<div class="msg err">❌ 移行指定が不正です</div>'), 400)
        pane = pane_for_id(raw_pane_id)
        agent = pane_agent(pane) if pane else None
        if not pane or not agent:
            return self._page(render('<div class="msg err">❌ 対象CLIは既に終了しています</div>'), 404)
        cwd = urllib.parse.urlparse(pane.get("cwd", "")).path
        candidates = resume_candidates(agent["tool"], cwd, agent["explicit_id"])
        selected = next((item for item in candidates if item["id"] == session_id), None)
        if not selected:
            return self._page(render('<div class="msg err">❌ 選択した会話を確認できません</div>'), 400)

        # 保存を完了させてからresumeするため、現在のTUIを通常終了させる。
        wezterm_cli("send-text", "--pane-id", raw_pane_id, "--no-paste", "\x03")
        time.sleep(0.3)
        wezterm_cli("send-text", "--pane-id", raw_pane_id, "--no-paste", "/exit\r")
        deadline = time.time() + 8
        while time.time() < deadline and pane_agent(pane):
            time.sleep(0.25)
        if pane_agent(pane):
            return self._page(render(
                '<div class="msg err">❌ CLIを終了できませんでした。処理が停止してから再試行してください</div>'
            ), 409)

        cmd = [*TOOLS[agent["tool"]], cwd]
        cmd += ["--resume", session_id] if agent["tool"] == "claude" else ["resume", session_id]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=20,
                env={**os.environ, "CLAUDE_TAB_HEADLESS": "1"},
            )
        except subprocess.TimeoutExpired:
            return self._page(render('<div class="msg err">❌ 再開処理がタイムアウトしました</div>'), 500)
        if result.returncode != 0:
            detail = html.escape((result.stderr or result.stdout or "").strip())
            return self._page(render(f'<div class="msg err">❌ 再開に失敗しました: {detail}</div>'), 500)
        set_session_metadata(
            launcher_session_name(result.stdout), selected["summary"], session_id
        )
        wezterm_cli("kill-pane", "--pane-id", raw_pane_id)
        return self._redirect("/")

    def _to_terminal_pane(self, session, item):
        """セッションを終了し、同じ会話を WezTerm タブ（tmux なし）で resume する。

        プラグインのインストール等、素の TUI で操作したい場合の Web → ターミナル移行。
        起動後の pane はサイドバーの WezTerm カードから読み取り専用で追える。
        """
        tmux_run("send-keys", "-t", session, "C-c")
        time.sleep(0.2)
        result = tmux_run("kill-session", "-t", session)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "終了できませんでした")
        cmd = [*TOOLS[item["tool"]], item["cwd"]]
        cmd += (["--resume", item["session_id"]] if item["tool"] == "claude"
                else ["resume", item["session_id"]])
        if item.get("bypass"):
            cmd += BYPASS_FLAGS[item["tool"]]
        spawned = subprocess.run(
            cmd, capture_output=True, text=True, timeout=20,
            env={**os.environ, "CLAUDE_TAB_NO_TMUX": "1"},
        )
        if spawned.returncode != 0:
            detail = (spawned.stderr or spawned.stdout or "").strip()
            raise RuntimeError(detail or "WezTermタブでの再開に失敗しました")
        invalidate_session_cache()
        match = re.search(r"\bpane (\d+)", spawned.stdout or "")
        return match.group(1) if match else ""

    def _restart_session(self, session, item, bypass, extra_args=()):
        """セッションを終了し、同じ会話を resume で立ち上げ直して新セッション名を返す。"""
        tmux_run("send-keys", "-t", session, "C-c")
        time.sleep(0.2)
        result = tmux_run("kill-session", "-t", session)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "終了できませんでした")
        cmd = [*TOOLS[item["tool"]], item["cwd"]]
        cmd += (["--resume", item["session_id"]] if item["tool"] == "claude"
                else ["resume", item["session_id"]])
        cmd += list(extra_args)
        if bypass:
            cmd += BYPASS_FLAGS[item["tool"]]
        resumed = subprocess.run(
            cmd, capture_output=True, text=True, timeout=20,
            env={**os.environ, "CLAUDE_TAB_HEADLESS": "1"},
        )
        if resumed.returncode != 0:
            detail = (resumed.stderr or resumed.stdout or "").strip()
            raise RuntimeError(detail or "resumeに失敗しました")
        new_session = launcher_session_name(resumed.stdout)
        if not new_session:
            raise RuntimeError("再起動したセッション名を取得できませんでした")
        set_session_metadata(
            new_session, item["summary"], item["session_id"], bypass, item.get("note", ""),
            bool(item.get("pinned")),
        )
        invalidate_session_cache()
        return new_session

    def _handoff_session(self, session, item):
        """会話記録を渡して、同じ cwd で反対側のツールを起動する。"""
        target = "codex" if item["tool"] == "claude" else "claude"
        handoff_path = save_handoff(item)
        prompt = (
            f"{item['tool']} からこのセッションを引き継いでください。"
            f"まず引き継ぎ資料 `{handoff_path}` を読み、現在の作業ツリーと git status を確認してください。"
            "会話中の依頼、決定事項、未完了作業を把握してから、その続きに着手してください。"
            "不明点は推測せず、必要な場合だけユーザーへ確認してください。"
        )
        cmd = [*TOOLS[target], item["cwd"]]
        if item.get("bypass"):
            cmd += BYPASS_FLAGS[target]
        cmd.append(prompt)
        started_at = time.time()
        launched = subprocess.run(
            cmd, capture_output=True, text=True, timeout=20,
            env={**os.environ, "CLAUDE_TAB_HEADLESS": "1"},
        )
        if launched.returncode != 0:
            detail = (launched.stderr or launched.stdout or "").strip()
            raise RuntimeError(detail or f"{target} の起動に失敗しました")
        new_session = launcher_session_name(launched.stdout)
        if not new_session:
            raise RuntimeError("引き継ぎ先のセッション名を取得できませんでした")
        session_id = wait_for_new_session_id(target, item["cwd"], started_at)
        set_session_metadata(
            new_session, item.get("summary") or prompt, session_id,
            bool(item.get("bypass")), item.get("note", ""), bool(item.get("pinned")),
        )
        result = tmux_run("kill-session", "-t", session)
        if result.returncode != 0:
            tmux_run("kill-session", "-t", new_session)
            raise RuntimeError(result.stderr.strip() or "引き継ぎ元を終了できませんでした")
        invalidate_session_cache()
        return new_session

    def _launch(self, raw_dir: str, model: str = "default", tool: str = "claude",
                prompt: str = "", launch_mode: str = "web", bypass: str = "0",
                resume: str = ""):
        path, err = validate_dir(raw_dir)
        if err:
            return self._page(render(f'<div class="msg err">❌ {html.escape(err)}</div>', "new"))
        if tool not in TOOLS:
            return self._page(render('<div class="msg err">❌ 不正なツール指定です</div>', "new"))
        if model not in {v for v, _ in MODELS_BY_TOOL[tool]}:
            return self._page(render('<div class="msg err">❌ 不正なモデル指定です</div>', "new"))
        if launch_mode not in {"web", "wezterm"}:
            return self._page(render('<div class="msg err">❌ 不正な起動方法です</div>', "new"))
        if bypass not in {"0", "1"}:
            return self._page(render('<div class="msg err">❌ 不正な権限指定です</div>', "new"))
        resume_log = ""
        if resume:
            if not re.fullmatch(r"[0-9a-f-]{36}", resume):
                return self._page(render('<div class="msg err">❌ 再開する会話の指定が不正です</div>', "new"))
            resume_log = conversation_log_path(tool, path, resume)
            if not resume_log:
                return self._page(render('<div class="msg err">❌ 再開する会話が見つかりません</div>', "new"))
        skip_permissions = bypass == "1"
        prompt = prompt.strip()
        if len(prompt) > 8000:
            return self._page(render('<div class="msg err">❌ プロンプトが長すぎます（8000文字まで）</div>', "new"))
        cmd = [*TOOLS[tool], path]
        if resume:
            cmd += ["--resume", resume] if tool == "claude" else ["resume", resume]
        if model != "default":
            cmd += ["--model", model]
        if skip_permissions:
            cmd += BYPASS_FLAGS[tool]
        if prompt:
            cmd.append(prompt)
        started_at = time.time()
        try:
            launcher_env = {**os.environ}
            if launch_mode == "web":
                launcher_env["CLAUDE_TAB_HEADLESS"] = "1"
            else:
                launcher_env["CLAUDE_TAB_NO_TMUX"] = "1"
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=20,
                env=launcher_env,
            )
        except subprocess.TimeoutExpired:
            return self._page(render('<div class="msg err">❌ タイムアウトしました</div>', "new"))
        if r.returncode == 0:
            if launch_mode == "wezterm":
                mode_note = "（バイパス）" if skip_permissions else ""
                msg = (f'<div class="msg ok">✅ WezTermタブで起動しました{mode_note}: '
                       f'{html.escape(path)}</div>')
                return self._page(render(msg, "new"))
            session_name = launcher_session_name(r.stdout)
            if resume:
                # 再開時は会話IDが分かっているので探索せず、元の要約を引き継ぐ
                summary = log_meta(resume_log, tool).get("summary", "")
                set_session_metadata(session_name, summary, resume, skip_permissions)
            else:
                session_id = wait_for_new_session_id(tool, path, started_at)
                set_session_metadata(session_name, prompt, session_id, skip_permissions)
            if session_name:
                invalidate_session_cache()
                return self._redirect(
                    "/terminal?session=" + urllib.parse.quote(session_name)
                )
            detail = "起動したセッション名を取得できませんでした"
        else:
            detail = (r.stderr or r.stdout or "").strip()
        msg = f'<div class="msg err">❌ 失敗: {html.escape(detail)}</div>'
        self._page(render(msg, "new"))

    def log_message(self, fmt, *args):
        pass  # launchd のログを汚さない


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Agent Deck — AI コーディング CLI の Web ランチャー & セッションマネージャ"
    )
    parser.add_argument(
        "--port", type=int,
        help="待ち受けポート（省略時: AGENT_DECK_PORT か設定ファイルの port、既定 8787）",
    )
    cli_args = parser.parse_args()
    if cli_args.port:
        PORT = cli_args.port
    os.makedirs(UPLOAD_DIR, mode=0o700, exist_ok=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
