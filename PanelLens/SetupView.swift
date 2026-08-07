import SwiftUI

struct SetupView: View {
    @ObservedObject var appState: AppState
    @AppStorage("hasCompletedSetup") private var hasCompletedSetup = false
    @Environment(\.dismissWindow) private var dismissWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            VStack(alignment: .leading, spacing: 6) {
                Text("Set up PanelLens")
                    .font(.largeTitle.bold())
                Text("Everything runs locally. Complete these steps once, then select a browser and start translating.")
                    .foregroundStyle(.secondary)
            }

            setupRow(
                title: "Screen Recording",
                detail: appState.hasScreenCapturePermission
                    ? "Permission granted"
                    : "Required to read the selected browser window",
                ready: appState.hasScreenCapturePermission
            ) {
                Button("Open Privacy Settings") {
                    appState.openScreenRecordingSettings()
                }
            }

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
                title: "Hy-MT2 7B",
                detail: modelDetail,
                ready: appState.sidecarState == .ready
            ) {
                if appState.runtimeCode == "model_missing" {
                    Button("Install Model") {
                        appState.installTranslationModel()
                    }
                    .disabled(appState.isInstallingModel)
                } else {
                    Button("Check Again") {
                        appState.checkLocalRuntime()
                    }
                }
            }

            if !appState.modelInstallationMessage.isEmpty {
                Text(appState.modelInstallationMessage)
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }

            Spacer()

            HStack {
                Text("You can reopen this assistant from the PanelLens menu.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                Button("Finish Setup") {
                    hasCompletedSetup = true
                    dismissWindow(id: "setup")
                }
                .buttonStyle(.borderedProminent)
                .disabled(!appState.isSetupReady)
            }
        }
        .padding(28)
        .frame(minWidth: 520, minHeight: 480)
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
        if appState.isInstallingModel {
            return "Downloading and installing…"
        }
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
