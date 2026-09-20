const toggle = document.querySelector("#toggle");
const cancelProcessing = document.querySelector("#cancel-processing");
const retryErrors = document.querySelector("#retry-errors");
const status = document.querySelector("#status");
const processingMode = document.querySelector("#processing-mode");
const glossary = document.querySelector("#glossary");
const sessionToken = document.querySelector("#session-token");
const provider = document.querySelector("#translation-provider");
const model = document.querySelector("#translation-model");
const modelStatus = document.querySelector("#model-status");
const connectionStatus = document.querySelector("#connection-status");
let tabId, enabled = false, engineReady = false, pageOrigins = [];

const send = message => chrome.runtime.sendMessage(message);
document.querySelector("#extension-version").textContent = `Extension ${chrome.runtime.getManifest().version} · load the folder shown by the Mac app`;
initialize().catch(error => { connectionStatus.textContent = error.message; });

async function initialize() {
  const stored = await chrome.storage.local.get("panelLensToken");
  sessionToken.value = stored.panelLensToken || "";
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  tabId = tab?.id;
  if (tabId) {
    try {
      const [{ result }] = await chrome.scripting.executeScript({ target: { tabId }, func: () => {
        const found = new Set([location.origin]);
        for (const image of document.images) {
          try { found.add(new URL(image.currentSrc || image.src, document.baseURI).origin); } catch (_) { /* ignore invalid URL */ }
          if (found.size >= 12) break;
        }
        return [...found].filter(origin => /^https?:\/\//.test(origin)).map(origin => `${origin}/*`);
      } });
      pageOrigins = result || [];
    } catch (_) {
      try {
        const origin = new URL(tab.url).origin;
        pageOrigins = /^https?:\/\//.test(origin) ? [`${origin}/*`] : [];
      } catch (_) { pageOrigins = []; }
    }
  }
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
      if (!pageOrigins.length) { status.textContent = "Open a normal comic page before starting the reader."; return; }
      const granted = await chrome.permissions.request({ origins: pageOrigins });
      if (!granted) { status.textContent = "Access to this page and its image sites was not granted."; return; }
      try { await chrome.tabs.sendMessage(tabId, { type: "PANELLENS_STATUS" }); }
      catch (_) {
        await chrome.scripting.insertCSS({ target: { tabId }, files: ["overlay.css"] });
        await chrome.scripting.executeScript({ target: { tabId }, files: ["prefetch-core.js", "content-script.js"] });
      }
    }
    update(await chrome.tabs.sendMessage(tabId, { type: "PANELLENS_SET_ENABLED", enabled: !enabled }));
  } catch (error) { status.textContent = `This page cannot be read by the extension. Use Screen Capture Fallback in the Mac app. ${error.message}`; }
});

cancelProcessing.addEventListener("click", async () => {
  try { update(await chrome.tabs.sendMessage(tabId, { type: "PANELLENS_CANCEL_ACTIVE" })); }
  catch (error) { status.textContent = error.message; }
});

retryErrors.addEventListener("click", async () => {
  try { update(await chrome.tabs.sendMessage(tabId, { type: "PANELLENS_RETRY_ERRORS" })); }
  catch (error) { status.textContent = error.message; }
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
  cancelProcessing.disabled = !enabled || !page.processing;
  retryErrors.disabled = !enabled || (!page.errors && !page.pausedError);
  const activeImages = page.processingImages?.length ? ` image ${page.processingImages.join(", ")}` : "";
  const summary = `${page.candidates} images found · ${page.ready} translated · ${page.queued || 0} queued · ${page.processing} processing${activeImages}${page.processing ? ` (${page.processingSeconds || 0}s)` : ""} · ${page.errors} errors`;
  status.textContent = page.pausedError ? `${summary}\n${page.pausedError}\nUse Retry failed images after fixing the cause.` :
    page.candidates === 0 && enabled ? "No readable comic images found. Try scrolling, or use Screen Capture Fallback in the Mac app." :
    enabled && page.candidates > page.ready && !page.processing && !page.queued ? `${summary}\nWaiting for you to scroll to more images.` : summary;
}

document.querySelector("#apply-model").addEventListener("click", async () => {
  if (!model.value.trim()) { modelStatus.textContent = "Choose a model installed in Ollama first."; return; }
  const response = await send({ type: "PANELLENS_SET_MODEL", settings: { provider: "ollama", model: model.value.trim() } });
  modelStatus.textContent = response.error?.message || response.settings?.runtime.message || "Model selected.";
  if (!response.error) await refresh();
});
