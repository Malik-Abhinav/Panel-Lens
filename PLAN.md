# PanelLens — Revised Build Plan

**A native macOS app that translates Korean manhwa and Chinese manhua directly inside a browser window using a local pipeline.**

Target machine: M4 MacBook Air with 16 GB unified memory.

## 1. Product Vision

A reader opens an untranslated Korean manhwa or Chinese manhua in a browser, selects that browser window, and presses a keyboard shortcut. PanelLens:

1. Captures the visible browser content.
2. Finds dialogue and narration text.
3. Recognizes Korean or Chinese text.
4. Reconstructs the reading order for the visible webtoon layout.
5. Translates the text into English with series terminology and recent dialogue as context.
6. Places clean English overlays over the original text.

After the manual workflow is reliable, continuous mode will detect when scrolling stops and translate newly visible content automatically. All screenshots, OCR results, translations, corrections, and glossaries remain on the Mac.

## 2. Scope

### Supported

- Korean manhwa
- Simplified Chinese manhua
- Traditional Chinese manhua
- Primarily vertical, browser-based webtoon layouts
- Dialogue bubbles and narration boxes
- Local OCR and translation
- Series-specific terminology and user corrections

### Explicitly out of scope

- Japanese manga
- Right-to-left manga reading order
- Japanese vertical-text OCR
- Full-page comic-book files or e-reader support in the first version
- Sound-effect replacement in the first version
- Image inpainting or removal of original lettering
- Mobile, Windows, and browser-extension versions
- Cloud OCR or cloud translation

### MVP language strategy

The reader selects the source language when creating or opening a series:

- Korean
- Simplified Chinese
- Traditional Chinese

Automatic language detection can be added later. Explicit selection avoids needing OCR output before PanelLens knows which OCR model to run.

## 3. MVP Definition

The first usable version does one thing:

> Select a browser window containing a Korean webtoon, press a shortcut, and see English translations correctly positioned over the visible dialogue and narration.

The MVP includes:

- One selected browser window
- Korean source language
- Manual translation shortcut
- Static screenshot capture
- OCR bounding boxes and recognized text
- Local English translation
- Click-through overlay
- Basic exact-match caching
- Clear error and processing states

The MVP does not include:

- Continuous scrolling
- Chinese OCR
- Automatic language detection
- Translation-memory similarity search
- Automatic glossary extraction
- Bubble editing
- Advanced visual styling
- Distribution to other users

## 4. Architecture

```text
Native macOS menu-bar app (Swift/SwiftUI)
        |
        | Select and capture one browser window
        v
ScreenCaptureKit
        |
        | Image + request metadata
        v
Long-running Python sidecar
        |
        +-- text detection
        +-- Korean or Chinese OCR
        +-- layout grouping and reading order
        +-- exact translation-memory lookup
        +-- local LLM translation
        +-- structured JSON response
        |
        v
Swift coordinate mapping
        |
        v
Transparent NSPanel overlay
```

### Responsibilities

**Swift app**

- Menu-bar UI and settings
- Permission onboarding
- Window selection and tracking
- Screen capture
- Sidecar lifecycle and IPC
- Coordinate conversion
- Overlay rendering and interaction
- User-visible errors

**Python sidecar**

- Image decoding and preprocessing
- OCR model lifecycle
- Text detection and recognition
- Region grouping and reading order
- Translation requests
- Translation memory and glossary storage
- Structured logs and timing information

## 5. Technology Choices

| Layer | Initial choice | Notes |
|---|---|---|
| macOS app | Swift + SwiftUI | Native menu-bar UI and settings |
| Capture | ScreenCaptureKit | Capture the selected window, not the entire display |
| Overlay | NSPanel hosting SwiftUI | Transparent, click-through, and aligned with the browser |
| Window geometry | ScreenCaptureKit metadata plus Accessibility APIs where needed | Account for browser movement, resizing, and coordinate differences |
| IPC | Newline-delimited JSON over stdin/stdout | Simple for the prototype; move image bytes out of JSON if profiling shows a bottleneck |
| Korean OCR | PaddleOCR Korean model | Validate accuracy on the actual target sites before committing |
| Chinese OCR | PaddleOCR Chinese models | Test simplified and traditional samples separately |
| OCR fallback | Apple Vision | Useful baseline and possible lightweight first pass |
| Image processing | OpenCV | Cropping, preprocessing, change detection, and experiments |
| Translation | Ollama with a locally installed multilingual model | Model must be benchmarked for quality, memory, and latency |
| Storage | SQLite | Series, glossary, translations, corrections, and cache metadata |
| Search | Exact lookup first; FTS later | Vector retrieval is optional and must earn its complexity |

