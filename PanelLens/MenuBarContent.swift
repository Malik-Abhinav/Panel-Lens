import AppKit
import SwiftUI

struct MenuBarContent: View {
    @ObservedObject var appState: AppState
    @Environment(\.openSettings) private var openSettings
    @Environment(\.openWindow) private var openWindow
    @AppStorage("showPerformanceDiagnostics")
    private var showPerformanceDiagnostics = false
    @AppStorage("hasCompletedSetup") private var hasCompletedSetup = false

    var body: some View {
        Label("PanelLens Local Engine", systemImage: "desktopcomputer")
        Text(appState.browserBridgeMessage).font(.caption).foregroundStyle(.secondary)
        Text(appState.sidecarMessage).font(.caption).foregroundStyle(.secondary)
        Text("Read and translate using the PanelLens browser extension.").font(.caption)

        Button("Browser Setup & Models…") { openWindow(id: "setup") }
        Button("Copy Extension Connection Key") { appState.copyBrowserConnectionKey() }
            .disabled(!appState.browserBridgeReady)
        Button("Check Engine") { appState.checkLocalRuntime() }

        Divider()
        Menu("Screen Capture Fallback") {
            Text("Use for readers the extension cannot access.").font(.caption)
            Text(appState.selectedWindowDescription).font(.caption)
            Button("Select Window…") {
                openWindow(id: "window-picker")
                Task { await appState.bringWindowPickerForward() }
            }
            Button("Translate Visible Area") { Task { await appState.captureSelectedWindow() } }
                .disabled(!appState.canCapture)
            Button(appState.isTranslationSessionActive ? "Pause Translation Session" : "Start Translation Session") {
                if appState.isTranslationSessionActive { appState.pauseTranslationSession() }
                else { appState.startTranslationSession() }
            }.disabled(appState.selectedWindowID == nil)
            Button("Select Reading Area…") { appState.selectReadingArea() }
                .disabled(appState.selectedWindowID == nil)
            if appState.hasReadingArea {
                Button("Use Full Browser Window") { appState.clearReadingArea() }
            }
            Button("Clear Translation Context") { appState.clearTranslationContext() }
            Button(appState.isOverlayVisible ? "Hide Overlay" : "Show Overlay") {
                if appState.isOverlayVisible { appState.hideOverlay() }
                else { appState.showTestOverlay() }
            }.disabled(appState.selectedWindowID == nil)
            Button("Screen Recording Permission…") { appState.openScreenRecordingSettings() }
        }
        if showPerformanceDiagnostics { performanceSummary }
        Divider()
        Button("Settings…") { openSettings() }.keyboardShortcut(",")
        Button("Quit PanelLens Engine") { NSApplication.shared.terminate(nil) }.keyboardShortcut("q")
        .onAppear {
            if !hasCompletedSetup { openWindow(id: "setup") }
        }
    }

    private var performanceSummary: some View {
        guard let snapshot = appState.resourceSnapshot else {
            return AnyView(
                Text("Performance info unavailable yet…")
                    .font(.caption)
            )
        }

        let batteryText = snapshot.batteryPercent.map { "\($0)%" } ?? "—"
        let batteryIcon = snapshot.isCharging ? "bolt.fill" : "battery.75"
        let ramText = String(
            format: "%.1f / %.1f GB",
            snapshot.systemUsedGB,
            snapshot.systemTotalGB
        )
        let appText = String(
            format: "%.0f MB  •  ~%.0f%% CPU",
            snapshot.appMemoryMB,
            snapshot.appCPUPercent
        )
        let sidecarText = String(
            format: "%.0f MB  •  ~%.0f%% CPU",
            snapshot.sidecarMemoryMB,
            snapshot.sidecarCPUPercent
        )
        let ollamaText = String(
            format: "%.0f MB  •  ~%.0f%% CPU",
            snapshot.ollamaMemoryMB,
            snapshot.ollamaCPUPercent
        )

        return AnyView(
            VStack(alignment: .leading, spacing: 4) {
                Label(
                    "Performance & Battery",
                    systemImage: snapshot.memoryPressureHigh
                        ? "exclamationmark.triangle"
                        : "gauge"
                )
                .font(.caption.weight(.semibold))
                .foregroundStyle(
                    snapshot.memoryPressureHigh ? .orange : .primary
                )

                HStack {
                    Image(systemName: batteryIcon)
                    Text(
                        "Battery \(batteryText)"
                            + (snapshot.isCharging ? " · Charging" : "")
                    )
                }
                .font(.caption)

                Text(
                    "RAM \(ramText)"
                        + (snapshot.memoryPressureHigh ? " · High" : "")
                )
                .font(.caption)
                .foregroundStyle(
                    snapshot.memoryPressureHigh ? .orange : .secondary
                )

                Text("PanelLens: \(appText)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Text("Sidecar: \(sidecarText)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Text("Ollama (model): \(ollamaText)")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                Text(
                    "The selected local model stays loaded between requests. Quit PanelLens when you are finished reading."
                )
                .font(.caption2)
                .foregroundStyle(.tertiary)
            }
        )
    }

    private var sidecarStatusImage: String {
        switch appState.sidecarState {
        case .stopped:
            "stop.circle"
        case .starting:
            "hourglass.circle"
        case .ready:
            "checkmark.circle"
        case .error:
            "exclamationmark.triangle"
        }
    }
}
