# Agent Deck

AI コーディング CLI（Claude Code / Codex）を Mac の tmux 上で起動し、
スマホや別 PC のブラウザから監視・操作する Web ランチャー & セッションマネージャです。

> Agent Deck is a web launcher & session manager for AI coding CLIs
> (Claude Code / Codex) running in tmux on macOS.
> Launch sessions from your phone, watch progress, send messages, and
> hand conversations off between CLIs. The web UI is available in Japanese and English.

![セッション操作画面（PC）](docs/images/terminal-pc.png)

| スマホ: セッション一覧 | スマホ: セッション操作 |
|:---:|:---:|
| ![スマホのセッション一覧](docs/images/list-sp.png) | ![スマホのセッション操作](docs/images/terminal-sp.png) |

## できること

- **ワンタップ起動**: プロジェクトのボタンを押すと tmux セッションで CLI が起動する
- **Issue / PRから起動**: GitHubの番号またはURLを指定し、内容を確認して専用worktreeで作業を始める
- **スマホから操作**: 端末出力のリアルタイム表示、メッセージ送信、AI回答の引用、Enter / Esc / Ctrl+C、画像・ファイル添付
- **sandbox 外でコマンド実行**: shell コードブロックから新しいWebシェルを開いて実行
- **セッション一覧**: 実行中/待機中の判定、会話の最初のプロンプト表示、コンテキスト使用率、作成した PR/issue のチップ表示、先頭固定/後回しの手動配置
- **再起動後の自動復元**: Mac の再起動で tmux が消えても、保存した会話IDからセッションを自動的に再開
- **GitHub レビュー**: 自分へのレビュー依頼または指定したPRから、PRのベースブランチとの差分を開いたAIセッションを開始
- **会話の再開**: 最近の会話を `--resume` 付きでワンタップ再起動。会話の本文や resume ID で全会話から探すこともできる。モデル切り替えも resume 方式で安全に行う
- **Claude ⇔ Codex の引き継ぎ**: 会話履歴を引き継ぎ資料として保存し、同じ作業ディレクトリで反対側の CLI に交代させる
- **Chatwork 受信箱**（任意）: メンションを一覧し、そのままプロンプトにセットして起動

起動ページではツール・起動方法・権限・モデル・最初のプロンプトを選んで
ワンタップでセッションを開始できます。

「Issue / PR」タブでは、プロジェクトを選び、Issue / PR番号またはGitHub URLを
入力すると種別を自動判定し、タイトル・本文・ラベルをプレビューできます。そこから起動すると、Issueは
デフォルトブランチを起点とする `agent-deck/issue-<番号>`、PRは対象headを起点とする
`agent-deck/pull-<番号>` ブランチの専用worktreeが作られ、対象URLを最初の指示として
セッションへ渡します。

![起動ページ（PC）](docs/images/launch-pc.png)

## 動作要件