Do not claim Neural Engine execution unless profiling confirms the chosen runtime actually uses it. Ollama commonly relies on Apple Silicon GPU acceleration through Metal.

## 6. Data Contract

Use a versioned, newline-delimited JSON protocol. Every request gets an ID so Swift can discard stale results after the reader scrolls or changes windows.

### Swift to Python

```json
{
  "protocol_version": 1,
  "request_id": "7D79A0A7-8FC4-4BC2-93AF-A4F532B4C209",
  "type": "translate_frame",
  "source_language": "ko",
  "target_language": "en",
  "series_id": "solo-leveling",
  "chapter": "180",
  "image": {
    "encoding": "jpeg_base64",
    "width": 1440,
    "height": 900,
    "scale_factor": 2.0,
    "data": "..."
  }
}
```

### Python to Swift

```json
{
  "protocol_version": 1,
  "request_id": "7D79A0A7-8FC4-4BC2-93AF-A4F532B4C209",
  "status": "ok",
  "image_size": [1440, 900],
  "regions": [
    {
      "id": "region-1",
      "bbox": [820, 130, 210, 96],
      "original": "일어났군.",
      "translation": "You're awake.",
      "source_language": "ko",
      "ocr_confidence": 0.94,
      "translation_confidence": 0.84,
      "region_type": "dialogue",
      "order": 0,
      "cached": false
    }
  ],
  "timings_ms": {
    "decode": 12,
    "ocr": 430,
    "translation": 1180,
    "total": 1702
  }
}
```

Bounding boxes use top-left image-pixel coordinates throughout the Python pipeline. Convert coordinates exactly once on the Swift side.

## 7. Build Strategy

Build a thin vertical slice first. The initial milestone is not merely successful capture, IPC, or OCR:

> One real Korean bubble is captured, recognized, translated, and rendered in the correct location.

Only add multiple regions, scrolling, memory, and polish after that path works.

## Phase 0 — Dataset and Technical Spikes

**Goal:** Test the risky assumptions before committing to an implementation.

- Collect 20–30 representative screenshots from content the developer has permission to use.
- Include:
  - Korean dialogue bubbles
  - Korean narration boxes
  - Text directly over artwork
  - Small and stylized fonts
  - Two speakers at similar vertical positions
  - Long vertical gaps between panels
  - Simplified and traditional Chinese samples for later phases
- Manually annotate several Korean pages with:
  - Text bounding boxes
  - Ground-truth transcription
  - Expected reading order
- Compare Apple Vision and PaddleOCR on the Korean samples.
- Test at least two local translation-model sizes on the M4/16 GB machine.
- Record cold-start time, warm latency, peak memory, and obvious accuracy failures.

**Done when:** The Korean OCR and translation choices are based on target screenshots rather than library reputation.

## Phase 1 — Native Capture and Overlay Shell

**Goal:** Select a browser, capture it, and keep a test overlay aligned.

- Create a macOS SwiftUI menu-bar application targeting a deliberate minimum macOS version.
- Add:
  - Translate Visible Area
  - Select Window
  - Settings
  - Quit
- Guide the user through Screen Recording permission.
- Request Accessibility access only for features that actually require it.
- Enumerate shareable windows and display browser title, application, and thumbnail.
- Capture a selected window with `SCScreenshotManager`.
- Create a transparent, borderless `NSPanel` that:
  - stays above the selected browser;
  - does not become the normal key window;
  - ignores mouse events in reading mode;
  - follows Spaces and full-screen behavior as supported.
