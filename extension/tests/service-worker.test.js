"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");
const { webcrypto } = require("node:crypto");
const { performance } = require("node:perf_hooks");

test("health rejects an incompatible engine protocol", async () => {
  let listener;
  const context = {
    AbortController, setTimeout, clearTimeout,
    chrome: {
      runtime: { onMessage: { addListener: callback => { listener = callback; } } },
      storage: { local: { get: async () => ({ panelLensToken: "token" }) } }
    },
    fetch: async () => new Response(JSON.stringify({ status: "ok", protocol_version: 2 }))
  };
  vm.runInNewContext(fs.readFileSync(require.resolve("../service-worker.js"), "utf8"), vm.createContext(context));
  const result = await new Promise(resolve => listener({ type: "PANELLENS_HEALTH" }, {}, resolve));
  assert.match(result.error.message, /incompatible/);
});

test("identical concurrent content coalesces work without conflating image IDs", async () => {
  let listener;
  let imageFetches = 0;
  let translations = 0;
  const context = {
    AbortController,
    Blob,
    Response,
    TextEncoder,
    btoa: (value) => Buffer.from(value, "binary").toString("base64"),
    chrome: {
      runtime: { onMessage: { addListener: (callback) => { listener = callback; } } },
      storage: { local: { get: async () => ({ panelLensToken: "test-token" }) } }
    },
    console: { debug() {} },
    crypto: webcrypto,
    performance,
    setTimeout,
    clearTimeout,
    fetch: async (url) => {
      if (url.startsWith("https://reader.test/")) {
        imageFetches += 1;
        return new Response(new Blob([new Uint8Array([1, 2, 3])], { type: "image/png" }));
      }
      translations += 1;
      await new Promise((resolve) => setTimeout(resolve, 5));
      return new Response(JSON.stringify({ status: "ok", regions: [] }), {
        status: 200, headers: { "Content-Type": "application/json" }
      });
    }
  };
  vm.runInNewContext(
    fs.readFileSync(require.resolve("../service-worker.js"), "utf8"),
    vm.createContext(context)
  );

  const send = (imageId) => new Promise((resolve) => listener({
    type: "PANELLENS_TRANSLATE",
    payload: {
      requestId: `request-${imageId}`, sessionId: "document-1", imageId,
      elementId: imageId, resourceVersion: 0,
      url: "https://reader.test/same.png", priority: "P1",
      pageOrigin: "https://reader.test", pageTitle: "Reader", context: []
    }
  }, {}, resolve));
  const [first, second] = await Promise.all([send("image-a"), send("image-b")]);

  assert.equal(imageFetches, 1);
  assert.equal(translations, 1);
  assert.equal(first.image_id, "image-a");
  assert.equal(second.image_id, "image-b");
  assert.equal(first.content_hash, second.content_hash);
  assert.equal(second.browser_metrics.duplicate_acquisition_avoided, true);
  assert.equal(
    Number(first.browser_metrics.duplicate_ocr_avoided) + Number(second.browser_metrics.duplicate_ocr_avoided),
    1
  );
  assert.equal(
    Number(first.browser_metrics.duplicate_translation_avoided) + Number(second.browser_metrics.duplicate_translation_avoided),
    1
  );
});

test("model selection stops all reader tabs before changing the provider", async () => {
  let listener;
  const events = [];
  const context = {
    AbortController, setTimeout, clearTimeout,
    chrome: {
      runtime: { onMessage: { addListener: callback => { listener = callback; } } },
      storage: { local: { get: async () => ({ panelLensToken: "token" }) } },
      tabs: {
        query: async () => [{ id: 1 }, { id: 2 }],
        sendMessage: async (id, message) => { events.push(`stop-${id}`); assert.equal(message.enabled, false); }
      }
    },
    fetch: async (_url, options) => {
      events.push("configure");
      assert.equal(JSON.parse(options.body).provider, "ollama");
      return new Response(JSON.stringify({ status: "ok" }));
    }
  };
  vm.runInNewContext(fs.readFileSync(require.resolve("../service-worker.js"), "utf8"), vm.createContext(context));
  const result = await new Promise(resolve => listener({ type: "PANELLENS_SET_MODEL", settings: { provider: "ollama", model: "my-local-model" } }, {}, resolve));
  assert.equal(result.status, "ok");
  assert.deepEqual(events, ["stop-1", "stop-2", "configure"]);
});
