# agent-deck

## /model 送信によるデフォルト汚染に注意

TUI への引数付き `/model` の打ち込みは「saved as your default for new sessions」となり、
`~/.claude/settings.json` のグローバルデフォルトを書き換える。過去に Web UI の
モデル切り替え（旧実装は `send_session_text` で `/model <値>` を送信）でデフォルトが
haiku に化け、翌朝の launchd 自動セッション（mf-daily-check 等）がすべて haiku で
動く事故が起きた（2026-08-01〜08-03）。

現在の実装は **resume 再起動方式**（`_restart_session` で kill →
`claude --resume <会話ID> --model <値>` で再開）。`--model` フラグはセッション限定で
永続化されないため、デフォルトは汚れない。モデル切り替えを再び `/model` 送信に
戻さないこと。

テストや一時利用でモデルを変える場合は以下を使う（どちらもセッション限定で永続化されない）:

- 起動時フラグ: `claude-tab <dir> --model haiku` / `claude --model haiku`
- TUI の `/model` を引数なしで開き、**s キー**で選択（session-only）
