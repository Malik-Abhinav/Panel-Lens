# PanelLens Sidecar

This directory contains the long-running local OCR and translation process.
PanelLens launches `main.py` automatically and communicates with it through
newline-delimited JSON over stdin/stdout.

The current IPC protocol supports:

- `ping` → `pong` health checks
- `translate` → Korean OCR followed by contextual English translation

Diagnostics are written to `~/Library/Logs/PanelLens/sidecar.log`; stdout is
reserved exclusively for IPC.

The project-specific Python 3.12 environment lives in `.venv`. OCR dependencies
are listed separately in `requirements-ocr.txt`.

## Local translation model

PanelLens uses Tencent Hy-MT2 7B through Ollama by default. The official Q4
GGUF is imported locally as `hy-mt2:7b`; model files belong in the ignored
top-level `models/` directory and are never committed.

```sh
ollama serve
ollama list
# Confirm that hy-mt2:7b is installed.
```

The sidecar calls Ollama at `http://127.0.0.1:11434`. Development overrides:

```sh
PANELLENS_OLLAMA_URL=http://127.0.0.1:11434
PANELLENS_OLLAMA_MODEL=hy-mt2:7b
PANELLENS_TRANSLATION_ADAPTER=auto
PANELLENS_OLLAMA_KEEP_ALIVE=30m
PANELLENS_RESULT_CACHE_SIZE=8
PANELLENS_TRANSLATION_CACHE_SIZE=64
```

`auto` selects Hy-MT2's numbered direct-translation adapter for model names
starting with `hy-mt2`, and the structured JSON adapter for other model names.
An explicit `hy-mt2` or `panelens-json` override makes model experiments
possible without changing OCR, IPC, caching, or validation code. For example,
the previous model can still be tested with:

```sh
PANELLENS_OLLAMA_MODEL=qwen2.5:7b
PANELLENS_TRANSLATION_ADAPTER=panelens-json
```

PanelLens warms the selected model in the background when the sidecar starts
and asks Ollama to keep it resident for 30 minutes. The sidecar also keeps the
eight most recent complete screenshot results in memory, so translating an
unchanged capture again avoids both OCR and model generation. Translation
responses include separate `ocr_processing_time_ms`,
`translation_processing_time_ms`, and `cache_hit` fields for profiling.

Translations are generated in one aligned page-level request. Every OCR region
has an ordered ID and type, allowing the model to use nearby dialogue and
narration for continuity while returning exactly one result per region. Cheap
structural validation catches empty output, leftover Hangul, implausible
expansion, and duplicated translations. Only suspicious regions receive a
separate adapter-aware repair request. A second cache normalizes harmless OCR
spacing differences so repeated pages receive the same English output.

The native app also sends up to 20 recent Korean/English blocks as rolling
reference context. Previous blocks are separated from the current viewport and
are never included in the requested output. The sidecar validates this history
and caps it at 6,000 characters so context helps with names, omitted subjects,
pronouns, terminology, and cross-viewport continuity without growing latency
indefinitely.

## Bubble filtering

Before translation, OCR regions are classified as dialogue, narration, or
non-dialogue text. Clean light speech-bubble interiors are retained. Korean
prose over artwork is also retained when its length, grammar, punctuation,
typography size, and OCR confidence indicate narration. Short sound effects,
signs, and small webpage labels outside bubbles are skipped. The default score
threshold was calibrated against the annotated Korean evaluation images and can
be overridden during experiments:

Wrapped OCR lines are grouped using both geometry and the connected light
background around them. This keeps multiline dialogue together inside one
balloon while preventing nearby, separately bounded balloons from being
translated as one sentence. Non-bubble narration falls back to geometric
grouping.

```sh
PANELLENS_MINIMUM_BUBBLE_SCORE=0.55
PANELLENS_MINIMUM_TRANSLUCENT_BUBBLE_SCORE=0.68
PANELLENS_MINIMUM_NARRATION_SCORE=0.60
```

Translucent balloons use a separate conservative path based on local
brightness, color neutrality, smoothness, OCR confidence, dialogue structure,
and neutral-colored lettering. Short fragments require stronger visual
confidence and punctuation. Small colored decorative sound effects remain
filtered even when they appear over a white page.

If Ollama is offline or the model is missing, the sidecar returns a structured
error for the macOS app to display.