- Draw several test rectangles using image-relative coordinates.
- Track window movement and resizing.
- Explicitly test:
  - Retina and non-Retina scaling;
  - browser movement between displays;
  - browser resizing;
  - browser toolbar and content offsets;
  - macOS bottom-left versus image top-left coordinate origins;
  - captured shadows or excluded window borders.

**Done when:** Test rectangles remain aligned with known locations in the captured image after movement and resizing.

## Phase 2 — Sidecar and One-Bubble Vertical Slice

**Goal:** Translate one manually specified crop end to end.

- Create a long-running Python sidecar.
- Reserve stdout exclusively for IPC and write diagnostics to stderr or a rotating log file.
- Launch and monitor the sidecar from Swift.
- Add `ping`, `health`, and `translate_frame` messages.
- Restart after a crash with a bounded retry policy.
- Send a screenshot and initially crop one known bubble manually.
- Run Korean OCR on the crop.
- Translate the recognized text through Ollama.
- Return one structured region.
- Render the result over the correct location.
- Ignore responses whose request ID is no longer current.

**Done when:** Pressing the shortcut translates one real Korean bubble and places the translation correctly.

## Phase 3 — Korean Text Detection and OCR

**Goal:** Return all useful Korean text regions from the visible frame.

- Establish Apple Vision and PaddleOCR baselines.
- Separate these concepts in code:
  - text detection;
  - text-line recognition;
  - dialogue/narration grouping;
  - optional sound-effect classification.
- Do not assume speech bubbles can be found reliably using simple contours.
- Use OCR text boxes as the primary first implementation.
- Experiment with preprocessing only where the dataset demonstrates a benefit:
  - contrast normalization;
  - upscaling small text;
  - thresholding;
  - padding crops;
  - rotation correction.
- Merge OCR lines that belong to the same dialogue or narration region using:
  - vertical and horizontal gaps;
  - overlap;
  - font scale;
  - surrounding bubble or box evidence.
- Exclude likely browser UI and obvious sound effects from translation where possible.
- Preserve both line-level and grouped bounding boxes for debugging.
- Store annotated debug images showing boxes, IDs, confidences, and order.

**Done when:** Most dialogue and narration on the Korean evaluation pages is returned as sensibly grouped text regions.

## Phase 4 — Webtoon Reading Order

**Goal:** Produce a stable order for vertical manhwa and manhua layouts.

Use a layout-aware heuristic:

1. Group nearby regions into vertical page sections or probable panels.
2. Order those sections from top to bottom.
3. Within a section, order primarily by vertical position.
4. Treat regions as occupying the same row only when their vertical ranges substantially overlap.
5. Within the same row, use left-to-right order as the default.
6. Preserve a deterministic fallback order and expose uncertain groupings in debug output.

Do not hardcode Japanese right-to-left behavior.

- Write unit tests for:
  - simple vertical dialogue;
  - two bubbles at the same height;
  - narration above a panel;
  - multiple lines merged into one bubble;
  - large vertical panel gaps;
  - overlapping region boxes.
- Measure pairwise ordering accuracy as well as perfect-page accuracy.

**Done when:** Ordered OCR output can be read naturally on the Korean evaluation set.

## Phase 5 — Contextual Translation

**Goal:** Produce natural English while preserving names and terminology.

- Start by translating all visible ordered regions in one structured request.
- Provide:
  - source and target language;
  - series synopsis if configured;
  - approved glossary;
  - recent translated dialogue;
  - numbered source regions.
- Require structured output containing the same region IDs.
- Validate:
  - every requested ID appears once;
  - no unknown IDs appear;
  - translation values are strings;
  - response length matches the request.
- Retry once after malformed output with a repair prompt.
- Apply a timeout and return a clear partial or failed result.
- Do not present model-generated confidence as a calibrated probability. Treat it as a UI hint until evaluated.
- Keep names and special terms deterministic by applying the approved glossary before or during translation.

**Done when:** A Korean page is translated coherently and repeated names remain consistent.

## Phase 6 — Overlay Rendering and Basic UX

