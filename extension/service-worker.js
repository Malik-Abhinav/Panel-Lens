"use strict";
const API_ROOT = "http://127.0.0.1:8765/v1";
const IMAGE_FETCH_TIMEOUT_MS = 30000;
const TRANSLATION_TIMEOUT_MS = 180000;
const MAX_IMAGE_BYTES = 16 * 1024 * 1024;
const acquisitions = new Map();
const translations = new Map();

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  route(message).then(sendResponse).catch((error) => sendResponse({
    status: "error", image_id: message.payload?.imageId,
    error: { code: "extension_request_failed", message: error.message }
  }));
  return true;
});

async function route(message) {
  if (message.type === "PANELLENS_MODEL_SETTINGS") return localFetch("/translation/settings");
  if (message.type === "PANELLENS_SET_MODEL") {
    const tabs = await chrome.tabs.query({});
    await Promise.allSettled(tabs.map(tab => chrome.tabs.sendMessage(tab.id, { type: "PANELLENS_SET_ENABLED", enabled: false })));
    return localFetch("/translation/settings", { method: "POST", body: JSON.stringify(message.settings) });
  }
  if (message.type === "PANELLENS_HEALTH") return localFetch("/health");
  if (message.type === "PANELLENS_CLEAR") return localFetch("/sessions/clear", { method: "POST", body: "{}" });
  if (message.type === "PANELLENS_CONTROL") {
    return localFetch(`/requests/${encodeURIComponent(message.requestId)}/${message.action}`, {
      method: "POST", body: JSON.stringify({ priority: message.priority })
    });
  }
  if (message.type === "PANELLENS_TRANSLATE") return translateImage(message.payload);
  throw new Error(`Unsupported extension message: ${message.type}`);
}

async function translateImage(payload) {
  const acquired = await acquire(payload);
  const series = documentTitle(payload.pageTitle);
  const context = Array.isArray(payload.context) ? payload.context : [];
  const operationKey = `${acquired.contentHash}:${series}:${JSON.stringify(context)}`;
  let operation = translations.get(operationKey);
  const coalesced = Boolean(operation);
  if (operation) {
    perf(payload, "cache_hit", {
      cache_level: "content_inflight", duplicate_ocr_avoided: true,
      duplicate_translation_avoided: true
    });
  } else {
    operation = localFetch("/images/translate", {
      method: "POST",
      pageOrigin: payload.pageOrigin,
      body: JSON.stringify({
        request_id: payload.requestId,
        session_id: payload.sessionId,
        image_id: payload.imageId,
        element_id: payload.elementId,
        resource_version: payload.resourceVersion,
        content_hash: acquired.contentHash,
        image_base64: acquired.base64,
        priority: payload.priority,
        series,
        context
      })
    }, TRANSLATION_TIMEOUT_MS).finally(() => translations.delete(operationKey));
    translations.set(operationKey, operation);
  }
  const response = {
    ...(await operation),
    image_id: payload.imageId,
    browser_metrics: {
      duplicate_acquisition_avoided: Boolean(acquired.duplicateAcquisitionAvoided),
      duplicate_ocr_avoided: coalesced,
      duplicate_translation_avoided: coalesced
    }
  };
  if (response.content_hash && response.content_hash !== acquired.contentHash) {
    throw new Error("Local response content identity did not match acquired bytes");
  }
  response.content_hash = acquired.contentHash;
  return response;
}

async function acquire(payload) {
  const hint = payload.url;
  if (acquisitions.has(hint)) {
    perf(payload, "cache_hit", { cache_level: "acquisition_promise", duplicate_acquisition_avoided: true });
    return { ...(await acquisitions.get(hint)), duplicateAcquisitionAvoided: true };
  }
  const operation = acquireOnce(payload).finally(() => acquisitions.delete(hint));
  acquisitions.set(hint, operation);
  return operation;
}

async function acquireOnce(payload) {
  const started = performance.now();
  perf(payload, "acquisition_start");
  const response = await fetchWithTimeout(payload.url, {
    cache: "force-cache", credentials: "include", redirect: "follow"
  }, IMAGE_FETCH_TIMEOUT_MS);
  if (!response.ok) throw new Error(`Image fetch returned HTTP ${response.status}`);
  const blob = await response.blob();
  if (!blob.type.startsWith("image/")) throw new Error(`Unsupported resource type: ${blob.type || "unknown"}`);
  if (blob.size > MAX_IMAGE_BYTES) throw new Error("Image exceeds the 16 MiB acquisition limit");
  const bytes = new Uint8Array(await blob.arrayBuffer());
  const contentHash = await sha256(bytes);
  perf(payload, "acquisition_end", {
    duration_ms: performance.now() - started, byte_size: bytes.byteLength,
    content_identity: contentHash
  });
  return { base64: bytesToBase64(bytes), contentHash, byteSize: bytes.byteLength };
}

async function localFetch(path, options = {}, timeoutMs = 30000) {
  const { panelLensToken = "" } = await chrome.storage.local.get("panelLensToken");
  if (!panelLensToken) throw new Error("Open the PanelLens Mac app, copy its connection key, and connect here.");
  let response;
  try { response = await fetchWithTimeout(`${API_ROOT}${path}`, {
    method: options.method || "GET", cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      "X-PanelLens-Token": panelLensToken,
      "X-PanelLens-Page-Origin": options.pageOrigin || ""
    },
    body: options.body
  }, timeoutMs); } catch (error) {
    throw new Error(`Cannot reach the PanelLens Mac app. Open it and check Browser Setup & Models. ${error.message}`);
  }
  if (response.status === 401) throw new Error("Connection key rejected. Copy the key from the PanelLens Mac app and reconnect.");
  const body = await response.json();
  if (path === "/health" && body.protocol_version !== 1) {
    throw new Error("PanelLens extension and Mac engine are incompatible. Install the matching app release, then reload the extension.");
  }
  if (!response.ok && body.status !== "error") throw new Error(`PanelLens returned HTTP ${response.status}`);
  return body;
}

async function fetchWithTimeout(url, options, timeoutMs) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try { return await fetch(url, { ...options, signal: controller.signal }); }
  catch (error) {
    if (error.name === "AbortError") throw new Error(`Timed out after ${Math.round(timeoutMs / 1000)} seconds`);
    throw error;
  } finally { clearTimeout(timeout); }
}

async function sha256(bytes) {
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
  return [...digest].map((value) => value.toString(16).padStart(2, "0")).join("");
}

function bytesToBase64(bytes) {
  const chunks = [];
  for (let offset = 0; offset < bytes.length; offset += 32768) {
    chunks.push(String.fromCharCode(...bytes.subarray(offset, offset + 32768)));
  }
  return btoa(chunks.join(""));
}

function documentTitle(value) { return typeof value === "string" ? value.slice(0, 200) : ""; }

function perf(payload, stage, fields = {}) {
  if (!payload?.telemetryEnabled) return;
  console.debug("[PanelLens performance]", JSON.stringify({
    schema_version: 1, timestamp_ms: performance.timeOrigin + performance.now(),
    monotonic_ms: performance.now(), stage, request_id: payload.requestId || "",
    image_id: payload.imageId || "", priority: payload.priority || "unknown",
    cache_level: fields.cache_level || "none", model_state: "unknown", ...fields
  }));
}
