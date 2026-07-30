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

PanelLens uses Ollama with Qwen2.5 7B by default:

```sh
ollama serve
ollama pull qwen2.5:7b
```

The sidecar calls Ollama at `http://127.0.0.1:11434`. Development overrides:

```sh
PANELLENS_OLLAMA_URL=http://127.0.0.1:11434
PANELLENS_OLLAMA_MODEL=qwen2.5:7b
PANELLENS_OLLAMA_KEEP_ALIVE=30m
PANELLENS_RESULT_CACHE_SIZE=8
PANELLENS_TRANSLATION_CACHE_SIZE=64
```

PanelLens warms Qwen in the background when the sidecar starts and asks Ollama
to keep it resident for 30 minutes. The sidecar also keeps the eight most recent
complete screenshot results in memory, so translating an unchanged capture
again avoids both OCR and model generation. Translation responses include
separate `ocr_processing_time_ms`, `translation_processing_time_ms`, and
`cache_hit` fields for profiling.

Translations are generated deterministically in one structured page-level
request. Every OCR region has an ordered ID and type, allowing the model to use
nearby dialogue and narration for continuity while returning exactly one result
per region. Cheap structural validation catches empty output, leftover Hangul,
implausible expansion, and duplicated translations. Only suspicious regions
receive a separate repair request. A second cache
normalizes harmless OCR spacing differences so repeated pages receive the same
English output.

## Bubble filtering

Before translation, OCR regions are classified as dialogue, narration, or
non-dialogue text. Clean light speech-bubble interiors are retained. Korean
prose over artwork is also retained when its length, grammar, punctuation,
typography size, and OCR confidence indicate narration. Short sound effects,
signs, and small webpage labels outside bubbles are skipped. The default score
threshold was calibrated against the annotated Korean evaluation images and can
be overridden during experiments:

```sh
PANELLENS_MINIMUM_BUBBLE_SCORE=0.55
PANELLENS_MINIMUM_NARRATION_SCORE=0.60
```

If Ollama is offline or the model is missing, the sidecar returns a structured
error for the macOS app to display.