**Goal:** Make translations readable without disrupting browsing.

- Scale image-pixel boxes to overlay-local coordinates.
- Handle aspect-fit or cropping explicitly; never assume equal X and Y scaling without verifying capture geometry.
- Build adaptive translation bubbles with:
  - text wrapping;
  - minimum and maximum font size;
  - configurable opacity;
  - padding based on box size;
  - neutral fallback background;
  - subtle states for low OCR confidence and errors.
- Initially cover the source text rather than attempting image inpainting.
- Add menu-bar states:
  - idle;
  - capturing;
  - recognizing;
  - translating;
  - complete;
  - error.
- Add a shortcut to hide/show overlays.
- Keep reading mode click-through.

**Done when:** A full visible Korean frame is translated and remains legible and aligned.

## Phase 7 — Cache, Series Context, and Corrections

**Goal:** Improve consistency and avoid repeating work.

### Core schema

```sql
CREATE TABLE series (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source_language TEXT NOT NULL,
    target_language TEXT NOT NULL DEFAULT 'en'
);

CREATE TABLE translations (
    id INTEGER PRIMARY KEY,
    series_id TEXT NOT NULL,
    source_text TEXT NOT NULL,
    normalized_source_text TEXT NOT NULL,
    translated_text TEXT NOT NULL,
    speaker TEXT,
    user_corrected INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(series_id) REFERENCES series(id)
);

CREATE TABLE glossary (
    id INTEGER PRIMARY KEY,
    series_id TEXT NOT NULL,
    source_term TEXT NOT NULL,
    translated_term TEXT NOT NULL,
    term_type TEXT,
    user_defined INTEGER NOT NULL DEFAULT 0,
    UNIQUE(series_id, source_term),
    FOREIGN KEY(series_id) REFERENCES series(id)
);
```

- Use exact normalized-text lookup first.
- Add a perceptual image-region cache for revisited content.
- Give user corrections and user-defined glossary entries highest priority.
- Add an edit mode that temporarily enables pointer interaction on the overlay.
- Save enough provenance to distinguish OCR text, model output, and user corrections.
- Add FTS-based similarity only after exact matching works.
- Add embeddings or vector retrieval only if evaluation shows it improves translation quality.

**Done when:** Revisiting a page is fast and a corrected name remains correct in later chapters.

## Phase 8 — Chinese Support

**Goal:** Add manhua without destabilizing the Korean pipeline.

- Add simplified and traditional Chinese series options.
- Benchmark the chosen OCR configurations on annotated Chinese samples.
- Add Chinese-specific normalization while preserving names and punctuation needed for translation.
- Test horizontal and occasional vertical Chinese text separately.
- Re-run detection, OCR, ordering, and translation metrics by language.
- Keep language-specific components behind a common OCR interface.

**Done when:** Korean, simplified Chinese, and traditional Chinese each pass their own evaluation set.

## Phase 9 — Continuous Scroll Mode

**Goal:** Translate after scrolling stops without wasting resources.

- Move from manual screenshots to a ScreenCaptureKit stream.
- Perform inexpensive change detection before invoking OCR.
- Compare downscaled grayscale frames or perceptual hashes.
- Ignore small changes such as cursors, video animations, and blinking UI where possible.
- Debounce until the page has been visually stable for a configurable interval.
- Cancel or supersede in-flight work when a newer frame arrives.
- Retain overlays for unchanged regions where matching is reliable.
- Remove stale overlays immediately when their source content moves.
- Measure idle CPU, active CPU/GPU, memory, and end-to-end latency on the target Mac.

Initial targets:

- Idle CPU below 2%
- Cached visible content below 500 ms
- Warm new Korean frame under 5 seconds
- No translation while actively scrolling
- No stale response rendered over a newer frame

These are targets, not claims, until measured.

**Done when:** Normal scrolling feels smooth and translations appear after the viewport settles.

## Phase 10 — Onboarding, Packaging, and Distribution

**Goal:** Make the app usable without a developer present.

- Build a first-launch checklist for:
  - Screen Recording permission;
  - Accessibility permission if required;
  - sidecar health;
  - Ollama availability;
  - selected model availability.
