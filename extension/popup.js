const toggle = document.querySelector("#toggle");
const status = document.querySelector("#status");
const processingMode = document.querySelector("#processing-mode");
const glossary = document.querySelector("#glossary");
let tabId;
let enabled = false;

initialize();

async function initialize() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  tabId = tab?.id;
  if (!tabId) return show("No active browser tab.");
  const health = await chrome.runtime.sendMessage({ type: "PANELLENS_HEALTH" });
  if (health.status !== "ok") return show(`Local engine unavailable: ${health.error?.message || "unknown error"}`);
  try {
    const page = await chrome.tabs.sendMessage(tabId, { type: "PANELLENS_STATUS" });
    update(page);
    setInterval(refresh, 1000);
  } catch (_error) {
    show("Reload this page after loading the extension.");
    toggle.disabled = true;
  }
}

async function refresh() {
  try {
    update(await chrome.tabs.sendMessage(tabId, { type: "PANELLENS_STATUS" }));
  } catch (_error) {
    // The tab may be navigating while the short-lived popup is open.
  }
}

toggle.addEventListener("click", async () => {
  const page = await chrome.tabs.sendMessage(tabId, { type: "PANELLENS_SET_ENABLED", enabled: !enabled });
  update(page);
});

processingMode.addEventListener("change", async () => {
  const page = await chrome.tabs.sendMessage(tabId, {
    type: "PANELLENS_SET_MODE",
    mode: processingMode.value
  });
  update(page);
});

glossary.addEventListener("change", async () => {
  const entries = glossary.value
    .split("\n")
    .map((line) => line.split("=", 2).map((part) => part.trim()))
    .filter(([korean, english]) => korean && english)
    .slice(0, 8)
    .map(([korean, english]) => ({ korean, english }));
  update(await chrome.tabs.sendMessage(tabId, {
    type: "PANELLENS_SET_GLOSSARY",
    entries
  }));
});

function update(page) {
  enabled = Boolean(page.enabled);
  processingMode.value = page.mode || "balanced";
  if (document.activeElement !== glossary) {
    glossary.value = (page.glossary || [])
      .map((item) => `${item.korean}=${item.english}`)
      .join("\n");
  }
  toggle.textContent = enabled ? "Stop on this page" : "Start on this page";
  const remaining = Math.max(0, page.candidates - page.ready - page.empty - page.errors);
  const summary = `${page.candidates} candidates · ${page.ready} ready · ${page.empty} no text · ${remaining} remaining · ${page.processing} processing · ${page.errors} errors`;
  show(page.pausedError ? `${summary}\nPaused: ${page.pausedError}\nStop and start to retry.` : summary);
}

function show(message) {
  status.textContent = message;
}
