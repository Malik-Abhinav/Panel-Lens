const toggle = document.querySelector("#toggle");
const status = document.querySelector("#status");
const processingMode = document.querySelector("#processing-mode");
const glossary = document.querySelector("#glossary");
const sessionToken = document.querySelector("#session-token");
const provider = document.querySelector("#translation-provider");
const model = document.querySelector("#translation-model");
const modelStatus = document.querySelector("#model-status");
const connectionStatus = document.querySelector("#connection-status");
let tabId, enabled = false, engineReady = false;

const send = message => chrome.runtime.sendMessage(message);
initialize().catch(error => { connectionStatus.textContent = error.message; });

async function initialize() {
  const stored = await chrome.storage.local.get("panelLensToken");
  sessionToken.value = stored.panelLensToken || "";
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  tabId = tab?.id;
  await connect();
  setInterval(refresh, 2000);
}

async function connect() {
  const selection = await send({ type: "PANELLENS_MODEL_SETTINGS" });
  if (selection.settings) {
    provider.value = selection.settings.provider;
    model.value = selection.settings.model;
    document.querySelector("#installed-models").replaceChildren(...selection.settings.ollama_models.map(name => {
      const option = document.createElement("option"); option.value = name; return option;
    }));
  }
  await refresh();
}

document.querySelector("#connect").addEventListener("click", async () => {
  await chrome.storage.local.set({ panelLensToken: sessionToken.value.trim() });
  connectionStatus.textContent = "Connecting…";
  await connect();
});

async function refresh() {
  try {
    const health = await send({ type: "PANELLENS_HEALTH" });
    engineReady = health.status === "ok" && Boolean(health.runtime?.ready);
    connectionStatus.textContent = health.status === "ok" ? "Connected to the PanelLens Mac app." : health.error?.message || "Open the PanelLens Mac app to connect.";
    if (health.runtime) modelStatus.textContent = health.runtime.message;
    toggle.disabled = !tabId || (!enabled && !engineReady);
    try { update(await chrome.tabs.sendMessage(tabId, { type: "PANELLENS_STATUS" })); }
    catch (_) { status.textContent = "Open a comic page, then start reading here."; }
  } catch (error) {
    engineReady = false;
    connectionStatus.textContent = error.message;
    toggle.disabled = !enabled;
  }
}

toggle.addEventListener("click", async () => {
  try {
    if (!enabled) {
      // Keep this permission request directly within the user's click gesture.
      const granted = await chrome.permissions.request({ origins: ["http://*/*", "https://*/*"] });
      if (!granted) { status.textContent = "Reader access was not granted."; return; }
      try { await chrome.tabs.sendMessage(tabId, { type: "PANELLENS_STATUS" }); }
      catch (_) {
        await chrome.scripting.insertCSS({ target: { tabId }, files: ["overlay.css"] });
        await chrome.scripting.executeScript({ target: { tabId }, files: ["prefetch-core.js", "content-script.js"] });
      }
    }
    update(await chrome.tabs.sendMessage(tabId, { type: "PANELLENS_SET_ENABLED", enabled: !enabled }));
  } catch (error) { status.textContent = `This page cannot be read by the extension. Use Screen Capture Fallback in the Mac app. ${error.message}`; }
});

processingMode.addEventListener("change", async () => {
  try { update(await chrome.tabs.sendMessage(tabId, { type: "PANELLENS_SET_MODE", mode: processingMode.value })); }
  catch (_) { status.textContent = "Start reading this page before changing its processing mode."; }
});

glossary.addEventListener("change", async () => {
  const entries = glossary.value.split("\n").map(line => line.split("=", 2).map(part => part.trim()))
    .filter(([korean, english]) => korean && english).slice(0, 8).map(([korean, english]) => ({ korean, english }));
  try { update(await chrome.tabs.sendMessage(tabId, { type: "PANELLENS_SET_GLOSSARY", entries })); }
  catch (_) { status.textContent = "Start reading this page before editing its glossary."; }
});

function update(page) {
  enabled = Boolean(page.enabled);
  processingMode.value = page.mode || "balanced";
  if (document.activeElement !== glossary) glossary.value = (page.glossary || []).map(item => `${item.korean}=${item.english}`).join("\n");
  toggle.textContent = enabled ? "Stop on this page" : "Start on this page";
  toggle.disabled = !tabId || (!enabled && !engineReady);
  const summary = `${page.candidates} images · ${page.ready} translated · ${page.processing} processing · ${page.errors} errors`;
  status.textContent = page.pausedError ? `${summary}\n${page.pausedError}\nStart again after resolving this.` :
    page.candidates === 0 && enabled ? "No readable comic images found. Try scrolling, or use Screen Capture Fallback in the Mac app." : summary;
}

document.querySelector("#apply-model").addEventListener("click", async () => {
  if (!model.value.trim()) { modelStatus.textContent = "Choose a model installed in Ollama first."; return; }
  const response = await send({ type: "PANELLENS_SET_MODEL", settings: { provider: "ollama", model: model.value.trim() } });
  modelStatus.textContent = response.error?.message || response.settings?.runtime.message || "Model selected.";
  if (!response.error) await refresh();
});
