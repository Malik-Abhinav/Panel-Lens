import SwiftUI

struct SettingsView: View {
    @ObservedObject var appState: AppState
    @AppStorage("showPerformanceDiagnostics")
    private var showPerformanceDiagnostics = false

    var body: some View {
        Form {
            Section("Translation") {
                LabeledContent("Source language", value: "Korean")
                LabeledContent("Target language", value: "English")
                LabeledContent("Local model", value: "hy-mt2:7b")
            }

            Section("Diagnostics") {
                Toggle(
                    "Show performance diagnostics in the menu",
                    isOn: $showPerformanceDiagnostics
                )
                Text("Displays approximate CPU, memory, and battery usage for PanelLens, its OCR sidecar, and Ollama.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        }
        .onAppear {
            appState.setPerformanceMonitoringEnabled(
                showPerformanceDiagnostics
            )
        }
        .onChange(of: showPerformanceDiagnostics) { _, enabled in
            appState.setPerformanceMonitoringEnabled(enabled)
        }
        .padding(20)
        .frame(width: 440)
    }
}
