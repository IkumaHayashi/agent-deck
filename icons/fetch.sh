#!/bin/sh
# ツールアイコンを公式配布元から取得して icons/ に配置する。
#
# Claude / OpenAI のロゴは各社の商標であり再配布ライセンスが付与されないため、
# リポジトリには同梱せず利用時に各自で取得する方式にしている。
# 取得したロゴの権利は各社に帰属し、ツールの識別表示の目的でのみ使用すること。
# WezTerm のアイコンは MIT ライセンスの公式リポジトリから取得する。
set -u
cd "$(dirname "$0")"

fetch() {
  name="$1"; url="$2"
  if ! curl -fsSL --max-time 30 -o "$name.tmp" "$url"; then
    echo "NG: $name の取得に失敗しました ($url)" >&2
    rm -f "$name.tmp"
    return 1
  fi
  # PNG シグネチャを確認してから配置する（エラーページ等の誤保存を防ぐ）
  case "$(head -c 8 "$name.tmp" | od -An -tx1 | tr -d ' \n')" in
    89504e470d0a1a0a)
      mv "$name.tmp" "$name"
      echo "OK: $name"
      ;;
    *)
      echo "NG: $url は PNG を返しませんでした" >&2
      rm -f "$name.tmp"
      return 1
      ;;
  esac
}

rc=0
fetch claude.png  "https://claude.ai/apple-touch-icon.png" || rc=1
fetch codex.png   "https://developers.openai.com/favicon.png" || rc=1
fetch wezterm.png "https://raw.githubusercontent.com/wezterm/wezterm/main/assets/icon/terminal.png" || rc=1

if [ "$rc" -ne 0 ]; then
  echo "一部のアイコンを取得できませんでした。無くても動作します（テキスト表示になる）。" >&2
fi
exit "$rc"
