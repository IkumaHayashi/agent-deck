// 閲覧中のページ（GitHub issue / Chatwork 等）を Agent Deck の /new に送るブックマークレット。
// 登録は /new ページ下部の「📎 Agent Deckに送る」リンクをブックマークバーへ
// ドラッグするのが確実（サーバーの実 URL が焼き込まれた版が配信される）。
// 手動登録する場合は下の YOUR_SERVER:8787 を自分のサーバーに置き換えて、
// ブックマーク編集画面の URL 欄に貼り付けること。
// アドレスバーへの貼り付けは Chrome が javascript: を削るため使えない。
// iOS Safari でも同じ手順で動く（サーバーに届くネットワークに接続時のみ）。
javascript:(function(){var s=String(getSelection()||"").trim();var p="以下のページを確認して対応してください。\n"+location.href+"\nタイトル: "+document.title;if(s){p+="\n\n選択テキスト:\n"+s.slice(0,6000);}window.open("http://YOUR_SERVER:8787/new?prompt="+encodeURIComponent(p),"_blank");})();
