  // ツール選択に応じてモデル選択肢を切り替える
  function currentTool() {
    var t = document.querySelector(".tools input:checked");
    return t ? t.value : "claude";
  }
  var codexModelsLoaded = false;
  var codexModelsLoading = false;
  async function loadCodexModels() {
    if (codexModelsLoaded || codexModelsLoading) return;
    codexModelsLoading = true;
    var status = document.getElementById("model-status");
    status.textContent = "読み込み中...";
    try {
      var response = await fetch("/api/launcher-models?lang=" + encodeURIComponent(document.documentElement.lang));
      var data = await response.json();
      if (!response.ok) throw new Error("モデル一覧を更新できませんでした");
      var target = document.getElementById("models-codex");
      var selected = target.querySelector("input:checked");
      var value = selected ? selected.value : "default";
      // 更新待ちの間に選んだモデルも維持する。
      if (!data.models.some(function (model) { return model.value === value; }) && selected) {
        data.models.push({value: value, label: selected.parentElement.textContent});
      }
      target.replaceChildren();
      data.models.forEach(function (model) {
        var label = document.createElement("label");
        var input = document.createElement("input");
        input.type = "radio"; input.name = "model-codex"; input.value = model.value;
        input.checked = model.value === value;
        var text = document.createElement("span"); text.textContent = model.label;
        label.append(input, text); target.append(label);
      });
      codexModelsLoaded = true;
      status.textContent = "";
    } catch (error) {
      status.textContent = "モデル一覧を更新できませんでした";
    } finally {
      codexModelsLoading = false;
    }
  }
  function syncModels() {
    ["claude", "codex"].forEach(function (t) {
      document.getElementById("models-" + t).style.display = t === currentTool() ? "" : "none";
    });
    document.getElementById("model-status").hidden = currentTool() !== "codex";
    if (currentTool() === "codex") loadCodexModels();
  }
  document.querySelectorAll(".tools input").forEach(function (r) {
    r.addEventListener("change", syncModels);
  });
  syncModels();
  var reviewsLoaded = false;
  var inboxLoaded = false;
  var resumeLoaded = false;
  var resumeLoading = false;
  function activateLauncherPanel(panelId) {
    document.querySelectorAll(".launcher-panel").forEach(function (panel) {
      panel.classList.toggle("active", panel.id === panelId);
    });
    document.querySelectorAll(".launcher-tabs button").forEach(function (button) {
      button.classList.toggle("active", button.dataset.panel === panelId);
    });
    if (panelId === "resume-panel" && !resumeLoaded) loadResume();
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
  // セッション起動（tmux + AIツールの起動）とページ遷移は数秒かかるため、
  // 待ちの間はローディングオーバーレイを出す。画面を覆うので二重送信も防げる
  var navLoading = document.createElement("div");
  navLoading.id = "nav-loading";
  navLoading.hidden = true;
  var navLoadingBox = document.createElement("div");
  navLoadingBox.className = "box";
  var navLoadingSpinner = document.createElement("div");
  navLoadingSpinner.className = "spinner";
  var navLoadingLabel = document.createElement("span");
  navLoadingBox.append(navLoadingSpinner, navLoadingLabel);
  navLoading.appendChild(navLoadingBox);
  document.body.appendChild(navLoading);
  function showNavLoading(message) {
    navLoadingLabel.textContent = message || "読み込み中...";
    navLoading.hidden = false;
  }
  // bfcacheで戻ってきたときは前回のオーバーレイが残るので消す
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) navLoading.hidden = true;
  });
  document.addEventListener("click", function (event) {
    if (event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    var link = event.target.closest("a[href]");
    if (!link || link.target === "_blank" || link.hasAttribute("download")) return;
    var href = link.getAttribute("href");
    if (!href || href.indexOf("#") === 0 || href.indexOf("javascript:") === 0) return;
    if (new URL(link.href, location.href).origin !== location.origin) return;
    showNavLoading("セッション一覧を読み込み中...");
  });
  // 選択中のツール・モデルを各起動フォームに hidden input として付与する。
  // resume フォームはツールが会話側で決まるため、権限だけを引き継ぐ。
  function wireLaunchForm(f) {
    f.addEventListener("submit", function () {
      showNavLoading(f.dataset.resume ? "会話を再開しています..." : "セッションを起動中...");
      var bypass = document.querySelector('.bypass-modes input:checked');
      var fields = [["bypass", bypass && bypass.value === "bypass" ? "1" : "0"]];
      if (!f.dataset.resume) {
        var t = currentTool();
        var m = document.querySelector("#models-" + t + " input:checked");
        fields.push(["model", m ? m.value : "default"], ["tool", t]);
        // PRレビューは入力欄の内容を初期プロンプトとして送らず、差分だけを開く。
        if (!f.elements.namedItem("pull_request")) {
          fields.push(["prompt", document.getElementById("prompt").value]);
        }
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
  var inboxOpen = document.getElementById("inbox-open");
  if (inboxOpen) {
    inboxOpen.addEventListener("click", function () {
      activateLauncherPanel("inbox-panel");
    });
  }
  var inboxBack = document.getElementById("inbox-back");
  if (inboxBack) inboxBack.addEventListener("click", function () {
    activateLauncherPanel("projects-panel");
  });
  // resume IDの一部を入力すると、全グループの候補を絞り込む。
  var resumeIdFilter = document.getElementById("resume-id-filter");
  if (resumeIdFilter) {
    resumeIdFilter.addEventListener("input", filterResume);
  }
  function filterResume() {
      var query = resumeIdFilter.value.trim().toLowerCase();
      var forms = document.querySelectorAll("form[data-resume-id]");
      var matches = 0;
      forms.forEach(function (form) {
        var matched = !query || form.dataset.resumeId.toLowerCase().includes(query);
        form.hidden = !matched;
        if (matched) matches += 1;
      });
      document.querySelectorAll(".resume-group").forEach(function (group) {
        group.hidden = !Array.from(group.querySelectorAll("form[data-resume-id]"))
          .some(function (form) { return !form.hidden; });
        if (query && !group.hidden) group.open = true;
      });
      var empty = document.getElementById("resume-filter-empty");
      if (empty) empty.hidden = !query || matches > 0;
  }
  async function loadResume() {
    if (resumeLoading) return;
    resumeLoading = true;
    var target = document.getElementById("resume-groups");
    var refresh = document.getElementById("resume-refresh");
    refresh.disabled = true;
    target.className = "cw-loading";
    target.textContent = "読み込み中...";
    document.getElementById("resume-filter-empty").hidden = true;
    try {
      var response = await fetch("/api/recent-conversations?lang=" + encodeURIComponent(document.documentElement.lang));
      var data = await response.json();
      if (!response.ok) throw new Error(data.error || "会話の取得に失敗しました");
      // サーバー側で会話の値をHTMLエスケープ済みの同一オリジン断片。
      target.innerHTML = data.html;
      target.className = "";
      target.querySelectorAll("form.launch").forEach(wireLaunchForm);
      resumeLoaded = true;
      filterResume();
    } catch (error) {
      resumeLoaded = false;
      showError(target, error.message);
    } finally {
      resumeLoading = false;
      refresh.disabled = false;
    }
  }
  document.getElementById("resume-refresh").addEventListener("click", loadResume);
  // 最初のプロンプト欄への画像ペースト。アップロードしてパスを本文に差し込む
  var promptBox = document.getElementById("prompt");
  var promptStatus = document.getElementById("prompt-status");
  // 外部ツールからの ?prompt=... でプロンプト欄をプリフィルする
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
  async function uploadLaunchImages(files) {
    try {
      for (var i = 0; i < files.length; i++) await uploadLaunchImage(files[i]);
    } catch (error) { promptStatus.textContent = "❌ " + error.message; }
  }
  // スマホはペーストできないので、📎 から写真・カメラ・ファイルを選ばせる
  var promptImagePicker = document.getElementById("prompt-image-picker");
  document.getElementById("prompt-attach").addEventListener("click", function () {
    promptImagePicker.click();
  });
  promptImagePicker.addEventListener("change", async function () {
    var files = Array.from(promptImagePicker.files || []);
    promptImagePicker.value = "";  // 同じ画像を続けて選び直せるようにする
    if (files.length) await uploadLaunchImages(files);
  });
  promptBox.addEventListener("paste", async function (event) {
    var images = Array.from((event.clipboardData || {}).items || [])
      .filter(function (item) { return item.kind === "file" && item.type.indexOf("image/") === 0; })
      .map(function (item) { return item.getAsFile(); }).filter(Boolean);
    if (!images.length) return;
    event.preventDefault();
    await uploadLaunchImages(images);
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
    var prompt = "以下の Chatwork メッセージに対応してください。\n" + item.url
      + "\n（room_id: " + item.room_id + " / message_id: " + item.message_id
      + "。本文は Chatwork MCP の get_room_message で取得してください）";
    set.addEventListener("click", function () {
      promptBox.value = prompt;
      activateLauncherPanel("projects-panel");
      promptBox.scrollIntoView({behavior: "smooth", block: "center"}); promptBox.focus();
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
  function githubTargetCard(item) {
    var box = document.createElement("div"); box.className = "review-request";
    var title = document.createElement("strong");
    title.textContent = item.repositoryName + "#" + item.number + " " + item.title;
    var meta = document.createElement("small");
    meta.textContent = (item.kind === "issue" ? "Issue" : "Pull Request")
      + " · " + item.state + (item.author && item.author.login ? " · @" + item.author.login : "");
    box.append(title, meta);
    if (item.body) {
      var body = document.createElement("div"); body.className = "github-preview-body";
      body.textContent = item.body.length > 500 ? item.body.slice(0, 500) + "…" : item.body;
      box.appendChild(body);
    }
    if (item.labels && item.labels.length) {
      var labels = document.createElement("div"); labels.className = "github-labels";
      item.labels.forEach(function (label) {
        var chip = document.createElement("span"); chip.textContent = label.name; labels.appendChild(chip);
      });
      box.appendChild(labels);
    }
    var form = document.createElement("form"); form.className = "launch";
    form.method = "post"; form.action = "/launch";
    [["dir", item.cwd], ["github_kind", item.kind], ["github_target", item.url]].forEach(function (pair) {
      var input = document.createElement("input"); input.type = "hidden";
      input.name = pair[0]; input.value = pair[1]; form.appendChild(input);
    });
    var button = document.createElement("button"); button.type = "submit";
    button.textContent = item.kind === "issue" ? "🚀 このIssueから起動" : "🚀 このPRから起動";
    form.appendChild(button); wireLaunchForm(form); box.appendChild(form);
    return box;
  }
  var githubPreviewTimer;
  var githubPreviewController;
  function clearGithubTargetPreview() {
    if (githubPreviewController) githubPreviewController.abort();
    githubPreviewController = null;
    var target = document.getElementById("github-preview");
    target.className = "cw-empty";
    target.textContent = "番号またはURLを入力すると内容を確認できます";
  }
  async function loadGithubTargetPreview() {
    var target = document.getElementById("github-preview");
    var selector = document.getElementById("github-selector").value.trim();
    if (!selector) {
      return clearGithubTargetPreview();
    }
    var dir = document.getElementById("github-project").value;
    if (githubPreviewController) githubPreviewController.abort();
    var controller = new AbortController(); githubPreviewController = controller;
    target.className = "cw-loading"; target.textContent = "GitHubから読み込み中...";
    try {
      var query = new URLSearchParams({dir: dir, target: selector});
      var response = await fetch("/api/github-item?" + query.toString(), {signal: controller.signal});
      var item = await response.json();
      if (controller !== githubPreviewController) return;
      if (!response.ok) throw new Error(item.error || "Issue / PRを取得できませんでした");
      target.className = ""; target.replaceChildren(githubTargetCard(item));
    } catch (error) {
      if (error.name === "AbortError" || controller !== githubPreviewController) return;
      showError(target, error.message);
    }
  }
  document.getElementById("github-target-form").addEventListener("submit", function (event) {
    event.preventDefault(); clearTimeout(githubPreviewTimer); loadGithubTargetPreview();
  });
  document.getElementById("github-selector").addEventListener("input", function () {
    clearTimeout(githubPreviewTimer);
    clearGithubTargetPreview();
    var value = this.value.trim();
    if (!value) return;
    if (/^\d+$/.test(value) || /^https:\/\/github\.com\/.+\/(?:issues|pull)\/\d+\/?$/i.test(value)) {
      githubPreviewTimer = setTimeout(loadGithubTargetPreview, 500);
    }
  });
  document.getElementById("github-project").addEventListener("change", function () {
    if (document.getElementById("github-selector").value.trim()) loadGithubTargetPreview();
  });
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
