const settingsForm = document.getElementById("settings-form");
const settingsStatus = document.getElementById("settings-status");
const settingsSave = document.getElementById("settings-save");
const restarting = document.getElementById("settings-restarting");
let settingsDirty = false;

function selectSettingsSection(name) {
  document.querySelectorAll("[data-settings-tab]").forEach(button => {
    button.classList.toggle("active", button.dataset.settingsTab === name);
  });
  document.querySelectorAll("[data-settings-section]").forEach(section => {
    section.classList.toggle("active", section.dataset.settingsSection === name);
  });
  history.replaceState(null, "", "#" + name);
}

document.querySelectorAll("[data-settings-tab]").forEach(button => {
  button.addEventListener("click", () => selectSettingsSection(button.dataset.settingsTab));
});

const initialSection = location.hash.slice(1);
if (document.querySelector(`[data-settings-section="${CSS.escape(initialSection)}"]`)) {
  selectSettingsSection(initialSection);
}

document.querySelectorAll("[data-add-row]").forEach(button => {
  button.addEventListener("click", () => {
    const prefix = button.dataset.addRow;
    const container = document.querySelector(`[data-project-rows="${prefix}"]`);
    const row = document.createElement("div");
    row.className = "project-setting-row";
    row.dataset.projectRow = "";
    row.innerHTML = `<input name="${prefix}_label" placeholder="表示名" aria-label="表示名">
      <input name="${prefix}_path" placeholder="~/projects/my-app" aria-label="プロジェクトのパス">
      <button type="button" class="remove-row" data-remove-row aria-label="削除" title="削除">×</button>`;
    container.append(row);
    row.querySelector("input").focus();
    settingsDirty = true;
  });
});

document.addEventListener("click", event => {
  const button = event.target.closest("[data-remove-row]");
  if (!button) return;
  const row = button.closest("[data-project-row]");
  const container = row.parentElement;
  if (container.querySelectorAll("[data-project-row]").length === 1) {
    row.querySelectorAll("input").forEach(input => input.value = "");
  } else {
    row.remove();
  }
  settingsDirty = true;
});

settingsForm.addEventListener("input", () => settingsDirty = true);
window.addEventListener("beforeunload", event => {
  if (!settingsDirty) return;
  event.preventDefault();
});

async function waitForRestart() {
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 500));
    try {
      const response = await fetch("/api/sidebar", {cache: "no-store"});
      if (!response.ok) continue;
      const data = await response.json();
      if (data.boot && data.boot !== bootId) {
        settingsDirty = false;
        location.reload();
        return;
      }
    } catch (error) { /* 再起動中の接続失敗は想定内 */ }
  }
  restarting.querySelector("strong").textContent = "再接続できませんでした";
  restarting.querySelector("small").textContent = "ポートや許可ネットワークを変更した場合は、新しいURLで開いてください。";
}

settingsForm.addEventListener("submit", async event => {
  event.preventDefault();
  settingsSave.disabled = true;
  settingsStatus.className = "";
  settingsStatus.textContent = "保存中…";
  try {
    const body = new URLSearchParams(new FormData(settingsForm));
    const response = await fetch("/api/settings", {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Agent-Deck-Request": "settings",
      },
      body,
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "設定を保存できませんでした");
    settingsStatus.textContent = "保存しました";
    restarting.hidden = false;
    waitForRestart();
  } catch (error) {
    settingsStatus.className = "error";
    settingsStatus.textContent = error.message;
    settingsSave.disabled = false;
  }
});
