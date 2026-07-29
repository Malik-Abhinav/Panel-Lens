# PanelLens Sidecar

This directory contains the long-running local OCR and translation process.
PanelLens launches `main.py` automatically and communicates with it through
newline-delimited JSON over stdin/stdout.

The current IPC protocol supports:

- `ping` → `pong` health checks
- `translate` → a fake Korean translation region for end-to-end testing

Diagnostics are written to `~/Library/Logs/PanelLens/sidecar.log`; stdout is
reserved exclusively for IPC.

The project-specific Python 3.12 environment lives in `.venv`. OCR dependencies
are listed separately in `requirements-ocr.txt`.
