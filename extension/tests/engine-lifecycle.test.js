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
