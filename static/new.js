  // ツール選択に応じてモデル選択肢を切り替える
  function currentTool() {
    var t = document.querySelector(".tools input:checked");
    return t ? t.value : "claude";
  }
  function syncModels() {
    ["claude", "codex"].forEach(function (t) {
      document.getElementById("models-" + t).style.display = t === currentTool() ? "" : "none";
    });
  }
  document.querySelectorAll(".tools input").forEach(function (r) {
    r.addEventListener("change", syncModels);
  });
  syncModels();
  var reviewsLoaded = false;
  var inboxLoaded = false;
  function activateLauncherPanel(panelId) {
    document.querySelectorAll(".launcher-panel").forEach(function (panel) {
      panel.classList.toggle("active", panel.id === panelId);
    });
    document.querySelectorAll(".launcher-tabs button").forEach(function (button) {
      button.classList.toggle("active", button.dataset.panel === panelId);
    });
    if (panelId === "reviews-panel" && !reviewsLoaded) {
      reviewsLoaded = true; loadReviews(false);
    }
    if (panelId === "inbox-panel" && !inboxLoaded) {
      inboxLoaded = true; loadMentions(false); loadRooms(false);
    }
  }
  document.querySelectorAll(".launcher-tabs button").forEach(function (button) {
    button.addEventListener("click", function () { activateLauncherPanel(button.dataset.panel); });
  });
  // 選択中のツール・モデルを各起動フォームに hidden input として付与する。
  // resume フォームはツールが会話側で決まるため、権限だけを引き継ぐ。
  function wireLaunchForm(f) {
    f.addEventListener("submit", function () {
      var bypass = document.querySelector('.bypass-modes input:checked');
      var fields = [["bypass", bypass && bypass.value === "bypass" ? "1" : "0"]];
      if (!f.dataset.resume) {
        var t = currentTool();
        var m = document.querySelector("#models-" + t + " input:checked");
        var p = document.getElementById("prompt").value;
        fields.push(["model", m ? m.value : "default"], ["tool", t], ["prompt", p]);
      }
      fields.forEach(function (kv) {
        if (f.elements.namedItem(kv[0])) return;
        var h = document.createElement("input");
        h.type = "hidden"; h.name = kv[0]; h.value = kv[1];
        f.appendChild(h);
      });
    });
  }
  document.querySelectorAll("form.launch").forEach(wireLaunchForm);
  // 折りたたまれた9件目以降の再開候補の表示切り替え
  var resumeToggle = document.getElementById("resume-toggle");
  if (resumeToggle) {
    resumeToggle.addEventListener("click", function () {
      var open = document.getElementById("resume-more").classList.toggle("open");
      resumeToggle.textContent = open ? "▴ 折りたたむ" : resumeToggle.dataset.label;
    });
  }
  // 最初のプロンプト欄への画像ペースト。アップロードしてパスを本文に差し込む
  var promptBox = document.getElementById("prompt");
  var promptStatus = document.getElementById("prompt-status");
  // ブックマークレット等からの ?prompt=... でプロンプト欄をプリフィルする
  var prefill = new URLSearchParams(location.search).get("prompt");
  if (prefill) {
    document.getElementById("prompt-details").open = true;
    promptBox.value = prefill.slice(0, 8000);
    promptBox.scrollIntoView({behavior: "smooth", block: "center"});
    promptBox.focus();
  }
  async function uploadLaunchImage(file) {
    if (file.size > 15 * 1024 * 1024) throw new Error("画像は15MBまでです");
    promptStatus.textContent = "画像をアップロード中...";
    var response = await fetch("/api/launch/image", {
      method: "POST",
      headers: {"Content-Type": file.type || "application/octet-stream"},
      body: file,
    });
    var data = await response.json();
    if (!response.ok) throw new Error(data.error || "画像のアップロードに失敗しました");
    var prefix = promptBox.value && !promptBox.value.endsWith("\n") ? "\n" : "";
    promptBox.value += prefix + "添付画像: " + data.path + "\n";
    promptStatus.textContent = "画像を添付しました";
  }
  promptBox.addEventListener("paste", async function (event) {
    var images = Array.from((event.clipboardData || {}).items || [])
      .filter(function (item) { return item.kind === "file" && item.type.indexOf("image/") === 0; })
      .map(function (item) { return item.getAsFile(); }).filter(Boolean);
    if (!images.length) return;
    event.preventDefault();
    try {
      for (var i = 0; i < images.length; i++) await uploadLaunchImage(images[i]);
    } catch (error) { promptStatus.textContent = "❌ " + error.message; }
  });
  function cleanChatwork(body) {
    return (body || "")
      .replace(/\[To:\d+\]/g, "")
      .replace(/\[rp aid=\d+[^\]]*\]/g, "")
      .replace(/\[picon:\d+\]/g, "")
      .replace(/\[qtmeta[^\]]*\]/g, "")
      .replace(/\[hr\]/g, "────────")
      .replace(/\[(?:info|\/info|title|\/title|qt|\/qt|code|\/code)\]/g, "")
      .trim();
  }
  function cwMessage(item) {
    var box = document.createElement("div"); box.className = "cw-message";
    var meta = document.createElement("div"); meta.className = "cw-meta";
    var date = item.send_time ? new Date(item.send_time * 1000).toLocaleString("ja-JP") : "";
    meta.textContent = item.room_name + " · " + item.sender + (date ? " · " + date : "");
    var body = document.createElement("div"); body.className = "cw-body";
    body.textContent = cleanChatwork(item.body);
    var set = document.createElement("button"); set.type = "button"; set.className = "cw-set";
    set.textContent = "📝 プロンプトにセット";
    set.addEventListener("click", function () {
      var prompt = "以下の Chatwork メッセージに対応してください。\n" + item.url
        + "\n（room_id: " + item.room_id + " / message_id: " + item.message_id
        + "。本文は Chatwork MCP の get_room_message で取得してください）";
      var textarea = document.getElementById("prompt"); textarea.value = prompt;
      textarea.scrollIntoView({behavior: "smooth", block: "center"}); textarea.focus();
    });
    box.append(meta, body, set); return box;
  }
  function showError(target, error) {
    target.className = "msg err"; target.textContent = "❌ " + error;
  }
  function reviewCard(item) {
    var box = document.createElement("div"); box.className = "review-request";
    var title = document.createElement("strong");
    title.textContent = item.repositoryName + "#" + item.number + " " + item.title;
    var meta = document.createElement("small");
    meta.textContent = (item.author && item.author.login ? item.author.login + " · " : "")
      + (item.isDraft ? "Draft · " : "") + (item.cwd ? item.cwd : "ローカルプロジェクトなし");
    box.append(title, meta);
    if (item.cwd) {
      var form = document.createElement("form"); form.className = "launch"; form.method = "post"; form.action = "/launch";
      [["dir", item.cwd], ["pull_request", item.url]].forEach(function (pair) {
        var input = document.createElement("input"); input.type = "hidden"; input.name = pair[0]; input.value = pair[1]; form.appendChild(input);
      });
      var button = document.createElement("button"); button.type = "submit"; button.textContent = "🔍 AIとレビュー";
      form.appendChild(button); wireLaunchForm(form); box.appendChild(form);
    }
    return box;
  }
  async function loadReviews(force) {
    var target = document.getElementById("review-requests"); target.className = "cw-loading"; target.textContent = "読み込み中...";
    try {
      var response = await fetch("/api/review-requests" + (force ? "?refresh=1" : ""));
      var data = await response.json(); if (!response.ok) throw new Error(data.error || "取得に失敗しました");
      target.className = ""; target.replaceChildren();
      if (!data.items.length) { target.className = "cw-empty"; target.textContent = "レビュー依頼はありません"; }
      data.items.forEach(function (item) { target.appendChild(reviewCard(item)); });
    } catch (error) { showError(target, error.message); }
  }
  document.getElementById("reviews-refresh").addEventListener("click", function () { loadReviews(true); });
  document.getElementById("specific-review").addEventListener("submit", async function (event) {
    event.preventDefault(); var target = document.getElementById("review-requests");
    try {
      var response = await fetch("/api/pull-request?pr=" + encodeURIComponent(document.getElementById("specific-pr").value));
      var item = await response.json(); if (!response.ok) throw new Error(item.error || "PRを取得できませんでした");
      target.className = ""; target.replaceChildren(reviewCard(item));
    } catch (error) { showError(target, error.message); }
  });
  async function loadMentions(force) {
    var target = document.getElementById("cw-mentions");
    target.className = "cw-loading"; target.textContent = "読み込み中...";
    try {
      var response = await fetch("/api/mentions" + (force ? "?refresh=1" : ""));
      var data = await response.json(); if (!response.ok) throw new Error(data.error || "取得に失敗しました");
      target.className = ""; target.replaceChildren();
      if (!data.items.length) { target.className = "cw-empty"; target.textContent = "メンションはありません"; }
      data.items.forEach(function (item) { target.appendChild(cwMessage(item)); });
    } catch (error) { showError(target, error.message); }
  }
  async function loadRooms(force) {
    var target = document.getElementById("cw-rooms");
    target.className = "cw-loading"; target.textContent = "読み込み中...";
    try {
      var response = await fetch("/api/rooms" + (force ? "?refresh=1" : ""));
      var data = await response.json(); if (!response.ok) throw new Error(data.error || "取得に失敗しました");
      target.className = ""; target.replaceChildren();
      data.items.forEach(function (room) {
        var button = document.createElement("button"); button.type = "button"; button.className = "cw-room";
        button.textContent = room.name; button.addEventListener("click", function () { loadRoom(room.room_id, room.name); });
        target.appendChild(button);
      });
    } catch (error) { showError(target, error.message); }
  }
  async function loadRoom(roomId, roomName) {
    var target = document.getElementById("cw-room-messages");
    target.className = "cw-loading"; target.textContent = "「" + roomName + "」を読み込み中...";
    try {
      var response = await fetch("/api/rooms/" + encodeURIComponent(roomId) + "/messages");
      var data = await response.json(); if (!response.ok) throw new Error(data.error || "取得に失敗しました");
      target.className = ""; target.replaceChildren();
      var heading = document.createElement("h2"); heading.textContent = roomName + " の直近メッセージ"; target.appendChild(heading);
      if (!data.items.length) { var empty = document.createElement("div"); empty.className = "cw-empty"; empty.textContent = "メッセージはありません"; target.appendChild(empty); }
      data.items.forEach(function (item) { target.appendChild(cwMessage(item)); });
      target.scrollIntoView({behavior: "smooth", block: "start"});
    } catch (error) { showError(target, error.message); }
  }
  // Chatwork 連携が無効な場合はパネルごと描画されない
  var cwRefresh = document.getElementById("cw-refresh");
  if (cwRefresh) {
    cwRefresh.addEventListener("click", function () { loadMentions(true); loadRooms(false); });
  }
