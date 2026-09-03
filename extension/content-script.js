(() => {
  const state = {
    enabled: false,
    mode: "balanced",
    glossary: [],
    sessionId: `${location.origin}${location.pathname}${location.search}`,
    candidates: [],
    records: new Map(),
    activeImageId: null,
    pausedError: null,
    schedulePending: false,
    layoutPending: false,
    observer: null
  };

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message.type === "PANELLENS_SET_ENABLED") {
      setEnabled(Boolean(message.enabled)).then(() => sendResponse(snapshot()));
      return true;
    }
    if (message.type === "PANELLENS_SET_MODE") {
      state.mode = ["eco", "balanced", "full"].includes(message.mode)
        ? message.mode
        : "balanced";
      publish("mode", { mode: state.mode });
      schedule();
      sendResponse(snapshot());
      return;
    }
    if (message.type === "PANELLENS_SET_GLOSSARY") {
      state.glossary = Array.isArray(message.entries)
        ? message.entries.slice(0, 8)
        : [];
      publish("glossary", { entries: state.glossary.length });
      sendResponse(snapshot());
      return;
    }
    if (message.type === "PANELLENS_STATUS") sendResponse(snapshot());
  });

  async function setEnabled(enabled) {
    if (enabled === state.enabled) return;
    state.enabled = enabled;
    if (enabled) {
      state.pausedError = null;
      discover();
      markReachedPanels();
      observePage();
      schedule();
    } else {
      stop();
    }
  }

  function discover() {
    resetAfterNavigation();
    const previous = new Set(state.candidates);
    state.candidates = [...document.images]
      .filter(isComicCandidate)
      .sort((a, b) => documentOrder(a, b));

    for (const image of state.candidates) {
      if (!state.records.has(image)) {
        state.records.set(image, {
          id: stableImageId(image),
          status: "idle",
          result: null,
          layer: null,
          completedAt: null,
          reachedAt: null
        });
      }
      previous.delete(image);
    }
    for (const removed of previous) removeRecord(removed);
    publish("candidates", { count: state.candidates.length });
  }

  function resetAfterNavigation() {
    const currentSession = `${location.origin}${location.pathname}${location.search}`;
    if (currentSession === state.sessionId) return;
    for (const record of state.records.values()) record.layer?.remove();
    state.records.clear();
    state.candidates = [];
    state.activeImageId = null;
    state.sessionId = currentSession;
    chrome.runtime.sendMessage({ type: "PANELLENS_CLEAR" });
    publish("navigation", { sessionId: currentSession });
  }

  function isComicCandidate(image) {
    const rect = image.getBoundingClientRect();
    const source = image.currentSrc || image.src;
    if (!source || source.startsWith("blob:")) return false;
    if (!image.complete || image.naturalWidth < 500 || image.naturalHeight < 500) return false;
    if (rect.width < Math.min(300, innerWidth * 0.45) || rect.height < 300) return false;
    const excluded = image.closest("header, nav, footer, aside, [role='banner'], [role='navigation'], [class*='avatar' i], [class*='icon' i], [class*='advert' i], [id*='advert' i]");
    return !excluded;
  }

  function documentOrder(a, b) {
    if (a === b) return 0;
    return a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
  }

  function stableImageId(image) {
    const source = image.currentSrc || image.src;
    let hash = 2166136261;
    for (let index = 0; index < source.length; index += 1) {
      hash ^= source.charCodeAt(index);
      hash = Math.imul(hash, 16777619);
    }
    return `image-${(hash >>> 0).toString(16)}`;
  }

  function observePage() {
    state.observer = new MutationObserver(() => {
      discover();
      schedule();
    });
    state.observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ["src", "srcset"] });
    addEventListener("scroll", onViewportChange, { passive: true });
    addEventListener("resize", onViewportChange, { passive: true });
  }

  function onViewportChange() {
    if (!state.enabled) return;
    if (!state.layoutPending) {
      state.layoutPending = true;
      requestAnimationFrame(() => {
        state.layoutPending = false;
        positionLayers();
        markReachedPanels();
      });
    }
    schedule();
  }

  function schedule() {
    if (state.schedulePending || !state.enabled || state.pausedError) return;
    state.schedulePending = true;
    queueMicrotask(async () => {
      state.schedulePending = false;
      if (state.activeImageId) return;
      discover();
      const target = schedulingTarget();
      if (!target) return;
      const batchSize = state.mode === "eco" ? 1 : 3;
      const next = target.images
        .filter((image) => state.records.get(image)?.status === "idle")
        .slice(0, batchSize);
      if (!next.length) return;
      await translateBatch(next, target.visible);
      schedule();
    });
  }

  function schedulingTarget() {
    const visibleIndexes = [];
    state.candidates.forEach((image, index) => {
      const rect = image.getBoundingClientRect();
      if (rect.bottom > 0 && rect.top < innerHeight) visibleIndexes.push(index);
    });
    if (state.mode === "full") {
      if (visibleIndexes.length === 0) {
        return { images: [...state.candidates], visible: new Set() };
      }
      const first = visibleIndexes[0];
      const last = visibleIndexes[visibleIndexes.length - 1];
      const visibleImages = state.candidates.slice(first, last + 1);
      return {
        images: [
          ...visibleImages,
          ...state.candidates.slice(last + 1),
          ...state.candidates.slice(0, first)
        ],
        visible: new Set(visibleImages)
      };
    }
    if (visibleIndexes.length > 0) {
      const first = visibleIndexes[0];
      const last = visibleIndexes[visibleIndexes.length - 1];
      const visible = new Set(state.candidates.slice(first, last + 1));
      const lookahead = state.mode === "eco" ? 2 : 4;
      return {
        images: state.candidates.slice(first, last + lookahead + 1),
        visible
      };
    }

    const upcoming = state.candidates.findIndex((image) => image.getBoundingClientRect().top >= innerHeight);
    if (upcoming < 0) return null;
    const lookahead = state.mode === "eco" ? 3 : 5;
    const images = state.candidates.slice(upcoming, upcoming + lookahead);
    return { images, visible: new Set([images[0]]) };
  }

  async function translateBatch(images, visible) {
    const records = images.map((image) => state.records.get(image)).filter(Boolean);
    for (const record of records) record.status = "processing";
    state.activeImageId = records.map((record) => record.id).join(",");
    const priority = images.some((image) => visible.has(image)) ? "visible" : "prefetch";
    publish("processing-batch", { imageIds: records.map((record) => record.id), priority, mode: state.mode });
    const startedAt = performance.now();
    let result;
    try {
      result = await chrome.runtime.sendMessage({
        type: "PANELLENS_TRANSLATE_BATCH",
        payload: {
          requestId: crypto.randomUUID(),
          sessionId: state.sessionId,
          images: images.map((image) => ({
            imageId: state.records.get(image).id,
            url: image.currentSrc || image.src
          })),
          priority,
          pageTitle: document.title,
          context: previousContext(images[0]),
          maxImageWidth: state.mode === "eco" ? 1400 : 1800
        }
      });
    } catch (error) {
      result = { status: "error", error: { message: error.message } };
    } finally {
      state.activeImageId = null;
    }

    if (!state.enabled) return;
    if (result?.status !== "ok" || !Array.isArray(result.images)) {
      const message = result?.error?.message || "Batch translation failed";
      for (const record of records) {
        if (record.status === "processing") {
          record.status = "error";
          record.error = message;
        }
      }
      state.pausedError = message;
      publish("error", { imageIds: records.map((record) => record.id), message });
      return;
    }

    for (const image of images) {
      if (!state.records.has(image)) continue;
      const record = state.records.get(image);
      const imageResult = result.images.find((item) => item.image_id === record.id);
      if (!imageResult) {
        record.status = "error";
        record.error = "Batch response omitted this image";
        state.pausedError = record.error;
        continue;
      }
      acceptTranslationResult(
        image,
        record,
        {
          ...imageResult,
          translation_processing_time_ms: result.translation_processing_time_ms
        },
        visible.has(image) ? "visible" : "prefetch",
        Math.round(performance.now() - startedAt)
      );
    }
  }

  function acceptTranslationResult(image, record, result, priority, processingMs) {
    record.result = result;
    record.completedAt = performance.now();
    record.status = result.regions?.length ? "ready" : "empty";
    if (record.status === "ready") render(image, record);
    const ahead = image.getBoundingClientRect().top >= innerHeight;
    publish(record.status === "ready" ? "ready" : "no-text", {
      imageId: record.id,
      priority,
      ahead,
      processingMs,
      ocrMs: result.ocr_processing_time_ms,
      translationMs: result.translation_processing_time_ms,
      detectedText: result.detected_text_count,
      filteredText: result.filtered_text_count,
      regions: result.regions.length,
      regionTypes: (result.regions || []).map((region) => region.region_type || "unknown")
    });
  }

  function previousContext(image) {
    const imageIndex = state.candidates.indexOf(image);
    const context = [];
    for (const previous of state.candidates.slice(0, imageIndex)) {
      const record = state.records.get(previous);
      if (record?.status !== "ready") continue;
      for (const region of record.result.regions || []) {
        if (region.original && region.translation) {
          context.push({ korean: region.original, english: region.translation });
        }
      }
    }
    const unique = [];
    const seen = new Set();
    for (const item of context.reverse()) {
      const key = `${item.korean}\u0000${item.english}`;
      if (seen.has(key)) continue;
      seen.add(key);
      unique.push(item);
      if (unique.length === Math.max(0, 20 - state.glossary.length)) break;
    }
    const glossaryContext = state.glossary.map((item) => ({
      korean: `[glossary] ${item.korean}`,
      english: item.english
    }));
    return [...unique.reverse(), ...glossaryContext];
  }

  function render(image, record) {
    record.layer?.remove();
    const layer = document.createElement("div");
    layer.className = "panellens-layer";
    layer.dataset.panellensImage = record.id;
    for (const region of record.result.regions || []) {
      if (!Array.isArray(region.bbox) || region.bbox.length !== 4) continue;
      const card = document.createElement("div");
      card.className = "panellens-card";
      card.textContent = region.translation || "";
      card.dataset.bbox = region.bbox.join(",");
      layer.appendChild(card);
    }
    document.documentElement.appendChild(layer);
    record.layer = layer;
    positionLayer(image, record);
  }

  function positionLayers() {
    for (const image of state.candidates) {
      const record = state.records.get(image);
      if (record?.layer) positionLayer(image, record);
    }
  }

  function positionLayer(image, record) {
    const rect = image.getBoundingClientRect();
    const layer = record.layer;
    layer.style.transform = `translate(${rect.left + scrollX}px, ${rect.top + scrollY}px)`;
    layer.style.width = `${rect.width}px`;
    layer.style.height = `${rect.height}px`;
    const scaleX = rect.width / image.naturalWidth;
    const scaleY = rect.height / image.naturalHeight;
    for (const card of layer.children) {
      const [x, y, width, height] = card.dataset.bbox.split(",").map(Number);
      card.style.left = `${x * scaleX}px`;
      card.style.top = `${y * scaleY}px`;
      card.style.width = `${width * scaleX}px`;
      card.style.minHeight = `${height * scaleY}px`;
    }
  }

  function markReachedPanels() {
    for (const image of state.candidates) {
      const record = state.records.get(image);
      if (!record || record.reachedAt || image.getBoundingClientRect().top >= innerHeight) continue;
      record.reachedAt = performance.now();
      if (record.completedAt) publish("cache-visible", { imageId: record.id, renderLatencyMs: 0, readyAheadMs: Math.round(record.reachedAt - record.completedAt) });
    }
  }

  function removeRecord(image) {
    state.records.get(image)?.layer?.remove();
    state.records.delete(image);
  }

  function stop() {
    state.observer?.disconnect();
    state.observer = null;
    removeEventListener("scroll", onViewportChange);
    removeEventListener("resize", onViewportChange);
    for (const record of state.records.values()) record.layer?.remove();
    state.records.clear();
    state.candidates = [];
    state.activeImageId = null;
    state.pausedError = null;
    chrome.runtime.sendMessage({ type: "PANELLENS_CLEAR" });
  }

  function snapshot() {
    const values = [...state.records.values()];
    return {
      enabled: state.enabled,
      mode: state.mode,
      glossary: state.glossary,
      pausedError: state.pausedError,
      candidates: values.length,
      ready: values.filter((record) => record.status === "ready").length,
      empty: values.filter((record) => record.status === "empty").length,
      processing: values.filter((record) => record.status === "processing").length,
      errors: values.filter((record) => record.status === "error").length
    };
  }

  function publish(event, detail) {
    console.debug("[PanelLens spike]", event, detail);
    document.dispatchEvent(new CustomEvent("panellens:metric", { detail: { event, ...detail } }));
  }
})();
