# PanelLens browser-native reader

This Manifest V3 extension is the preferred acquisition path for compatible
image-based readers. It discovers original `<img>` resources, predicts the next
images from geometry and scroll motion, submits bounded P0-P3 work to the local
pipeline, and renders completed results only when the exact image record enters
the viewport. The native ScreenCaptureKit path remains the fallback.

## Connect through the Mac app

Open PanelLens and choose **Browser Setup & Models**. The app installs its
bundled runtime and automatically starts the authenticated loopback service;
do not launch a second Python server. Use **Show Extension Files**, then load
that stable BrowserExtension folder from Chrome/Edge's Extensions page (Developer
mode → Load unpacked). This is the temporary installation path until extension
store publication; no store URL exists yet.

Use **Copy Connection Key** in the app, paste it into the popup, and click
**Connect**. The key persists across restarts and is stored in extension-local
storage. On a comic page, click **Start on this page** and grant reader access
when the browser asks. Keep the Mac app running; no Screen Recording permission
is needed for browser reading. After updating PanelLens, use Show Extension Files
and Reload on the browser's Extensions page to update a locally loaded extension.

The service binds only to `127.0.0.1:8765`, validates extension origins and the
connection key, limits request bodies to 24 MiB, and never returns wildcard CORS.
The Mac app stores its connection key with user-only file permissions. Its health
response exposes bridge readiness/session and model identity, never the key.
The HTTP service shares the native process's exact translation/runtime modules;
there is no duplicate worker or settings process.

Model changes pause active reader tabs. While reading, a five-second health check
also catches native-side model changes, app restarts, and disconnects, clears
stale overlays, and asks the reader to start again. Missing/rejected keys and
unavailable models are shown separately from page-reading errors.

For source-development only, `sidecar/http_server.py` still provides a standalone
server. Do not run it alongside the Mac app on the same port; the app reports a
port conflict instead of silently connecting to the wrong process.

## Discovery and scheduling

- One initial `document.images` pass seeds records.
- `MutationObserver` handles inserted/removed images and `src`, `srcset`, and
  `sizes` changes. Image load events handle lazy resources; intersection and
  resize observers handle viewport entry and geometry changes.
- Scheduling is throttled to at most once per 100 ms during scroll. Overlay
  positioning uses `requestAnimationFrame`; OCR, acquisition, and planning do
  not run inside the frame callback.
- Geometry, not URL or array index, defines deterministic reading order. Each
  record binds document UUID, stable element ID, resource revision, dimensions,
  position, source hint, and eventually SHA-256 content identity.
- P0 is visible, P1 is next in the motion direction, P2 has sufficient predicted
  lead time, and P3 is speculative/obsolete. The browser is capped at four
  total queued/active jobs, two speculative jobs, 16 MiB per image, and 32 MiB
  queued bytes. Only three acquisitions may be in flight; Phase 2 still permits
  one OCR and one Ollama worker. Concurrent identical content is coalesced after
  hashing without conflating its separate DOM records.
- Completed browser results are capped at twelve records. Stale generations,
  removed elements, cancelled requests, and mismatched response identities are
  never rendered.

Telemetry is opt-in with `localStorage.panellensPerformance = "1"`. Events omit
URLs, image contents, OCR text, and translations; numeric byte counts are safe
metadata and remain available.

## Deterministic replay

Serve the repository and open `extension/fixtures/reader.html`:

```sh
python3 -m http.server 8080 --bind 127.0.0.1
```

The fixture contains 20 lazy image panels, dynamic insertion, `srcset` mutation,
and normal, half-viewport, rapid A/B/E/K/Q, reverse, and slow controls. Run core
tests and the 30-trial deterministic comparison with:

```sh
node --test extension/tests/prefetch-core.test.js
node experiments/browser_prefetch_benchmark.js \
  --output /tmp/panellens-browser-prefetch-benchmark.json
```

## Unsupported acquisition

Canvas, WebGL, CSS backgrounds, video, inaccessible shadow roots, unreliable
blob URLs, DRM resources, unsupported image formats, and virtualized content
that never enters the DOM are not browser-native candidates. Authentication,
CORS, expired URL, redirect, or fetch failures emit `fallback-required`; the
unchanged native app remains available for ScreenCaptureKit acquisition.
Automatic handoff into the native app is not yet wired. The extension does not
weaken page security to force acquisition.
