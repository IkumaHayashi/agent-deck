#!/usr/bin/env python3
"""Claude Code / Codex の使用量（レートリミット枠）を JSON で標準出力する。

config.json の `usage_command` に設定すると、サイドバー下部へ表示される。

    "usage_command": "python3 ~/agent-deck/tools/ai-usage.py"

⚠️ これは参考実装である。どちらのベンダーも使用量を返す公開APIを提供して
いないため、Claude Code / Codex 本体が内部で使う**非公開エンドポイント**を
叩いている。ベンダー側の変更で予告なく壊れることがある。壊れた場合は
`usage_command` を外せば、Agent Deck の他の機能には影響しない。

トークンは各CLIが保存したものを読むだけで、送信先は各ベンダーのAPIのみ。
取得に成功したレスポンスは CACHE_DIR にキャッシュし、一時的なエラーや
オフライン時は1時間以内のキャッシュを「⚠」付きで表示する。
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

# Claude Code はキーチェーン、Codex は auth.json にトークンを置く
CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_AUTH_PATH = os.path.expanduser("~/.codex/auth.json")
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

CACHE_DIR = os.path.expanduser("~/.cache/agent-deck/usage")
CACHE_MAX_AGE_SEC = 60 * 60  # これより古いキャッシュは使わない
# server.py 側は usage_command を25秒で打ち切る。2プロバイダ分を叩いても
# 収まるよう、1リクエストあたりは短めにする
HTTP_TIMEOUT_SEC = 10

KIND_LABELS = {
    "session": "5時間枠",
    "weekly_all": "週間枠",
}

WARN_THRESHOLD = 70
DANGER_THRESHOLD = 90


def http_get_json(url, headers):
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "agent-deck-ai-usage/1.0",
            **headers,
        },
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SEC) as response:
        return json.load(response)


def parse_reset(raw):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def format_reset(reset_at):
    if reset_at is None:
        return ""
    now = datetime.now(timezone.utc).astimezone()
    if reset_at.date() == now.date():
        return f"リセット {reset_at:%H:%M}"
    return f"リセット {reset_at.month}/{reset_at.day} {reset_at:%H:%M}"


def severity_level(percent, severity=None):
    """normal / warning / critical の3値に正規化する。"""
    if severity == "critical" or percent >= DANGER_THRESHOLD:
        return "critical"
    if severity in ("warning", "elevated") or percent >= WARN_THRESHOLD:
        return "warning"
    return "normal"


def make_row(label, percent, resets_at, severity=None):
    return {
        "label": label,
        "percent": percent,
        "reset_label": format_reset(resets_at),
        "level": severity_level(percent, severity),
    }


# ---------------------------------------------------------------- キャッシュ


def cache_path(provider_id):
    return os.path.join(CACHE_DIR, f"{provider_id}.json")


def save_cache(provider_id, usage):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(cache_path(provider_id), "w", encoding="utf-8") as cache:
            json.dump(
                {
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "usage": usage,
                },
                cache,
            )
    except OSError:
        pass


def load_cache(provider_id):
    """有効期限内なら (usage, 取得時刻[ローカル]) を返す。"""
    try:
        with open(cache_path(provider_id), encoding="utf-8") as cache:
            cached = json.load(cache)
        fetched_at = datetime.fromisoformat(cached["fetched_at"])
        age = (datetime.now(timezone.utc) - fetched_at).total_seconds()
        if age <= CACHE_MAX_AGE_SEC:
            return cached["usage"], fetched_at.astimezone()
    except (OSError, ValueError, KeyError):
        pass
    return None, None


def collect_with_cache(provider_id, fetch, parse):
    """fetch を試み、失敗時はキャッシュへフォールバックして parse 結果を返す。"""
    stale_note = None
    try:
        usage = fetch()
        save_cache(provider_id, usage)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            return {
                "ok": False,
                "error": "unauthorized",
                "message": "トークン期限切れ。CLIを一度起動すると更新されます",
            }
        usage, fetched_at = load_cache(provider_id)
        if usage is None:
            return {
                "ok": False,
                "error": "http",
                "message": f"APIエラー: HTTP {error.code}",
            }
        stale_note = f"HTTP {error.code} のため {fetched_at:%H:%M} 時点の値を表示中"
    except (urllib.error.URLError, TimeoutError):
        usage, fetched_at = load_cache(provider_id)
        if usage is None:
            return {
                "ok": False,
                "error": "network",
                "message": "ネットワークエラー（オフライン？）",
            }
        stale_note = f"オフラインのため {fetched_at:%H:%M} 時点の値を表示中"

    result = parse(usage)
    if not result.get("rows"):
        return {
            "ok": False,
            "error": "no_data",
            "message": "使用量データが取得できませんでした",
        }
    result.update({"ok": True, "stale": stale_note})
    return result


# ---------------------------------------------------------------- Claude Code


def claude_get_token():
    """キーチェーンから Claude Code の OAuth アクセストークンを読む。"""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", CLAUDE_KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    try:
        credentials = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return None
    return credentials.get("claudeAiOauth", {}).get("accessToken")


def claude_limit_label(limit):
    kind = limit.get("kind", "")
    if kind in KIND_LABELS:
        return KIND_LABELS[kind]
    model = ((limit.get("scope") or {}).get("model") or {}).get("display_name")
    if kind == "weekly_scoped" and model:
        return f"週間枠 ({model})"
    return kind or "不明"


def claude_parse(usage):
    rows = []
    for limit in usage.get("limits") or []:
        if not isinstance(limit, dict) or "percent" not in limit:
            continue
        rows.append(
            make_row(
                claude_limit_label(limit),
                float(limit["percent"]),
                parse_reset(limit.get("resets_at")),
                limit.get("severity"),
            )
        )
    if not rows:
        # 旧形式フォールバック: five_hour / seven_day 直下のウィンドウ
        for key, label in (("five_hour", "5時間枠"), ("seven_day", "週間枠")):
            window = usage.get(key)
            if isinstance(window, dict) and window.get("utilization") is not None:
                rows.append(
                    make_row(
                        label,
                        float(window["utilization"]),
                        parse_reset(window.get("resets_at")),
                    )
                )

    extra = usage.get("extra_usage")
    extra_row = None
    if isinstance(extra, dict) and extra.get("is_enabled"):
        exponent = int(extra.get("decimal_places") or 2)
        used = float(extra.get("used_credits") or 0) / (10**exponent)
        monthly_limit = float(extra.get("monthly_limit") or 0) / (10**exponent)
        extra_row = make_row(
            "追加クレジット", float(extra.get("utilization") or 0), None
        )
        extra_row["reset_label"] = f"${used:.2f} / ${monthly_limit:.2f}"

    return {"rows": rows, "extra": extra_row}


def collect_claude():
    token = claude_get_token()
    if not token:
        return {
            "ok": False,
            "error": "no_token",
            "message": "キーチェーンにトークンがありません。claude で一度ログインしてください",
        }
    return collect_with_cache(
        "claude",
        lambda: http_get_json(
            CLAUDE_USAGE_URL,
            {
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
            },
        ),
        claude_parse,
    )


# ---------------------------------------------------------------- Codex


def codex_get_auth():
    try:
        with open(CODEX_AUTH_PATH, encoding="utf-8") as auth_file:
            auth = json.load(auth_file)
    except (OSError, json.JSONDecodeError):
        return None, None
    tokens = auth.get("tokens") or {}
    return tokens.get("access_token"), tokens.get("account_id")


def codex_window_label(window, name=None):
    if name:
        return name
    hours = (window.get("limit_window_seconds") or 0) / 3600
    if hours <= 24:
        return f"{round(hours)}時間枠"
    return "週間枠" if round(hours / 24) == 7 else f"{round(hours / 24)}日枠"


def codex_window_row(window, name=None):
    if not isinstance(window, dict) or window.get("used_percent") is None:
        return None
    reset = None
    if window.get("reset_at"):
        reset = datetime.fromtimestamp(window["reset_at"], tz=timezone.utc).astimezone()
    return make_row(
        codex_window_label(window, name), float(window["used_percent"]), reset
    )


def codex_parse(usage):
    rows = []
    rate_limit = usage.get("rate_limit") or {}
    for key in ("primary_window", "secondary_window"):
        row = codex_window_row(rate_limit.get(key))
        if row:
            rows.append(row)
    for entry in usage.get("additional_rate_limits") or []:
        window = (entry.get("rate_limit") or {}).get("primary_window")
        row = codex_window_row(window, entry.get("limit_name"))
        # 追加枠（Spark等）は別勘定でメーターされ、未使用でもAPIが返してくる。
        # 0%のうちは表示ノイズなので、使い始めたときだけ出す
        if row and row["percent"] > 0:
            rows.append(row)
    return {"rows": rows, "extra": None}


def collect_codex():
    token, account_id = codex_get_auth()
    if not token:
        return None  # Codex 未使用の環境ではセクションごと出さない
    return collect_with_cache(
        "codex",
        lambda: http_get_json(
            CODEX_USAGE_URL,
            {
                "Authorization": f"Bearer {token}",
                "chatgpt-account-id": account_id or "",
            },
        ),
        codex_parse,
    )


# ---------------------------------------------------------------- 集約


def collect_all():
    providers = []
    claude = collect_claude()
    claude["name"] = "Claude Code"
    providers.append(claude)

    codex = collect_codex()
    if codex is not None:
        codex["name"] = "Codex"
        providers.append(codex)

    return {
        "providers": providers,
        "updated_at": datetime.now(timezone.utc).astimezone().strftime("%H:%M"),
    }


def main():
    print(json.dumps(collect_all(), ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # サイドバー側で原因が見えるように
        print(
            json.dumps(
                {"providers": [], "error": f"予期しないエラー: {error}"},
                ensure_ascii=False,
            )
        )
        sys.exit(0)
