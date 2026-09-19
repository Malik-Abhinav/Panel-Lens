# App and extension release

PanelLens packages an Apple Silicon Mac app, its local OCR runtime, and an unpacked Chrome/Edge extension. Users install or import their own translation model in Ollama. No translation model is included or downloaded by PanelLens.

## Build

Use `scripts/package_runtime.py` to assemble the pinned Python and OCR engine, then `sh scripts/build_distribution.sh` to create the app ZIP and SHA-256 file. The build is ad hoc signed unless a signing identity is explicitly provided. It does not require Apple Developer Program membership. The extension is inside `PanelLens.app/Contents/Resources/extension`.

## Release checks

Inspect the ZIP file list to confirm it contains the app and extension, with no translation weights, training material, research images, credentials, or logs. Verify the ZIP checksum. On a clean Apple Silicon Mac, test first launch and Apple's [Open Anyway](https://support.apple.com/en-gb/102445) flow, choose an installed Ollama model, connect the unpacked extension, and verify a real page receives an overlay. Test model switching and an app update. An unpacked extension needs manual Reload after updating the app.

Public download, first-open, and full browser overlay checks remain unverified. Do not describe an untested asset as a verified installer.
