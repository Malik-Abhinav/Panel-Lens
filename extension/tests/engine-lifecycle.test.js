"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const Core = require("../prefetch-core.js");

function reader(health) {
  let listener, poll;
  const messages = [];
  class Observer { observe() {} disconnect() {} }
  const context = {
    PanelLensPrefetchCore: Core, crypto: { randomUUID: () => "doc" },
    location: { origin: "https://reader.test", hostname: "reader.test", pathname: "/comic", search: "" },
    localStorage: { getItem: () => null },
    document: { images: [], documentElement: {}, addEventListener() {} },
    chrome: { runtime: { onMessage: { addListener: callback => { listener = callback; } }, sendMessage: async message => {
      messages.push(message.type);
      return message.type === "PANELLENS_HEALTH" ? health.current : { status: "ok" };
    } } },
    IntersectionObserver: Observer, ResizeObserver: Observer, MutationObserver: Observer,
    addEventListener() {}, removeEventListener() {}, scrollY: 0, performance: { now: () => 1 },
    setTimeout: () => 1, clearTimeout() {}, setInterval: callback => { poll = callback; return 1; }, clearInterval() {},
    console, URLSearchParams
  };
  vm.runInNewContext(fs.readFileSync(require.resolve("../content-script.js"), "utf8"), vm.createContext(context));
  return { send: message => new Promise(resolve => listener(message, {}, resolve)), poll: () => poll(), messages };
}

const healthy = () => ({ status: "ok", runtime: { ready: true, model: "b3" }, browser_bridge: { session: "one" } });

test("reading stops and clears its results after engine restart", async () => {
  const health = { current: healthy() };
  const page = reader(health);
  assert.equal((await page.send({ type: "PANELLENS_SET_ENABLED", enabled: true })).enabled, true);
  health.current = { ...healthy(), browser_bridge: { session: "two" } };
  await page.poll();
  const state = await page.send({ type: "PANELLENS_STATUS" });
  assert.equal(state.enabled, false);
  assert.equal(state.ready, 0);
  assert.match(state.pausedError, /engine or translation model changed/);
  assert.ok(page.messages.includes("PANELLENS_CLEAR"));
});

test("reading cannot start with a disconnected or unpaired engine", async () => {
  const page = reader({ current: { status: "error", error: { message: "Connect the Mac app" } } });
  const state = await page.send({ type: "PANELLENS_SET_ENABLED", enabled: true });
  assert.equal(state.enabled, false);
  assert.equal(state.pausedError, "Connect the Mac app");
});

test("cancel and retry keep earlier translations on a long page", async () => {
  let listener, pendingTranslation;
  const timers = [];
  let nextId = 0;
  class Element { contains() { return false; } }
  class Image extends Element {
    constructor(src, top) { super(); Object.assign(this, { src, currentSrc: src, top, naturalWidth: 650, naturalHeight: 800, complete: true, isConnected: true }); }
    getBoundingClientRect() { return { left: 0, top: this.top, width: 650, height: 800 }; }
    closest() { return null; }
    addEventListener() {}
  }
  class Observer { observe() {} disconnect() {} }
  const images = [new Image("https://reader.test/one.png", 0), new Image("https://reader.test/two.png", 900)];
  const context = {
    PanelLensPrefetchCore: Core, crypto: { randomUUID: () => `id-${++nextId}` },
    location: { origin: "https://reader.test", hostname: "reader.test", pathname: "/comic", search: "" },
    localStorage: { getItem: () => null },
    document: { images, title: "Reader", documentElement: {}, addEventListener() {} },
    Element, HTMLImageElement: Image,
    chrome: { runtime: { onMessage: { addListener: callback => { listener = callback; } }, sendMessage: async message => {
      if (message.type === "PANELLENS_HEALTH") return healthy();
      if (message.type === "PANELLENS_TRANSLATE") {
        if (message.payload.url.endsWith("one.png")) return { status: "ok", image_id: message.payload.imageId, regions: [{ bbox: [0, 0, 20, 20], translation: "Hello" }] };
        return new Promise(resolve => { pendingTranslation = resolve; });
      }
      return { status: "ok" };
    } } },
    IntersectionObserver: Observer, ResizeObserver: Observer, MutationObserver: Observer,
    addEventListener() {}, removeEventListener() {}, scrollX: 0, scrollY: 0, innerHeight: 800,
    performance: { now: () => 1000 },
    setTimeout: callback => { timers.push(callback); return timers.length; }, clearTimeout() {},
    setInterval: () => 1, clearInterval() {}, requestAnimationFrame() {},
    console, URLSearchParams
  };
  vm.runInNewContext(fs.readFileSync(require.resolve("../content-script.js"), "utf8"), vm.createContext(context));
  const send = message => new Promise(resolve => listener(message, {}, resolve));
  await send({ type: "PANELLENS_SET_ENABLED", enabled: true });
  while (timers.length) { timers.shift()(); await new Promise(setImmediate); }
  let state = await send({ type: "PANELLENS_STATUS" });
  assert.equal(state.ready, 1);
  assert.equal(state.processing, 1);
  state = await send({ type: "PANELLENS_CANCEL_ACTIVE" });
  assert.equal(state.ready, 1);
  assert.equal(state.processing, 0);
  assert.equal(state.errors, 1);
  pendingTranslation({ status: "error", image_id: "late", error: { message: "late failure" } });
  await new Promise(setImmediate);
  state = await send({ type: "PANELLENS_RETRY_ERRORS" });
  assert.equal(state.ready, 1);
  assert.equal(state.errors, 0);
  assert.equal(state.enabled, true);
});
