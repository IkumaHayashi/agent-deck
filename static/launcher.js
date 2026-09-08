// 新規作成への往復だけを同じdocument内で扱う。iframeと元の画面を保持し、
// ターミナル・プロンプトの入力やスクロール位置を遷移のたびに失わない。
(() => {
  const app = document.querySelector(".app");
  if (!app) return;
  const originalUrl = location.pathname + location.search;
  const originalTitle = document.title;
  let pane = document.querySelector(".launcher-pane");
  let backButton;
  let opener;

  function ensurePane() {
    if (!pane) {
      pane = document.createElement("main");
      pane.className = "launcher-pane route-only";
      const heading = document.createElement("header");
      heading.className = "pane-heading";
      const title = document.createElement("h1");
      title.textContent = "新規セッション";
      heading.append(title);
      const frame = document.createElement("iframe");
      frame.className = "launcher-frame";
      frame.title = "＋ 新規セッションを開始";
      frame.src = "/new?embedded=1&lang=" + encodeURIComponent(document.documentElement.lang);
      pane.append(heading, frame);
      app.append(pane);
    }
    if (!backButton) {
      backButton = document.createElement("button");
      backButton.type = "button";
      backButton.className = "launcher-back";
      backButton.textContent = "← 元の画面へ";
      backButton.addEventListener("click", () => history.back());
      pane.querySelector(".pane-heading").prepend(backButton);
    }
    return pane;
  }

  function renderRoute() {
    const open = location.pathname === "/new";
    if (open) ensurePane();
    document.body.classList.toggle("launcher-open", open);
    app.querySelectorAll(":scope > main:not(.launcher-pane)").forEach(main => {
      main.inert = open;
    });
    document.title = open ? "新規セッション - Agent Deck" : originalTitle;
    if (open) backButton.focus({preventScroll: true});
    else if (opener && opener.isConnected) opener.focus({preventScroll: true});
  }

  function launcherLink(event) {
    const link = event.target.closest("a[href]");
    if (!link || link.hasAttribute("download") || (link.target && link.target !== "_self")) return null;
    const url = new URL(link.href, location.href);
    if (url.origin !== location.origin || url.hash || url.search) return null;
    if (url.pathname === "/new" || (url.pathname === "/" && link.classList.contains("new-link"))) return link;
    return null;
  }

  // captureで既存の全画面ローディングより先に処理する。
  document.addEventListener("click", event => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = launcherLink(event);
    if (!link) return;
    event.preventDefault();
    if (location.pathname !== "/new") {
      opener = link;
      ensurePane();
      history.pushState(null, "", "/new");
      renderRoute();
    }
  }, true);
  // マウス・キーボードで選び始めた時点で軽量なフォームを読み込む。
  document.addEventListener("pointerover", event => { if (launcherLink(event)) ensurePane(); });
  document.addEventListener("focusin", event => { if (launcherLink(event)) ensurePane(); });
  window.addEventListener("popstate", () => {
    if (location.pathname === "/new" || location.pathname + location.search === originalUrl) renderRoute();
  });
})();
