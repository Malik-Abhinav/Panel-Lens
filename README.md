# PanelLens

PanelLens is a native macOS app for translating Korean manhwa and Chinese manhua
inside a selected browser window using a local OCR and translation pipeline.

The current focus is the Korean MVP:

> Select a browser window, press a shortcut, and display correctly positioned
> English translations over visible Korean dialogue and narration.

The private roadmap is kept outside Git. See
[evaluation/README.md](evaluation/README.md) for the OCR evaluation workflow.

## Current status

- Native menu-bar app builds and runs on macOS 14+
- Browser window selection and ScreenCaptureKit screenshots work
- Click-through overlay follows the selected browser as it moves and resizes
- Korean and Chinese OCR evaluation dataset structure is prepared
- Project-specific Python 3.12 environment is installed
- Swift launches and monitors the Python sidecar using newline-delimited JSON
- Korean OCR runs locally on the selected reader area
- Contextual Korean-to-English translation uses local Ollama/Qwen2.5 7B
