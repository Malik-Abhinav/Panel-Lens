const API_ROOT = "http://127.0.0.1:8765/v1";
const IMAGE_FETCH_TIMEOUT_MS = 30000;
const TRANSLATION_TIMEOUT_MS = 120000;

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type === "PANELLENS_HEALTH") {
    fetch(`${API_ROOT}/health`, { cache: "no-store" })
      .then(readJson)
      .then(sendResponse)
      .catch((error) => sendResponse({ status: "error", error: { message: error.message } }));
    return true;
  }

  if (message.type === "PANELLENS_CLEAR") {
    fetch(`${API_ROOT}/sessions/clear`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}"
    })
      .then(readJson)
      .then(sendResponse)
      .catch((error) => sendResponse({ status: "error", error: { message: error.message } }));
    return true;
  }

  if (message.type === "PANELLENS_TRANSLATE") {
    translateImage(message.payload)
      .then(sendResponse)
      .catch((error) => sendResponse({
        status: "error",
        image_id: message.payload.imageId,
        error: { code: "extension_fetch_failed", message: error.message }
      }));
    return true;
  }

  if (message.type === "PANELLENS_TRANSLATE_BATCH") {
    translateImageBatch(message.payload)
      .then(sendResponse)
      .catch((error) => sendResponse({
        status: "error",
        error: { code: "extension_fetch_failed", message: error.message }
      }));
    return true;
  }
});

async function translateImage(payload) {
  const imageResponse = await fetchWithTimeout(
    payload.url,
    { cache: "force-cache", credentials: "include" },
    IMAGE_FETCH_TIMEOUT_MS
  );
  if (!imageResponse.ok) throw new Error(`Image fetch returned HTTP ${imageResponse.status}`);
  const imageBlob = await imageResponse.blob();
  const imageBase64 = await blobToBase64(imageBlob);
  const response = await fetchWithTimeout(
    `${API_ROOT}/images/translate`,
    {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        request_id: payload.requestId,
        session_id: payload.sessionId,
        image_id: payload.imageId,
        image_base64: imageBase64,
        priority: payload.priority,
        series: documentTitle(payload.pageTitle),
        context: Array.isArray(payload.context) ? payload.context : []
      })
    },
    TRANSLATION_TIMEOUT_MS
  );
  return readJson(response);
}

async function translateImageBatch(payload) {
  const images = await Promise.all(payload.images.map(async (image) => ({
    image_id: image.imageId,
    image_base64: await fetchImageBase64(image.url)
  })));
  const response = await fetchWithTimeout(
    `${API_ROOT}/images/translate-batch`,
    {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        request_id: payload.requestId,
        session_id: payload.sessionId,
        images,
        priority: payload.priority,
        series: documentTitle(payload.pageTitle),
        context: Array.isArray(payload.context) ? payload.context : [],
        max_image_width: payload.maxImageWidth
      })
    },
    TRANSLATION_TIMEOUT_MS
  );
  return readJson(response);
}

async function fetchImageBase64(url) {
  const response = await fetchWithTimeout(
    url,
    { cache: "force-cache", credentials: "include" },
    IMAGE_FETCH_TIMEOUT_MS
  );
  if (!response.ok) throw new Error(`Image fetch returned HTTP ${response.status}`);
  return blobToBase64(await response.blob());
}

async function fetchWithTimeout(url, options, timeoutMs) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error(`Timed out after ${Math.round(timeoutMs / 1000)} seconds`);
    }
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

async function readJson(response) {
  const body = await response.json();
  if (!response.ok && body.status !== "error") {
    throw new Error(`PanelLens returned HTTP ${response.status}`);
  }
  return body;
}

async function blobToBase64(blob) {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  const chunks = [];
  for (let offset = 0; offset < bytes.length; offset += 32768) {
    chunks.push(String.fromCharCode(...bytes.subarray(offset, offset + 32768)));
  }
  return btoa(chunks.join(""));
}

function documentTitle(value) {
  return typeof value === "string" ? value.slice(0, 200) : "";
}
