# PanelLens

PanelLens puts English translations over Korean text in comic images. Read with the Chrome or Edge extension; the Mac companion runs OCR and sends recognized text to a translation model in your own Ollama installation. The extension and companion communicate over an authenticated `127.0.0.1` connection.

**Release status:** The [0.3.1 test prerelease](https://github.com/Malik-Abhinav/Panel-Lens/releases/tag/v0.3.1-rc.1) provides an Apple Silicon app and unpacked browser extension. A second macOS account exposed problems in 0.3.0; the 0.3.1 fixes still need a clean-account reading test before a stable release. The ZIP contains no translation model or model download. You provide and choose an Ollama model on your Mac.

## Set up

1. Install [Ollama](https://ollama.com/download) and install or import a model you have the right to use. PanelLens accepts any model that appears in Ollama's installed model list. Translation quality varies by model.
2. Download the [0.3.1 Mac app ZIP](https://github.com/Malik-Abhinav/Panel-Lens/releases/download/v0.3.1-rc.1/PanelLens-0.3.1-macos-arm64.zip) and [checksum file](https://github.com/Malik-Abhinav/Panel-Lens/releases/download/v0.3.1-rc.1/SHA256SUMS). Check the ZIP checksum, unzip it, and open PanelLens. This build uses an ad hoc signature, so macOS may require **System Settings → Privacy & Security → Open Anyway** after the first launch. Follow [Apple's instructions](https://support.apple.com/en-gb/102445).
3. Start Ollama. In the PanelLens setup window, select an installed model or type its Ollama name, then click **Use Selected Model**. PanelLens never downloads a model automatically.
4. Click **Show Extension Files**. In Chrome or Edge, open the Extensions page, enable Developer mode, click **Load unpacked**, and choose the shown folder.
5. Click **Copy Connection Key** in the Mac app. Paste it into the extension popup and click **Connect**. Open a comic page and click **Start on this page**. Chrome asks for access to that page and the image sites currently visible to the extension.

You can change models in the Mac app or extension popup. Reading pauses during a change, and you can start the page again afterward. The extension reads ordinary image resources; for canvas, DRM, or inaccessible images, use **Screen Capture Fallback** in the Mac app. Only that fallback needs macOS Screen Recording permission.

The reader starts in Eco mode. The popup shows queued work, the image currently processing, and elapsed time. If a model stalls, click **Cancel processing**, then **Retry failed images** after the model responds or you have changed models. Completed translations on that page stay visible during cancel and retry. Stopping the reader or switching models clears that page's overlays; start it again afterward. A model appearing in Ollama only confirms installation, not that it can translate successfully.

Ollama can import a compatible local model file. See [Ollama's model import guide](https://docs.ollama.com/import) for the formats and steps it currently supports. Once the imported model appears in Ollama, select it in PanelLens. Directly selecting an arbitrary weights file in PanelLens is not supported.

## Privacy and limits

OCR runs in the Mac app. The extension sends readable image bytes to the authenticated local companion. The companion sends recognized text to the configured Ollama endpoint, which defaults to your Mac. If you change Ollama to a remote endpoint, that endpoint receives the text. PanelLens has no account or hosted translation service. Review translations against the original for important details.

The current app target is Apple Silicon macOS 14 or later. Its bundled OCR engine needs no user-installed Python or pip. Ollama and your selected model are separate installations. The unpacked extension must be reloaded after an app update and does not update automatically.

The app source is covered by [LICENSE](LICENSE). See [release preparation](distribution/README.md) and [extension details](extension/README.md).
