"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

test("Start requests only the current page and discovered image sites", async () => {
  const fields = new Map();
  function field(id) {
    if (!fields.has(id)) fields.set(id, {
      value: "", textContent: "", disabled: false, listeners: {},
      addEventListener(type, callback) { this.listeners[type] = callback; },
      replaceChildren() {}
    });
    return fields.get(id);
  }
  let requested;
  const page = { enabled: false, mode: "eco", glossary: [], candidates: 2, ready: 0, queued: 0, processing: 0, errors: 0 };
  const context = {
    document: { querySelector: field, activeElement: null, createElement: () => ({ value: "" }) },
    chrome: {
      runtime: {
        getManifest: () => ({ version: "0.3.1" }),
        sendMessage: async message => message.type === "PANELLENS_MODEL_SETTINGS"
          ? { settings: { provider: "ollama", model: "installed", ollama_models: ["installed"] } }
          : { status: "ok", runtime: { ready: true, message: "Model installed" } }
      },
      storage: { local: { get: async () => ({ panelLensToken: "key" }), set: async () => {} } },
      tabs: {
        query: async () => [{ id: 7, url: "https://reader.test/chapter" }],
        sendMessage: async (_id, message) => message.type === "PANELLENS_SET_ENABLED" ? { ...page, enabled: true } : page
      },
      scripting: { executeScript: async () => [{ result: ["https://reader.test/*", "https://cdn.test/*"] }] },
      permissions: { request: async value => { requested = value.origins; return true; } }
    },
    setInterval() {}, URL
  };
  vm.runInNewContext(fs.readFileSync(require.resolve("../popup.js"), "utf8"), vm.createContext(context));
  await new Promise(setImmediate);
  await field("#toggle").listeners.click();
  assert.deepEqual([...requested], ["https://reader.test/*", "https://cdn.test/*"]);
});
