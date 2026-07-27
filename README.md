# PanelLens

PanelLens is a native macOS app for translating Korean manhwa and Chinese manhua
inside a selected browser window using a local OCR and translation pipeline.

The current focus is the Korean MVP:

> Select a browser window, press a shortcut, and display correctly positioned
> English translations over visible Korean dialogue and narration.

See [PLAN.md](PLAN.md) for the complete roadmap and
[evaluation/README.md](evaluation/README.md) for the first hands-on task.

## Current status

- Product scope and phased plan defined
- Evaluation dataset structure prepared
- Ollama and a project-specific Python 3.12 environment installed
- Native menu-bar application scaffolded in `PanelLens.xcodeproj`
- Sidecar JSON health-check protocol implemented and verified
- Full Xcode installation is still required to build and run the macOS app
