(() => {
  "use strict";
  const Core = globalThis.PanelLensPrefetchCore;
  const policy = { ...Core.DEFAULT_POLICY };
  const state = {
    enabled: false,
    mode: "balanced",
    glossary: [],
    documentId: crypto.randomUUID(),
    navigationKey: `${location.origin}${location.pathname}${location.search}`,
    nextElementId: 1,
    elementIds: new WeakMap(),
    records: new Map(),
    recordsById: new Map(),
    candidates: [],
    active: new Map(),
    resultCache: new Map(),
    completedOrder: [],
    queue: new Core.BoundedQueue(policy),
    motion: new Core.MotionTracker(),
    mutationObserver: null,
    intersectionObserver: null,
    resizeObserver: null,
    scheduleTimer: null,
    engineTimer: null,
    engineIdentity: null,
    enginePollPending: false,
    layoutPending: false,
    pausedError: null,
    metrics: {
      viewportEntries: 0, prefetchHits: 0, p1Hits: 0, stale: 0,
      cancelled: 0, discarded: 0, duplicateAcquisition: 0,
      duplicateOcr: 0, duplicateTranslation: 0, acquiredBytes: 0,
      decodedBytesOwned: 0, decodedMemoryMeasurable: false, maxQueueDepth: 0
    }
  };
  const telemetryEnabled = localStorage.getItem("panellensPerformance") === "1";
  const trace = telemetryEnabled ? new Core.TraceRecorder({
    sessionId: state.documentId, documentId: state.documentId
  }) : null;
  document.addEventListener("panellens:export-trace", () => {
    document.documentElement.dataset.panellensTrace = trace?.serialize() || "";
  });

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message.type === "PANELLENS_SET_ENABLED") {
      setEnabled(Boolean(message.enabled)).then(() => sendResponse(snapshot()));
      return true;
    }
    if (message.type === "PANELLENS_SET_MODE") {
      state.mode = ["eco", "balanced"].includes(message.mode) ? message.mode : "balanced";
      policy.lookAhead = state.mode === "eco" ? 2 : 3;
      requestSchedule(true);
      sendResponse(snapshot());
      return;
    }
    if (message.type === "PANELLENS_SET_GLOSSARY") {
      state.glossary = Array.isArray(message.entries) ? message.entries.slice(0, 8) : [];
      sendResponse(snapshot());
      return;
    }
    if (message.type === "PANELLENS_STATUS") sendResponse(snapshot());
    if (message.type === "PANELLENS_EXPORT_TRACE") sendResponse(trace ? trace.serialize() : null);
  });
  if (location.hostname === "127.0.0.1" && new URLSearchParams(location.search).get("panellensLive") === "1") {
    setEnabled(true);
  }

  async function setEnabled(enabled) {
    if (enabled === state.enabled) return;
    state.enabled = enabled;
    if (!enabled) return stop();
    state.pausedError = null;
    const health = await chrome.runtime.sendMessage({ type: "PANELLENS_HEALTH" }).catch(error => ({ error: { message: error.message } }));
    if (!state.enabled) return;
    state.engineIdentity = Core.engineIdentity(health);
    if (!state.engineIdentity) {
      pauseForEngine(health.error?.message || health.runtime?.message || "Open the PanelLens Mac app and connect the extension.");
      return;
    }
    state.engineTimer = setInterval(checkEngine, 5000);
    discoverInitial();
    observeLifecycle();
    sampleViewport();
    requestSchedule(true);
  }

  function pauseForEngine(message) {
    state.enabled = false;
    stop();
    state.pausedError = message;
  }

  async function checkEngine() {
    if (!state.enabled || state.enginePollPending) return;
    state.enginePollPending = true;
    try {
      const health = await chrome.runtime.sendMessage({ type: "PANELLENS_HEALTH" });
      if (!state.enabled) return;
      const identity = Core.engineIdentity(health);
      if (!identity) pauseForEngine(health.error?.message || health.runtime?.message || "PanelLens engine disconnected.");
      else if (identity !== state.engineIdentity) pauseForEngine("The engine or translation model changed. Start this page again to refresh its translations.");
    } catch (_) {
      if (state.enabled) pauseForEngine("PanelLens engine disconnected. Reopen the Mac app and reconnect.");
    } finally { state.enginePollPending = false; }
  }

  function elementId(image) {
    if (!state.elementIds.has(image)) state.elementIds.set(image, `e${state.nextElementId++}`);
    return state.elementIds.get(image);
  }

  function imageSnapshot(image, domOrder = 0) {
    const rect = image.getBoundingClientRect();
    return {
      documentId: state.documentId,
      elementId: elementId(image),
      src: image.src,
      currentSrc: image.currentSrc,
      naturalWidth: image.naturalWidth,
      naturalHeight: image.naturalHeight,
      width: rect.width,
      height: rect.height,
      top: rect.top + scrollY,
      left: rect.left + scrollX,
      complete: image.complete && image.naturalWidth > 0,
      domOrder,
      now: performance.now()
    };
  }

  function consider(image, domOrder = state.records.size, deferRebuild = false) {
    if (!(image instanceof HTMLImageElement)) return;
    let record = state.records.get(image);
    if (!record) {
      record = Core.createImageRecord(imageSnapshot(image, domOrder));
      record.element = image;
      record.layer = null;
      record.visible = false;
      record.everProcessed = false;
      record.resourceReadyVersion = -1;
      state.records.set(image, record);
      state.recordsById.set(record.id, record);
      perf("discover", record, dimensions(record));
      image.addEventListener("load", onImageLoad, { passive: true });
      state.intersectionObserver?.observe(image);
      state.resizeObserver?.observe(image);
    } else {
      const oldId = record.id;
      const snapshot = imageSnapshot(image, record.domOrder);
      const nextSource = snapshot.currentSrc || snapshot.src || "";
      if (nextSource && nextSource !== record.source) cancelRecord(record, "resource_changed");
      const changed = Core.updateImageRecord(record, snapshot);
      if (changed) {
        state.recordsById.delete(oldId);
        state.recordsById.set(record.id, record);
        record.layer?.remove();
        record.layer = null;
      }
    }
    const excluded = Boolean(image.closest("header,nav,footer,aside,[role='banner'],[role='navigation'],[class*='avatar' i],[class*='icon' i],[class*='advert' i],[id*='advert' i]"));
    record.candidate = Core.isCandidate(record, excluded);
    if (!deferRebuild) rebuildCandidates();
    if (record.status === "idle" && record.resourceReadyVersion !== record.resourceVersion) {
      record.resourceReadyVersion = record.resourceVersion;
      perf("resource_ready", record, dimensions(record));
    }
  }

  function discoverInitial() {
    [...document.images].forEach((image, index) => consider(image, index, true));
    rebuildCandidates();
  }

  function discoverNode(node) {
    if (!(node instanceof Element)) return;
    if (node instanceof HTMLImageElement) consider(node);
    node.querySelectorAll?.("img").forEach((image) => consider(image));
  }

  function rebuildCandidates() {
    state.candidates = Core.orderCandidates(
      [...state.records.values()].filter((record) => record.candidate && record.element.isConnected)
    );
  }

  function onImageLoad(event) {
    consider(event.currentTarget);
    requestSchedule(true);
  }

  function observeLifecycle() {
    state.intersectionObserver = new IntersectionObserver(onIntersections, { threshold: [0, 0.01] });
    state.resizeObserver = new ResizeObserver((entries) => {
      for (const entry of entries) consider(entry.target, state.records.get(entry.target)?.domOrder, true);
      rebuildCandidates();
      requestLayout();
    });
    for (const record of state.records.values()) {
      state.intersectionObserver.observe(record.element);
      state.resizeObserver.observe(record.element);
    }
    state.mutationObserver = new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        if (mutation.type === "attributes") consider(mutation.target);
        for (const node of mutation.addedNodes) discoverNode(node);
        for (const node of mutation.removedNodes) removeDisconnected(node);
      }
      requestSchedule(true);
    });
    state.mutationObserver.observe(document.documentElement, {
      subtree: true, childList: true, attributes: true,
      attributeFilter: ["src", "srcset", "sizes"]
    });
    addEventListener("scroll", onViewportChange, { passive: true });
    addEventListener("resize", onViewportChange, { passive: true });
  }

  function removeDisconnected(node) {
    for (const [image, record] of state.records) {
      if ((image === node || (node instanceof Element && node.contains(image))) && !image.isConnected) {
        cancelRecord(record, "element_removed");
        record.layer?.remove();
        state.records.delete(image);
        state.recordsById.delete(record.id);
      }
    }
    rebuildCandidates();
  }

  function onViewportChange() {
    sampleViewport();
    requestLayout();
    requestSchedule(false);
  }

  function sampleViewport() {
    const motion = state.motion.update(scrollY, performance.now());
    perf("viewport_motion", null, {
      scroll_velocity: Math.round(motion.velocity), scroll_direction: motion.direction
    });
  }

  function onIntersections(entries) {
    for (const entry of entries) {
      const record = state.records.get(entry.target);
      if (!record) continue;
      const entering = entry.isIntersecting && !record.visible;
      record.visible = entry.isIntersecting;
      if (!entering) continue;
      record.viewportEnteredAt = performance.now();
      state.metrics.viewportEntries += 1;
      const hit = ["ready", "empty"].includes(record.status);
      if (hit) state.metrics.prefetchHits += 1;
      if (hit && record.completedPriority === "P1") state.metrics.p1Hits += 1;
      perf("viewport_enter", record, {
        priority: "P0", prefetch_hit: hit,
        distance_to_viewport: 0,
        scroll_velocity: Math.round(state.motion.velocity),
        scroll_direction: state.motion.direction
      });
      perf(hit ? "cache_hit" : "cache_miss", record, {
        cache_level: "element_result", priority: "P0", prefetch_hit: hit
      });
      if (record.status === "ready") render(record, true);
    }
    requestSchedule(true);
  }

  function requestSchedule(immediate) {
    if (!state.enabled || state.pausedError || state.scheduleTimer) return;
    state.scheduleTimer = setTimeout(() => {
      state.scheduleTimer = null;
      schedule();
    }, immediate ? 0 : 100);
  }

  function refreshGeometry() {
    [...state.records.values()].forEach((record, index) => consider(record.element, index, true));
    rebuildCandidates();
  }

  function schedule() {
    if (!state.enabled) return;
    resetAfterNavigation();
    refreshGeometry();
    const viewport = { scrollY, height: innerHeight };
    const planned = Core.plan(state.candidates, viewport, state.motion, policy);
    const plannedIds = new Set(planned.filter((item) => item.priority !== "P3").map((item) => item.record.id));

    for (const [id, active] of state.active) {
      const item = planned.find((candidate) => candidate.record.id === id);
      const newPriority = item?.priority || "P3";
      if (newPriority !== active.priority) {
        active.priority = newPriority;
        perf("queue_enter", active.record, { priority: newPriority, reprioritized: true, queue_depth: state.active.size });
        chrome.runtime.sendMessage({ type: "PANELLENS_CONTROL", action: "reprioritize", requestId: active.requestId, priority: newPriority });
      }
      if (!plannedIds.has(id) && newPriority === "P3") cancelRecord(active.record, "viewport_obsolete");
    }

    for (const item of planned) {
      item.record.priority = item.priority;
      if (!item.eligible) continue;
      if (!state.queue.items.has(item.record.id) && state.queue.items.size + state.active.size >= policy.maxTotalWork) continue;
      const outcome = state.queue.upsert(item);
      if (!outcome.accepted) continue;
      if (!outcome.reprioritized) perf("queue_enter", item.record, {
        priority: item.priority, distance_to_viewport: Math.round(item.distance),
        estimated_time_to_viewport: Number.isFinite(item.timeToViewportMs) ? Math.round(item.timeToViewportMs) : null,
        estimated_processing_time: policy.expectedProcessingMs
      });
    }
    state.metrics.maxQueueDepth = Math.max(state.metrics.maxQueueDepth, state.queue.items.size + state.active.size);
    launchAvailable();
  }

  function launchAvailable() {
    const maxInFlight = state.mode === "eco" ? 1 : 3;
    while (state.active.size < maxInFlight) {
      const item = state.queue.ordered()[0];
      if (!item) break;
      state.queue.remove(item.record.id);
      processRecord(item).catch((error) => failRecord(item.record, error));
    }
  }

  async function processRecord(item) {
    const record = item.record;
    if (!record.element.isConnected || record.status !== "idle") return;
    const recordId = record.id;
    const generation = record.generation;
    const requestId = crypto.randomUUID();
    record.requestId = requestId;
    record.status = "processing";
    state.active.set(recordId, { record, requestId, priority: item.priority });
    perf("queue_exit", record, {
      priority: item.priority, queue_depth: state.queue.items.size,
      distance_to_viewport: Math.round(item.distance),
      estimated_time_to_viewport: Number.isFinite(item.timeToViewportMs) ? Math.round(item.timeToViewportMs) : null,
      estimated_processing_time: policy.expectedProcessingMs,
      scroll_velocity: Math.round(state.motion.velocity), scroll_direction: state.motion.direction
    });
    const response = await chrome.runtime.sendMessage({
      type: "PANELLENS_TRANSLATE",
      payload: {
        requestId, sessionId: state.documentId, imageId: record.id,
        elementId: record.elementId, resourceVersion: record.resourceVersion,
        url: record.source, priority: item.priority, pageOrigin: location.origin,
        pageTitle: document.title, context: previousContext(record), telemetryEnabled
      }
    });
    state.active.delete(recordId);
    if (!state.enabled || !record.element.isConnected || record.generation !== generation || response?.image_id !== record.id) {
      state.metrics.discarded += 1;
      perf("result_discarded", record, { reason: "identity_mismatch" });
      launchAvailable();
      return;
    }
    if (response?.status !== "ok") {
      if (response?.error?.code === "cancelled") record.status = "idle";
      else failRecord(record, new Error(response?.error?.message || "Translation failed"));
      launchAvailable();
      return;
    }
    record.contentIdentity = response.content_hash || response.performance?.content_hash || null;
    record.byteSize = response.received_image_bytes || 0;
    state.metrics.duplicateAcquisition += Number(Boolean(response.browser_metrics?.duplicate_acquisition_avoided));
    state.metrics.duplicateOcr += Number(Boolean(response.browser_metrics?.duplicate_ocr_avoided));
    state.metrics.duplicateTranslation += Number(Boolean(response.browser_metrics?.duplicate_translation_avoided));
    state.metrics.acquiredBytes += record.byteSize;
    record.result = response;
    record.completedAt = performance.now();
    record.completedPriority = item.priority;
    record.status = response.regions?.length ? "ready" : "empty";
    record.everProcessed = true;
    perf("result_ready", record, {
      cache_level: response.cache_sources?.translation || "miss",
      vut_ms: Core.visibleUntranslatedTime(record.viewportEnteredAt, record.completedAt)
    });
    storeCompleted(record, response);
    perf("cache_store", record, { cache_level: "element_result", priority: item.priority });
    if (record.visible && record.status === "ready") render(record, false);
    launchAvailable();
    requestSchedule(false);
  }

  function storeCompleted(record, response) {
    if (record.contentIdentity) {
      state.resultCache.delete(record.contentIdentity);
      state.resultCache.set(record.contentIdentity, response);
    }
    state.completedOrder = state.completedOrder.filter((id) => id !== record.id);
    state.completedOrder.push(record.id);
    while (state.completedOrder.length > 12) {
      const evictedId = state.completedOrder.shift();
      const evicted = state.recordsById.get(evictedId);
      if (!evicted || evicted.visible || evicted.id === record.id) continue;
      if (evicted.contentIdentity) state.resultCache.delete(evicted.contentIdentity);
      evicted.layer?.remove(); evicted.layer = null;
      evicted.result = null; evicted.status = "idle"; evicted.completedAt = null;
    }
  }

  function cancelRecord(record, reason) {
    state.queue.remove(record.id);
    const active = state.active.get(record.id);
    if (!active || active.cancelRequested) return;
    active.cancelRequested = true;
    state.metrics.cancelled += 1;
    perf("cancel_requested", record, { reason, priority: active.priority });
    chrome.runtime.sendMessage({ type: "PANELLENS_CONTROL", action: "cancel", requestId: active.requestId });
  }

  function failRecord(record, error) {
    for (const [id, active] of state.active) if (active.record === record) state.active.delete(id);
    record.status = "error";
    record.error = error.message;
    state.pausedError = error.message;
    publish("error", { imageId: record.id, message: error.message });
    publish("fallback-required", { imageId: record.id, acquisition: "ScreenCaptureKit" });
  }

  function render(record, prefetchHit) {
    if (!record.visible || !record.element.isConnected || record.status !== "ready") return;
    const started = performance.now();
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
    positionLayer(record);
    perf("overlay_layout", record, { duration_ms: performance.now() - started });
    requestAnimationFrame(() => {
      record.renderedAt = performance.now();
      perf("result_rendered", record, {
        duration_ms: record.viewportEnteredAt ? record.renderedAt - record.viewportEnteredAt : 0,
        prefetch_hit: prefetchHit
      });
      perf("first_paint", record, { duration_ms: performance.now() - started });
    });
  }

  function requestLayout() {
    if (state.layoutPending) return;
    state.layoutPending = true;
    requestAnimationFrame(() => {
      state.layoutPending = false;
      for (const record of state.records.values()) if (record.layer) positionLayer(record);
    });
  }

  function positionLayer(record) {
    const rect = record.element.getBoundingClientRect();
    record.layer.style.transform = `translate(${rect.left + scrollX}px,${rect.top + scrollY}px)`;
    record.layer.style.width = `${rect.width}px`;
    record.layer.style.height = `${rect.height}px`;
    const scaleX = rect.width / Math.max(1, record.intrinsicWidth);
    const scaleY = rect.height / Math.max(1, record.intrinsicHeight);
    for (const card of record.layer.children) {
      const [x, y, width, height] = card.dataset.bbox.split(",").map(Number);
      Object.assign(card.style, { left: `${x * scaleX}px`, top: `${y * scaleY}px`, width: `${width * scaleX}px`, minHeight: `${height * scaleY}px` });
    }
  }

  function previousContext(record) {
    const index = state.candidates.indexOf(record);
    const pairs = [];
    for (const previous of state.candidates.slice(Math.max(0, index - 5), index)) {
      for (const region of previous.result?.regions || []) {
        if (region.original && region.translation) pairs.push({ korean: region.original, english: region.translation });
      }
    }
    return [...pairs.slice(-Math.max(0, 20 - state.glossary.length)), ...state.glossary.map((item) => ({ korean: `[glossary] ${item.korean}`, english: item.english }))];
  }

  function resetAfterNavigation() {
    const key = `${location.origin}${location.pathname}${location.search}`;
    if (key === state.navigationKey) return;
    stop();
    state.navigationKey = key;
    state.documentId = crypto.randomUUID();
    discoverInitial();
    observeLifecycle();
    sampleViewport();
  }

  function stop() {
    clearInterval(state.engineTimer);
    state.engineTimer = null;
    clearTimeout(state.scheduleTimer);
    state.scheduleTimer = null;
    state.mutationObserver?.disconnect();
    state.intersectionObserver?.disconnect();
    state.resizeObserver?.disconnect();
    removeEventListener("scroll", onViewportChange);
    removeEventListener("resize", onViewportChange);
    for (const record of state.records.values()) {
      cancelRecord(record, "extension_stopped");
      record.layer?.remove();
    }
    state.records.clear(); state.recordsById.clear(); state.candidates = [];
    state.queue = new Core.BoundedQueue(policy); state.active.clear();
    state.resultCache.clear(); state.completedOrder = [];
    chrome.runtime.sendMessage({ type: "PANELLENS_CLEAR" }).catch(() => {});
  }

  function dimensions(record) {
    return { image_width: record.intrinsicWidth, image_height: record.intrinsicHeight, displayed_width: Math.round(record.displayedWidth), displayed_height: Math.round(record.displayedHeight) };
  }

  function snapshot() {
    const values = [...state.records.values()];
    return {
      enabled: state.enabled, mode: state.mode, glossary: state.glossary,
      pausedError: state.pausedError, candidates: state.candidates.length,
      ready: values.filter((record) => record.status === "ready").length,
      empty: values.filter((record) => record.status === "empty").length,
      processing: state.active.size, errors: values.filter((record) => record.status === "error").length,
      queued: state.queue.items.size, metrics: { ...state.metrics,
        prefetchHitRate: state.metrics.viewportEntries ? state.metrics.prefetchHits / state.metrics.viewportEntries : 0 }
    };
  }

  function publish(event, detail) {
    console.debug("[PanelLens browser]", event, detail);
    document.dispatchEvent(new CustomEvent("panellens:metric", { detail: { event, ...detail } }));
  }

  function perf(stage, record, fields = {}) {
    if (!telemetryEnabled) return;
    const detail = {
      schema_version: 1, timestamp_ms: performance.timeOrigin + performance.now(),
      monotonic_ms: performance.now(), stage,
      request_id: record?.requestId || "", image_id: record?.id || "",
      document_id: record?.documentId || state.documentId,
      element_id: record?.elementId || "", content_identity: record?.contentIdentity || "",
      priority: fields.priority || record?.priority || "unknown",
      cache_level: fields.cache_level || "none", model_state: fields.model_state || "unknown",
      queue_depth: state.queue.items.size + state.active.size, ...fields
    };
    trace?.record(stage, record || {}, detail);
    console.debug("[PanelLens performance]", JSON.stringify(detail));
    document.dispatchEvent(new CustomEvent("panellens:performance", { detail }));
  }
})();