- Explain local-model disk and memory requirements before download.
- Decide between:
  - developer-focused distribution requiring Ollama;
  - bundling a controlled inference runtime and model-management experience.
- Package the Python runtime and required models or replace the sidecar with a distributable runtime.
- Ensure app signing, hardened runtime, sandbox decisions, and notarization match the chosen distribution method.
- Test installation on a clean macOS user account and another Mac.
- Provide logs and a diagnostics export with no screenshots included by default.

**Done when:** Another person can install, configure, and use PanelLens without direct help.

## 8. Evaluation

Maintain separate Korean and Chinese evaluation results.

### Text detection

- Precision: detected regions containing useful dialogue or narration
- Recall: annotated dialogue or narration successfully detected
- IoU or center-overlap accuracy for bounding boxes

### OCR

- Character Error Rate
- Exact-line accuracy
- Accuracy by text size and region type

### Reading order

- Pairwise ordering accuracy
- Percentage of pages with completely correct order

### Translation

- Human ratings for adequacy, fluency, and terminology consistency
- Name and glossary consistency
- Compare against a chosen baseline on the same source text

BLEU can be reported as a secondary metric, but it should not be the primary quality claim for dialogue because multiple English translations can be valid.

### Performance

- Cold start and warm start
- OCR latency
- Translation latency
- End-to-end latency
- Peak memory
- Idle and active CPU/GPU usage
- Cache hit rate during a real reading session

## 9. Suggested Timeline

| Stage | Focus | Estimate |
|---|---|---|
| 0 | Dataset and risky technical spikes | 3–5 days |
| 1 | Window capture, overlay, and coordinates | 1 week |
| 2 | One-bubble end-to-end vertical slice | 1 week |
| 3–4 | Korean detection, OCR, grouping, and order | 2–3 weeks |
| 5–6 | Contextual translation and full overlay UX | 1–2 weeks |
| 7 | Cache, series glossary, and corrections | 1–2 weeks |
| 8 | Chinese support | 1–2 weeks |
| 9 | Continuous scrolling and performance | 1–2 weeks |
| 10 | Packaging, onboarding, and distribution | 1–3 weeks |

A convincing Korean prototype is realistic before the complete product. A polished, distributable Korean-and-Chinese application should be scheduled based on evaluation results rather than committed to an eight-week deadline.

## 10. Milestones

### Milestone A — Technical proof

One Korean bubble translates and aligns correctly.

### Milestone B — Korean MVP

One visible Korean webtoon frame translates on demand with useful accuracy.

### Milestone C — Usable Korean reader

Context, caching, corrections, and continuous scrolling work together.

### Milestone D — Manhwa and manhua release

Korean plus simplified/traditional Chinese pass evaluation and the app installs cleanly.

## 11. Main Risks

| Risk | Mitigation |
|---|---|
| OCR misses stylized or artwork-overlay text | Annotated target dataset, preprocessing experiments, and OCR fallback |
| OCR lines are grouped into the wrong bubbles | Preserve line boxes, develop explicit grouping tests, and render debug overlays |
| Reading order fails in complex panels | Section grouping, overlap-aware ordering, measurable evaluation, and deterministic fallback |
| Translation changes names or terminology | Approved per-series glossary, batch context, and persistent corrections |
| Overlay drifts from captured content | Normalize coordinate systems, test display scaling, and treat geometry as an early milestone |
| Local model is too slow or memory-heavy | Benchmark multiple sizes on the actual 16 GB machine |
| Scrolling produces stale translations | Request IDs, cancellation, stability debounce, and immediate stale-overlay removal |
| Distribution becomes fragile | Separate prototype dependencies from the eventual packaged runtime decision |

## 12. Immediate Next Step

Before creating the full Xcode application:

1. Build the Korean evaluation folder.
2. Select five representative screenshots.
3. Benchmark Apple Vision and PaddleOCR against manually transcribed text.
4. Benchmark two local translation models on that OCR output.
5. Record results in a simple table.

Then implement Milestone A: one Korean bubble from capture to aligned overlay.

