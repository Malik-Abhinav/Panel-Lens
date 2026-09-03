# PanelLens browser-prefetch feasibility spike

This unpacked Chromium Manifest V3 extension tests ordinary `<img>` readers.
It deliberately has broad image host access and an unauthenticated loopback API;
those are prototype-only choices, not the planned production security design.

## Start the local engine

From the repository root, with Ollama and `hy-mt2:7b` ready:

```sh
sidecar/.venv/bin/python sidecar/http_server.py
```

The server always binds to `127.0.0.1` and defaults to port `8765`.

## Load and exercise the extension

1. Open `chrome://extensions`, enable Developer mode, and choose **Load unpacked**.
2. Select this `extension` directory.
3. Serve the synthetic fixture from the repository root:

   ```sh
   python3 -m http.server 8080 --bind 127.0.0.1
   ```

4. Open `http://127.0.0.1:8080/extension/fixtures/reader.html`.
5. Open the PanelLens toolbar popup and choose **Start on this page**.
6. Inspect the page console for `[PanelLens spike]` metrics. `ready` events with
   `ahead: true` prove preprocessing; `cache-visible` reports how long a result
   was ready before the panel entered the viewport. Each `ready` event also
   reports OCR/translation timing, detected and filtered text counts, and the
   returned region types so missed narration can be diagnosed.
7. Repeat on an ordinary image-based reader. Do not add copyrighted images or
   captures to this repository.

The fixture checks discovery, the bounded queue, local transport, and overlay
geometry. A real Korean reader is still required for the feasibility decision.
Test slow/fast scrolling, window resizing, and browser zoom at 80%, 100%, and
125%. The decision gate requires at least two upcoming images to emit `ready`
with `ahead: true` before either is reached, with aligned overlays and no
perceptible scroll stutter.

## Processing modes

- **Eco** keeps two images ahead, translates one image per request, and caps
  unusually wide OCR inputs at 1400 pixels.
- **Balanced** keeps four images ahead, combines up to three ordered images in
  one Hy-MT2 request, and caps unusually wide inputs at 1800 pixels.
- **Full** uses the same three-image batching but continues through every
  detected image. Images inserted later by an infinite-scrolling reader are
  also discovered and processed.

Visible images remain highest priority in every mode. Downscaled OCR geometry
is converted back to natural-image coordinates before overlays render. Full
mode can keep CPU/GPU activity and power use high for a long time.

### Compare Eco and Balanced

Use the same reader and restart the Python server between runs so caches do not
skew the comparison. Record at least ten processed images in each mode. Compare
the console `processingMs`, `ocrMs`, and `translationMs` values plus the resource
CSV. Balanced is only a win if it lowers total wall time or energy without
increasing missed text or translation errors; batching is not assumed to be
faster for dialogue-heavy images.

For accuracy, verify that dark text in uniform colored boxes is retained and
that omitted Korean subjects are not assigned a gender without supporting
context. The extension sends at most twelve recent unique Korean/English pairs
as rolling context to limit repeated prompt work.

Use **Name and gender glossary** for canonical series information that cannot
be inferred reliably from Hangul. Enter one mapping per line, for example:

```text
베리엘=Belial (male)
엘리스=Ellis
```

At most eight glossary entries are kept as reference-only context. The
remaining context budget is filled with recent unique translations.

## Record CPU and memory use

From the repository root, record five minutes at two-second intervals with:

```sh
zsh experiments/record_prefetch_resources.sh 300 2
```

The ignored CSV under `experiments/results/prefetch/` records CPU percentage
and resident memory for the Python server, both the Ollama server and model
runner (`llama-server`), and the native PanelLens app. Ollama's Metal allocation
uses unified GPU memory that process RSS does not fully represent; use
`ollama ps` to record the loaded model size and CPU/GPU split as well.
For Apple Silicon GPU power/utilization, run this separately in another
terminal because macOS requires administrator permission:

```sh
sudo powermetrics --samplers gpu_power -i 2000 \
  -o /tmp/panellens-gpu-powermetrics.txt
```

Stop `powermetrics` with Control-C after the translation run. Neither report
contains source images or translation text.
