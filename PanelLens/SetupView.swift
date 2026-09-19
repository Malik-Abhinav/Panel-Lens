import SwiftUI

struct SetupView: View {
    @ObservedObject var appState: AppState
    @AppStorage("hasCompletedSetup") private var hasCompletedSetup = false
    @Environment(\.dismissWindow) private var dismissWindow
    @State private var updateMessage = ""
    @State private var availableReleaseURL: URL?

    var body: some View {
        ScrollView {
        VStack(alignment: .leading, spacing: 20) {
            VStack(alignment: .leading, spacing: 6) {
                Text("Connect your browser")
                    .font(.largeTitle.bold())
                Text("Read with the browser extension. PanelLens keeps the translation engine running locally on your Mac.")
                    .foregroundStyle(.secondary)
            }

            setupRow(title: "1. Start the local engine", detail: appState.browserBridgeMessage, ready: appState.browserBridgeReady) {
                Button("Check Engine") { appState.checkLocalRuntime() }
            }
            VStack(alignment: .leading, spacing: 8) {
                Text("2. Install the browser extension").font(.headline)
                Text("Chrome and Edge are supported. Store publication is pending; this build includes extension files for Load unpacked in your browser's Extensions page.")
                    .font(.callout).foregroundStyle(.secondary)
                Button("Show Extension Files") { appState.revealBrowserExtension() }
                Text("3. Connect once, then start reading").font(.headline)
                Text("Copy your connection key, paste it into the extension, and click Connect. On a comic page, click Start on this page.")
                    .font(.callout).foregroundStyle(.secondary)
                Button("Copy Connection Key") { appState.copyBrowserConnectionKey() }
                    .disabled(!appState.browserBridgeReady)
                Text(appState.browserSetupMessage).font(.callout).foregroundStyle(.secondary)
            }

            Text(appState.sidecarMessage).font(.callout).foregroundStyle(.secondary)
            if appState.sidecarState == .starting { ProgressView() }
            Button("Retry Runtime Installation") { appState.repairTranslationRuntime() }
                .disabled(appState.sidecarState == .starting)

            Picker("Translation provider", selection: $appState.translationProvider) {
                Text("Ollama").tag("ollama")
            }
            Text("Install or import a model in Ollama, then choose it here. PanelLens does not supply a translation model.")
                .font(.callout).foregroundStyle(.secondary)
            if appState.translationProvider == "ollama" {
                TextField("Installed Ollama model name", text: $appState.selectedTranslationModel)
                if !appState.availableTranslationModels.isEmpty {
                    Picker("Installed models", selection: $appState.selectedTranslationModel) {
                        ForEach(appState.availableTranslationModels, id: \.self) { Text($0).tag($0) }
                    }
                }
            }
            Button("Use Selected Model") { appState.applyTranslationSettings() }
                .disabled(appState.selectedTranslationModel.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            Text(appState.translationSettingsMessage).font(.callout).foregroundStyle(.secondary)

            VStack(alignment: .leading, spacing: 6) {
                Text("Updates").font(.headline)
                Text("App and bundled engine: 0.3.0 · Extension: 0.3.0")
                    .font(.callout).foregroundStyle(.secondary)
                Button("Check GitHub Releases") { Task { await checkForUpdates() } }
                if !updateMessage.isEmpty {
                    Text(updateMessage).font(.callout).foregroundStyle(.secondary)
                }
                if let availableReleaseURL {
                    Button("Open Release to Install Update") { NSWorkspace.shared.open(availableReleaseURL) }
                }
                Text("After installing a new app, reload its BrowserExtension in Chrome or Edge. Your Ollama models stay in Ollama.")
                    .font(.caption).foregroundStyle(.secondary)
            }

            if appState.translationProvider == "ollama" {
                setupRow(
                    title: "Ollama",
                    detail: ollamaDetail,
                    ready: appState.isOllamaInstalled
                ) {
                    if appState.isOllamaInstalled {
                        Button("Open Ollama") { appState.openOllama() }
                    } else {
                        Button("Download Ollama") {
                            appState.openOllamaDownload()
                        }
                    }
                }

                setupRow(
                    title: appState.selectedTranslationModel,
                    detail: modelDetail,
                    ready: appState.sidecarState == .ready
                ) {
                    Button("Check Again") {
                        appState.checkLocalRuntime()
                    }
                }

            }

            DisclosureGroup("Optional: screen capture fallback") {
                Text("For pages the extension cannot read, use Screen Capture Fallback in the menu. Only that path requires Screen Recording permission.")
                    .font(.callout)
                Button("Screen Recording Settings") { appState.openScreenRecordingSettings() }
            }
            Spacer()

            HStack {
                Text("You can reopen this assistant from the PanelLens menu.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                Button("Done") {
                    hasCompletedSetup = true
                    dismissWindow(id: "setup")
                }
                .buttonStyle(.borderedProminent)
                .disabled(!appState.isSetupReady)
            }
        }
        }
        .task { appState.readTranslationSettings() }
        .task {
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(2))
                if !Task.isCancelled { appState.checkLocalRuntime() }
            }
        }
        .padding(28)
        .frame(minWidth: 520, minHeight: 480)
    }

    private func checkForUpdates() async {
        updateMessage = "Checking GitHub…"
        availableReleaseURL = nil
        do {
            let endpoint = URL(string: "https://api.github.com/repos/Malik-Abhinav/Panel-Lens/releases?per_page=30")!
            var request = URLRequest(url: endpoint)
            request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse else { throw URLError(.badServerResponse) }
            if http.statusCode == 404 {
                updateMessage = "No public PanelLens release is available yet."
                return
            }
            guard http.statusCode == 200,
                  let releases = try JSONSerialization.jsonObject(with: data) as? [[String: Any]] else {
                throw URLError(.badServerResponse)
            }
            guard let release = releases.first(where: { ($0["tag_name"] as? String)?.hasPrefix("app-v") == true && ($0["draft"] as? Bool) == false && ($0["prerelease"] as? Bool) == false }),
                  let tag = release["tag_name"] as? String,
                  let address = release["html_url"] as? String,
                  let url = URL(string: address), url.host == "github.com" else {
                updateMessage = "No public PanelLens app release is available yet."
                return
            }
            let installed = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0.3.0"
            if tag == "app-v\(installed)" {
                updateMessage = "App \(installed) is the latest published version."
            } else {
                availableReleaseURL = url
                updateMessage = "Published release: \(tag). Review its app and extension versions before installing."
            }
        } catch {
            updateMessage = "Could not check releases: \(error.localizedDescription). Retry when online."
        }
    }

    private var ollamaDetail: String {
        if !appState.ollamaLaunchMessage.isEmpty {
            return appState.ollamaLaunchMessage
        }
        if !appState.isOllamaInstalled {
            return "Not installed"
        }
        return appState.runtimeCode == "ollama_offline"
            ? "Installed but not running"
            : "Installed"
    }

    private var modelDetail: String {
        return appState.sidecarMessage
    }

    @ViewBuilder
    private func setupRow<Actions: View>(
        title: String,
        detail: String,
        ready: Bool,
        @ViewBuilder actions: () -> Actions
    ) -> some View {
        HStack(alignment: .center, spacing: 14) {
            Image(systemName: ready ? "checkmark.circle.fill" : "circle")
                .font(.title2)
                .foregroundStyle(ready ? .green : .secondary)
                .frame(width: 28)
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.headline)
                Text(detail)
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            actions()
        }
        .padding(14)
        .background(.quaternary.opacity(0.45), in: RoundedRectangle(cornerRadius: 12))
    }
}
