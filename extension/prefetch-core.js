(function expose(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.PanelLensPrefetchCore = api;
})(typeof globalThis === "object" ? globalThis : this, () => {
  "use strict";

  const PRIORITY = Object.freeze({ P0: 0, P1: 1, P2: 2, P3: 3 });
  const DEFAULT_POLICY = Object.freeze({
    expectedProcessingMs: 7500,
    lookAhead: 3,
    maxQueued: 4,
    maxTotalWork: 4,
    maxSpeculative: 2,
    maxImageBytes: 16 * 1024 * 1024,
    maxQueuedBytes: 32 * 1024 * 1024,
    slowVelocityPxPerSecond: 120
  });

  function fnv1a(value) {
    let hash = 2166136261;
    for (let index = 0; index < value.length; index += 1) {
      hash ^= value.charCodeAt(index);
      hash = Math.imul(hash, 16777619);
    }
    return (hash >>> 0).toString(16).padStart(8, "0");
  }

  function createImageRecord(snapshot) {
    const source = snapshot.currentSrc || snapshot.src || "";
    return {
      id: `${snapshot.documentId}:${snapshot.elementId}:r0`,
      documentId: snapshot.documentId,
      elementId: snapshot.elementId,
      resourceVersion: 0,
      source,
      sourceHint: fnv1a(source),
      currentSrc: snapshot.currentSrc || "",
      intrinsicWidth: snapshot.naturalWidth || 0,
      intrinsicHeight: snapshot.naturalHeight || 0,
      displayedWidth: snapshot.width || 0,
      displayedHeight: snapshot.height || 0,
      naturalTop: snapshot.top || 0,
      naturalLeft: snapshot.left || 0,
      domOrder: snapshot.domOrder || 0,
      contentIdentity: null,
      status: snapshot.complete ? "idle" : "waiting_resource",
      priority: "P3",
      requestId: null,
      result: null,
      byteSize: 0,
      discoveredAt: snapshot.now || 0,
      viewportEnteredAt: null,
      completedAt: null,
      renderedAt: null,
      generation: 0
    };
  }

  function updateImageRecord(record, snapshot) {
    const source = snapshot.currentSrc || snapshot.src || "";
    const resourceChanged = Boolean(source && source !== record.source);
    if (resourceChanged) {
      record.resourceVersion += 1;
      record.id = `${record.documentId}:${record.elementId}:r${record.resourceVersion}`;
      record.source = source;
      record.sourceHint = fnv1a(source);
      record.contentIdentity = null;
      record.status = snapshot.complete ? "idle" : "waiting_resource";
      record.requestId = null;
      record.result = null;
      record.completedAt = null;
      record.renderedAt = null;
      record.generation += 1;
    } else if (snapshot.complete && record.status === "waiting_resource") {
      record.status = "idle";
    }
    record.currentSrc = snapshot.currentSrc || "";
    record.intrinsicWidth = snapshot.naturalWidth || 0;
    record.intrinsicHeight = snapshot.naturalHeight || 0;
    record.displayedWidth = snapshot.width || 0;
    record.displayedHeight = snapshot.height || 0;
    record.naturalTop = snapshot.top || 0;
    record.naturalLeft = snapshot.left || 0;
    record.domOrder = snapshot.domOrder || record.domOrder;
    return resourceChanged;
  }

  function isCandidate(record, ancestorExcluded = false) {
    if (!record.source || record.source.startsWith("blob:")) return false;
    if (ancestorExcluded) return false;
    if (record.intrinsicWidth < 500 || record.intrinsicHeight < 500) return false;
    if (record.displayedWidth < 280 || record.displayedHeight < 300) return false;
    const ratio = record.intrinsicWidth / Math.max(1, record.intrinsicHeight);
    return ratio >= 0.2 && ratio <= 3.5;
  }

  function orderCandidates(records) {
    return [...records].sort((a, b) => {
      const vertical = a.naturalTop - b.naturalTop;
      if (Math.abs(vertical) > Math.min(a.displayedHeight, b.displayedHeight) * 0.25) {
        return vertical;
      }
      const horizontal = a.naturalLeft - b.naturalLeft;
      if (Math.abs(horizontal) > 1) return horizontal;
      return a.domOrder - b.domOrder || a.id.localeCompare(b.id);
    });
  }

  class MotionTracker {
    constructor(alpha = 0.35) {
      this.alpha = alpha;
      this.sample = null;
      this.velocity = 0;
      this.direction = "none";
    }

    update(position, nowMs) {
      if (this.sample) {
        const elapsed = Math.max(1, nowMs - this.sample.at);
        const instantaneous = ((position - this.sample.position) * 1000) / elapsed;
        this.velocity = this.velocity * (1 - this.alpha) + instantaneous * this.alpha;
        this.direction = Math.abs(this.velocity) < 10 ? "none" : this.velocity > 0 ? "down" : "up";
      }
      this.sample = { position, at: nowMs };
      return { velocity: this.velocity, direction: this.direction };
    }
  }

  function distanceToViewport(record, viewport) {
    const top = record.naturalTop - viewport.scrollY;
    const bottom = top + record.displayedHeight;
    if (bottom > 0 && top < viewport.height) return 0;
    return top >= viewport.height ? top - viewport.height : Math.min(-0.001, bottom);
  }

  function plan(records, viewport, motion, policy = DEFAULT_POLICY) {
    const ordered = orderCandidates(records);
    const visible = ordered.filter((record) => distanceToViewport(record, viewport) === 0);
    const visibleIds = new Set(visible.map((record) => record.id));
    const direction = motion.direction === "up" ? -1 : 1;
    const speed = Math.max(Math.abs(motion.velocity), policy.slowVelocityPxPerSecond);
    const candidates = ordered.map((record) => {
      const distance = distanceToViewport(record, viewport);
      const inDirection = direction > 0 ? distance >= 0 : distance <= 0;
      const timeToViewportMs = distance === 0 ? 0 : inDirection
        ? (Math.abs(distance) / speed) * 1000
        : Infinity;
      return { record, distance, inDirection, timeToViewportMs };
    });
    const ahead = candidates
      .filter((item) => item.distance !== 0 && item.inDirection)
      .sort((a, b) => Math.abs(a.distance) - Math.abs(b.distance));
    const aheadRank = new Map(ahead.map((item, index) => [item.record.id, index]));

    return candidates.map((item) => {
      let priority = "P3";
      const rank = aheadRank.get(item.record.id);
      if (visibleIds.has(item.record.id)) priority = "P0";
      else if (rank === 0) priority = "P1";
      else if (rank < policy.lookAhead && item.timeToViewportMs >= policy.expectedProcessingMs) priority = "P2";
      return {
        ...item,
        priority,
        estimatedProcessingMs: policy.expectedProcessingMs,
        eligible: priority !== "P3" && !["ready", "empty", "processing"].includes(item.record.status)
      };
    }).sort((a, b) => PRIORITY[a.priority] - PRIORITY[b.priority] || Math.abs(a.distance) - Math.abs(b.distance));
  }

  class BoundedQueue {
    constructor(policy = DEFAULT_POLICY) {
      this.policy = policy;
      this.items = new Map();
    }

    upsert(item) {
      const existing = this.items.get(item.record.id);
      if (existing) {
        existing.priority = item.priority;
        existing.distance = item.distance;
        return { accepted: true, reprioritized: true };
      }
      const speculative = [...this.items.values()].filter((value) => value.priority !== "P0").length;
      const bytes = [...this.items.values()].reduce((sum, value) => sum + (value.record.byteSize || 0), 0);
      if (this.items.size >= this.policy.maxQueued) return { accepted: false, reason: "job_limit" };
      if (item.priority !== "P0" && speculative >= this.policy.maxSpeculative) return { accepted: false, reason: "speculative_limit" };
      if ((item.record.byteSize || 0) > this.policy.maxImageBytes) return { accepted: false, reason: "image_bytes" };
      if (bytes + (item.record.byteSize || 0) > this.policy.maxQueuedBytes) return { accepted: false, reason: "queued_bytes" };
      this.items.set(item.record.id, item);
      return { accepted: true, reprioritized: false };
    }

    remove(id) { return this.items.delete(id); }

    ordered() {
      return [...this.items.values()].sort((a, b) => PRIORITY[a.priority] - PRIORITY[b.priority] || Math.abs(a.distance) - Math.abs(b.distance));
    }
  }

  class TraceRecorder {
    constructor({ sessionId = "", documentId = "", clock = () => performance.now() } = {}) {
      this.sessionId = sessionId;
      this.documentId = documentId;
      this.clock = clock;
      this.events = [];
    }

    record(stage, record = {}, fields = {}) {
      const event = {
        schema_version: 1,
        source: "live browser + live local ML service",
        monotonic_ms: this.clock(),
        session_id: this.sessionId,
        document_id: this.documentId,
        element_id: record.elementId || "",
        resource_id: record.id || "",
        content_hash: record.contentIdentity || fields.content_identity || null,
        stage,
        ...fields
      };
      delete event.ocr_text;
      delete event.translation_text;
      delete event.image_bytes;
      this.events.push(event);
      return event;
    }

    serialize() {
      return JSON.stringify({
        schema_version: 1,
        source: "recorded-browser-trace replay",
        session_id: this.sessionId,
        document_id: this.documentId,
        events: this.events
      });
    }
  }

  function replayTrace(serialized) {
    const trace = typeof serialized === "string" ? JSON.parse(serialized) : serialized;
    if (trace?.schema_version !== 1 || !Array.isArray(trace.events)) throw new Error("Unsupported trace format");
    let previous = -Infinity;
    return trace.events.map((event) => {
      if (!Number.isFinite(event.monotonic_ms) || event.monotonic_ms < previous) {
        throw new Error("Trace timestamps must be monotonic");
      }
      previous = event.monotonic_ms;
      return { ...event, replay_source: "recorded-browser-trace replay" };
    });
  }

  function visibleUntranslatedTime(firstViewportEntryMs, resultReadyMs) {
    if (!Number.isFinite(firstViewportEntryMs) || !Number.isFinite(resultReadyMs)) return null;
    return Math.max(0, resultReadyMs - firstViewportEntryMs);
  }

  function vutSummary(samples) {
    const valid = samples.filter((value) => Number.isFinite(value)).sort((a, b) => a - b);
    const percentile = (fraction) => valid.length ? valid[Math.ceil(fraction * valid.length) - 1] : null;
    return {
      count: valid.length,
      p50_ms: percentile(0.5), p90_ms: percentile(0.9), p95_ms: percentile(0.95),
      zero_percent: valid.length ? (valid.filter((value) => value === 0).length * 100) / valid.length : null
    };
  }

  function engineIdentity(health) {
    if (health?.status !== "ok" || !health.runtime?.ready) return null;
    const identity = health.translation_identity || {};
    return JSON.stringify([health.browser_bridge?.session || "", identity.runtime || "", identity.model || health.runtime.model, identity.model_version || "", identity.prompt_version || ""]);
  }

  return {
    engineIdentity,
    PRIORITY,
    DEFAULT_POLICY,
    fnv1a,
    createImageRecord,
    updateImageRecord,
    isCandidate,
    orderCandidates,
    MotionTracker,
    distanceToViewport,
    plan,
    BoundedQueue,
    TraceRecorder,
    replayTrace,
    visibleUntranslatedTime,
    vutSummary
  };
});