- macOS
- tmux
- Python 3.12 以上（標準ライブラリのみ。追加パッケージ不要）
- [Claude Code](https://claude.com/claude-code) および/または Codex CLI
- PR/issue チップ表示を使う場合は `gh` CLI

## インストール

```sh
git clone https://github.com/YOUR_NAME/agent-deck.git
cd agent-deck

# CLI ランチャーを PATH に置く
ln -s "$PWD/deck" ~/.local/bin/deck

# 設定ファイル（任意。無くても既定値で動く）
mkdir -p ~/.config/agent-deck
cp config.example.json ~/.config/agent-deck/config.json
# → project_bases / pinned を自分のプロジェクトに書き換える

# ツールアイコン（任意）: 公式配布元から取得する。無ければテキスト表示
./icons/fetch.sh

# Web UI を launchd で常駐させる
mkdir -p ~/.local/share/agent-deck
sed -e "s|__REPO_DIR__|$PWD|g" -e "s|__HOME__|$HOME|g" \
    -e "s|__PYTHON__|$(command -v python3)|g" \
    launchd/com.agent-deck.web.plist.template \
    > ~/Library/LaunchAgents/com.agent-deck.web.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.agent-deck.web.plist
```

`http://<Mac の Tailscale IP>:8787` にアクセスすると起動ページが開きます。

画面下部には現在のバージョンが表示されます。GitHub Releases に新しいバージョンが
ある場合は更新ボタンが現れ、作業ツリーにローカル変更がなければ対象Releaseへ
fast-forwardして自動再起動します。

FileVault が有効な場合は再起動後に一度 Mac 本体でログインが必要です。

## ⚠️ セキュリティ上の注意

**Agent Deck に認証はありません。** アクセス元 IP による制限のみで、
既定では Tailscale 網内（100.64.0.0/10）と localhost だけを許可します。

- Web UI に到達できる人は、**あなたの Mac 上で任意のコマンドを実行できるのと同等**の権限を持ちます（任意ディレクトリで CLI を起動し、任意のテキストを送信できるため）
- `allowed_networks` を信頼できる端末しかいないネットワークより広げないでください
- 公共の LAN やインターネットへの直接公開は絶対にしないでください
- ポート転送やリバースプロキシで公開する場合は、必ず前段に認証を置いてください

## 既知の制約

- **内部仕様への依存**: Claude Code の会話ログ（JSONL）、選択肢画面の tmux パース、Codex の rollout ファイル等、CLI の非公開仕様に依存しています。CLI のアップデートで表示が壊れることがあります
- **API 課金**: セッション一覧の「要対応/他者待ち/完了」分類は `claude -p` を呼び出すため、少量の API / サブスクリプション利用が発生します。既定は Haiku で、`wait_classifier_model` により Sonnet などへ変更できます

## 表示言語

Web UIは日本語と英語に対応しています。初回アクセス時はブラウザの
`Accept-Language` を参照し、対応言語が見つからない場合は日本語を表示します。
画面の「表示言語」から切り替えると、選択内容をCookieへ1年間保存します。

言語を直接指定する場合はURLへ `lang=ja` または `lang=en` を付けます。

```text
http://<Mac の Tailscale IP>:8787/?lang=en
```

## 使い方

### 権限バイパス起動

`/new` の「権限」で「⚠️ バイパス」を選ぶと、確認プロンプトを省いて起動します。

| ツール | フラグ | 効果 |
|--------|--------|------|
| Claude Code | `--dangerously-skip-permissions` | 権限確認をスキップ |
| Codex | `-a never -s workspace-write` | 確認なし・書き込みはワークスペース内に限定 |

バイパスで起動したセッションは一覧に `bypass` バッジが付き、Web UI からの
restart（resume）でも同じ権限モードを引き継ぎます。
ショートカット用の直接起動 URL では `bypass=1` を付けます。

```
/launch?dir=<パス>&go=1&model=<m>&bypass=1
```

### CLI ランチャー（deck）

```sh
deck <ディレクトリ> [CLIへの追加引数...]
deck ~/projects/my-app --model haiku
TAB_BIN=codex deck ~/projects/my-app
```

CLI は tmux セッション内で動くため、ssh が切れてもセッションは生き続けます。

通常セッションの会話ID・作業ディレクトリ・モデル・権限モード・メモ・並び位置は
`data_dir/sessions.json` に保存されます。Mac の再起動後に Agent Deck が立ち上がると、
保存された会話を自動的に `resume` してセッション一覧へ戻します。「終了」ボタンで
閉じたセッションは復元対象から外れます。`CLAUDE_TAB_EPHEMERAL=1` の自動実行ジョブも
復元されません。自動復元を無効にする場合は設定に `"restore_sessions": false` を指定します。

環境変数:

| 変数 | 効果 |
|------|------|
| `TAB_BIN=<コマンド>` | 起動する CLI を差し替える（既定は claude） |
| `CLAUDE_TAB_EPHEMERAL=1` | CLI の終了と同時に tmux セッションごと破棄する（自動実行ジョブ向け） |
| `CLAUDE_TAB_LABEL=<名前>` | セッションに識別札を付け、起動時に同じ札の古いセッションを片付ける |

起動したセッションには `CLAUDE_TAB_SESSION`（別名 `DECK_SESSION`）が渡されるので、
CLI 自身が `tmux kill-session -t "$CLAUDE_TAB_SESSION"` で自分を終了できます。

### Web からセッションを操作する

Web UI から起動した tmux セッションは、セッション一覧から開くと端末出力のリアルタイム
表示・メッセージ送信・Enter / Esc / Ctrl+C・セッション終了ができます。
「終了」でリンクされた Git worktree 内のセッションを閉じると、その worktree も
`git worktree remove` で自動削除します。メイン作業ツリーは削除せず、同じ worktree を
別セッションが使用中の場合は最後のセッション終了時まで削除を保留します。
未コミットの変更がある worktree は Git の安全機構により削除せず、エラーを表示します。
AI の回答内でテキストをドラッグ選択すると「選択部分を引用」がポップアップします。
押すと、その部分を引用形式で入力欄へ追加できます。
過去のメッセージを読んでいる間は新着が届いてもスクロール位置を維持し、最下部へ
戻すと新着への自動追従を再開します。
サイドバーの検索欄から、実行中・終了済みを含む全セッションのやりとりを文字列検索
できます（一覧画面では `Cmd+F` / `Ctrl+F`、会話画面では `Cmd+Shift+F` /
`Ctrl+Shift+F`）。結果を開くと該当セッション内の一致箇所を表示し、終了済みの会話は
その場で再開します。会話ヘッダーの「検索」（または `Cmd+F` / `Ctrl+F`）では、
現在の会話内を検索し、Enter / Shift+Enter または矢印ボタンで前後へ移動できます。
新規セッション画面の「会話を再開」タブでも、検索欄に2文字以上入力すると直近一覧の
絞り込みから全会話の本文検索に切り替わり、一致箇所の抜粋を見ながら再開できます。

会話内の `sh` / `bash` / `zsh` コードブロックにある「シェルで実行」を押すと、
確認後に同じ作業ディレクトリで新しい tmux セッションを開き、コマンドを実行します。
コマンドは AI CLI を経由しないため Codex の sandbox 対象外です。実行後は
そのWebシェルへ移動し、出力を確認できます。

### Claude ⇔ Codex の引き継ぎ

セッション画面の「→ Codex」「→ Claude」から、会話履歴をローカルの引き継ぎ資料へ
保存し、同じ作業ディレクトリのまま反対側の CLI へ切り替えられます。

## 設定

`~/.config/agent-deck/config.json`（`AGENT_DECK_CONFIG` 環境変数で変更可）。
全項目とコメントは [config.example.json](config.example.json) を参照してください。

Codex のモデル選択肢は、設定の `models.codex` を省略すると Codex CLI から
利用可能なモデルを自動取得します。取得結果は5分間キャッシュし、CLIから取得できない
場合は `~/.codex/models_cache.json`、さらに内蔵一覧の順にフォールバックします。
選択肢を固定したい場合だけ `models.codex` を設定してください。

PCではサイドバー上部の歯車、スマートフォンでは設定ページへのリンクから設定UIを
開けます。一般設定とプロジェクト設定は画面から保存でき、Agent Deckの再起動後に
反映されます。実行コマンド・CLIパス・ポート・許可ネットワークなどセキュリティや
接続に関わる項目は確認専用です。変更する場合は設定ファイルを直接編集してください。

差分モードでは、通常のセッションは作業ディレクトリをリポジトリのデフォルトブランチと比較します。
初期表示は `diff_open` で選べます。`never`（既定）は常に閉じ、
`auto` は差分を取得できた場合だけ開き、`always` は常に開いて開始します。
差分モードでも画面下部の入力欄から、そのままセッションへ指示を送信できます。
起動ページの「GitHub レビュー」から開始したセッションでは、この設定にかかわらず
対象PRを紐づけて差分モードを最初から開き、PRのベースブランチと比較します。
レビュー依頼とPR URLの対象リポジトリは、
`pinned`、`project_bases`、`extra_projects` に登録されたプロジェクトの `origin` remoteから特定します。

ポートは優先順に `--port` フラグ > `AGENT_DECK_PORT` 環境変数 > 設定ファイルの
`port` > 既定値 8787 で決まります。設定ファイルを分ければ同じマシンで
複数インスタンスを動かせます（例: `AGENT_DECK_CONFIG=demo.json python3 server.py --port 8788`）。

実行時のデータ（アップロード画像・キャッシュ・ログ）は
`~/.local/share/agent-deck/` に保存されます（`data_dir` で変更可）。

### AI使用量の表示

`usage_command` にコマンドを設定すると、その標準出力のJSONをサイドバー下部へ
表示します。未設定なら何も表示しません。結果は5分キャッシュされ、失敗しても
Agent Deck の他の機能には影響しません。

同梱の参考実装を使う場合:

```json
"usage_command": "python3 ~/agent-deck/tools/ai-usage.py"
```

[`tools/ai-usage.py`](tools/ai-usage.py) は Claude Code の5時間枠・週間枠と、
Codex の各枠を取得します。トークンは各CLIが保存したもの（Claude Code は
キーチェーン、Codex は `~/.codex/auth.json`）を読むだけで、送信先は各ベンダーの
APIのみです。Codex にログインしていない環境では Claude Code の行だけ出ます。

> ⚠️ **参考実装は非公開APIに依存しています。** どちらのベンダーも使用量を返す
> 公開APIを提供していないため、各CLIが内部で使うエンドポイントを叩いています。
> ベンダー側の変更で予告なく壊れることがあります。壊れた場合は
> `usage_command` を外してください。

#### 自分でコマンドを用意する

`usage_command` は任意のコマンドを実行できるので、参考実装を使わず自分の環境に
合わせたスクリプトを書いても構いません。次の形のJSONを標準出力へ出してください。

```json
{
  "updated_at": "15:04",
  "providers": [
    {
      "name": "Claude Code",
      "ok": true,
      "stale": null,
      "rows": [
        {"label": "5時間枠", "percent": 42.0, "reset_label": "リセット 15:19", "level": "normal"}
      ],
      "extra": null
    }
  ]
}
```

| フィールド | 意味 |
|-----------|------|
| `providers[].name` | 行の先頭に出す名前 |
| `providers[].ok` | `false` なら「取得失敗」と表示し、`message` をツールチップに出す |
| `providers[].rows[]` | 表示する枠。`label` は末尾の「枠」を落として表示する |
| `rows[].percent` | 使用率（0〜100）。四捨五入して表示する |
| `rows[].reset_label` | ツールチップに出すリセット時刻。空文字なら出さない |
| `rows[].level` | `normal` / `warning`（橙） / `critical`（赤） |
| `providers[].extra` | 行末に添える追加項目（`rows[]` と同じ形）。不要なら `null` |
| `providers[].stale` | キャッシュ表示中の注記。設定すると「⚠」を出す |

`label` と `reset_label` の接頭辞「リセット 」は、英語表示のとき
`locales/en.json` にあるものだけ翻訳されます。

## 開発

新規起動ページのフロントエンドは、役割ごとに次のファイルへ分けています。

- `templates/new.html`: HTML構造とPythonから差し込むプレースホルダー
- `static/new.css`: レイアウトとレスポンシブ表示
- `static/new.js`: タブ切り替え、フォーム、GitHubレビュー・Chatwork連携

```sh
python3 -m pip install -r requirements-dev.txt
make check
```

Python コードを Ruff で整形するには次を実行します。

```sh
make format
```

### リリース

`VERSION` を更新して変更をmainへcommitした後、次のスクリプトを実行します。
lint・format・test・ブランチ・作業ツリーを検証してから、タグとGitHub Releaseを公開します。

```sh
scripts/release.sh 0.1.0
```

## ライセンス

[MIT](LICENSE)
