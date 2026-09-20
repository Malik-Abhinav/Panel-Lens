"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const Core = require("../prefetch-core.js");

test("trace serialization, replay, and privacy", () => {
  let now = 10;
  const trace = new Core.TraceRecorder({ sessionId: "s", documentId: "d", clock: () => now++ });
  trace.record("discover", { elementId: "e1", id: "r1" }, { image_bytes: "secret", ocr_text: "secret" });
  trace.record("result_ready", { elementId: "e1", id: "r1" });
  const replay = Core.replayTrace(trace.serialize());
  assert.equal(replay.length, 2);
  assert.equal(replay[0].image_bytes, undefined);
  assert.equal(replay[0].replay_source, "recorded-browser-trace replay");
});

test("replay rejects non-monotonic timing", () => {
  assert.throws(() => Core.replayTrace({ schema_version: 1, events: [
    { monotonic_ms: 2 }, { monotonic_ms: 1 }
  ] }), /monotonic/);
});

test("visible untranslated time handles visible and prefetched results", () => {
  assert.equal(Core.visibleUntranslatedTime(100, 350), 250);
  assert.equal(Core.visibleUntranslatedTime(350, 100), 0);
  assert.deepEqual(Core.vutSummary([0, 100, 200, 300]), {
    count: 4, p50_ms: 100, p90_ms: 300, p95_ms: 300, zero_percent: 25
  });
});

function record(id, top, overrides = {}) {
  return {
    id, documentId: "doc", elementId: id, source: `https://example.test/${id}.png`,
    intrinsicWidth: 900, intrinsicHeight: 1400, displayedWidth: 900,
    displayedHeight: 1400, naturalTop: top, naturalLeft: 0, domOrder: Number(id.slice(1)) || 0,
    status: "idle", byteSize: 1024, ...overrides
  };
}

test("image identity binds document, element, and resource version", () => {
  const image = Core.createImageRecord({ documentId: "doc", elementId: "e1", src: "a", complete: true });
  assert.equal(image.id, "doc:e1:r0");
  const changed = Core.updateImageRecord(image, { src: "b", complete: true });
  assert.equal(changed, true);
  assert.equal(image.id, "doc:e1:r1");
  assert.equal(image.contentIdentity, null);
});

test("same URL does not imply same DOM identity", () => {
  const a = Core.createImageRecord({ documentId: "doc", elementId: "e1", src: "same", complete: true });
  const b = Core.createImageRecord({ documentId: "doc", elementId: "e2", src: "same", complete: true });
  assert.notEqual(a.id, b.id);
  assert.equal(a.sourceHint, b.sourceHint);
});

test("candidate filter rejects small, excluded, blob, and extreme images", () => {
  assert.equal(Core.isCandidate(record("e1", 0)), true);
  assert.equal(Core.isCandidate(record("e1", 0, { intrinsicWidth: 100 })), false);
  assert.equal(Core.isCandidate(record("e1", 0), true), false);
  assert.equal(Core.isCandidate(record("e1", 0, { source: "blob:x" })), false);
  assert.equal(Core.isCandidate(record("e1", 0, { intrinsicWidth: 9000, intrinsicHeight: 500 })), false);
});

test("geometry determines reading order before DOM order", () => {
  const ordered = Core.orderCandidates([record("e1", 2000), record("e2", 0), record("e3", 1000)]);
  assert.deepEqual(ordered.map((item) => item.id), ["e2", "e3", "e1"]);
});

test("motion tracker smooths velocity and changes direction", () => {
  const tracker = new Core.MotionTracker(1);
  tracker.update(0, 0);
  assert.equal(tracker.update(500, 500).direction, "down");
  assert.equal(tracker.update(100, 1000).direction, "up");
});

test("planner assigns visible, next, lead-time, and speculative priorities", () => {
  const records = [0, 1400, 2800, 4200, 5600].map((top, index) => record(`e${index}`, top));
  const plan = Core.plan(records, { scrollY: 0, height: 900 }, { velocity: 150, direction: "down" });
  const priorities = Object.fromEntries(plan.map((item) => [item.record.id, item.priority]));
  assert.deepEqual(priorities, { e0: "P0", e1: "P1", e2: "P2", e3: "P2", e4: "P3" });
});

test("reverse scrolling prioritizes geometry above the viewport", () => {
  const records = [0, 1400, 2800].map((top, index) => record(`e${index}`, top));
  const plan = Core.plan(records, { scrollY: 2800, height: 900 }, { velocity: -500, direction: "up" });
  const priorities = Object.fromEntries(plan.map((item) => [item.record.id, item.priority]));
  assert.equal(priorities.e2, "P0");
  assert.equal(priorities.e1, "P1");
  assert.equal(priorities.e0, "P3");
});

test("bounded queue caps jobs, bytes, speculative work, and reprioritizes", () => {
  const queue = new Core.BoundedQueue({ ...Core.DEFAULT_POLICY, maxQueued: 3, maxSpeculative: 2, maxQueuedBytes: 3000 });
  assert.equal(queue.upsert({ record: record("e1", 0), priority: "P2", distance: 1 }).accepted, true);
  assert.equal(queue.upsert({ record: record("e2", 1), priority: "P2", distance: 2 }).accepted, true);
  assert.equal(queue.upsert({ record: record("e3", 2), priority: "P2", distance: 3 }).accepted, false);
  assert.equal(queue.upsert({ record: record("e1", 0), priority: "P0", distance: 0 }).reprioritized, true);
  assert.equal(queue.ordered()[0].priority, "P0");
});

test("normal and rapid viewport replays always rank the latest viewport P0", () => {
  for (const stream of [["A", "B", "C", "D"], ["A", "B", "E", "K", "Q"], ["A", "B", "C", "D", "C", "B"]]) {
    const records = stream.map((id, index) => record(id, index * 1400));
    const current = records.at(-1);
    const plan = Core.plan(records, { scrollY: current.naturalTop, height: 900 }, { velocity: 1000, direction: "down" });
    assert.equal(plan.find((item) => item.record === current).priority, "P0");
  }
});

test("engine identity changes on model selection or engine restart", () => {
  const health = { status: "ok", runtime: { ready: true, model: "b3" }, browser_bridge: { session: "first" }, translation_identity: { model: "b3", model_version: "1" } };
  assert.equal(Core.engineIdentity({ status: "error" }), null);
  assert.notEqual(Core.engineIdentity(health), Core.engineIdentity({ ...health, browser_bridge: { session: "second" } }));
  assert.notEqual(Core.engineIdentity(health), Core.engineIdentity({ ...health, translation_identity: { model: "other" } }));
  assert.equal(Core.engineIdentity(health), Core.engineIdentity({ ...health }));
});
