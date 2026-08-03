import AppKit
import SwiftUI

struct MenuBarContent: View {
    @ObservedObject var appState: AppState
    @Environment(\.openSettings) private var openSettings
    @Environment(\.openWindow) private var openWindow
    @AppStorage("showPerformanceDiagnostics")
    private var showPerformanceDiagnostics = false

    var body: some View {
        Text(appState.selectedWindowDescription)
            .foregroundStyle(.secondary)

        Text(appState.message)
            .font(.caption)
            .foregroundStyle(appState.status == .error ? .red : .secondary)

        Label(
            appState.sidecarMessage,
            systemImage: sidecarStatusImage
        )
        .font(.caption)
        .foregroundStyle(
            appState.sidecarState == .error ? .red : .secondary
        )

        if !appState.translations.isEmpty {
            Divider()

            Text("Translations")
                .font(.caption.weight(.semibold))

            ForEach(appState.translations.prefix(6)) { item in
                VStack(alignment: .leading, spacing: 2) {
                    Text(item.original)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)

                    Text(item.translation)
                        .lineLimit(2)
                }
            }
        }

        if showPerformanceDiagnostics {
            Divider()

            performanceSummary

            Divider()
        } else {
            Divider()
        }

        Button("Translate Visible Area") {
            Task {
                await appState.captureSelectedWindow()
            }
        }
        .keyboardShortcut("t", modifiers: [.command, .shift])
        .disabled(!appState.canCapture)

        Button(
            appState.isTranslationSessionActive
                ? "Pause Translation Session"
                : "Start Translation Session"
        ) {
            if appState.isTranslationSessionActive {
                appState.pauseTranslationSession()
            } else {
                appState.startTranslationSession()
            }
        }
        .disabled(
            appState.selectedWindowID == nil
                || (!appState.isTranslationSessionActive
                    && appState.status == .working)
        )

        if appState.isTranslationSessionActive {
            Label(
                "Context: \(appState.translationContextCount)/20 blocks",
                systemImage: "text.line.last.and.arrowtriangle.forward"
            )
            .font(.caption)
            .foregroundStyle(.secondary)

            Button("Clear Translation Context") {
                appState.clearTranslationContext()
            }
            .disabled(appState.translationContextCount == 0)
        }

        Button("Select Window…") {
            openWindow(id: "window-picker")
            Task {
                await appState.bringWindowPickerForward()
            }
        }

        Button(
            appState.hasReadingArea
                ? "Change Reading Area…"
                : "Select Reading Area…"
        ) {
            appState.selectReadingArea()
        }
        .disabled(appState.selectedWindowID == nil)

        if appState.hasReadingArea {
            Button("Use Full Browser Window") {
                appState.clearReadingArea()
            }
        }

        Button(
            appState.isOverlayVisible
                ? "Hide Translation Overlay"
                : "Show Translation Overlay"
        ) {
            if appState.isOverlayVisible {
                appState.hideOverlay()
            } else {
                appState.showTestOverlay()
            }
        }
        .disabled(appState.selectedWindowID == nil)

        Button(
            appState.sidecarState == .ready
                ? "Check Local Runtime"
                : "Retry Local Runtime"
        ) {
            appState.checkLocalRuntime()
        }

        if !appState.hasScreenCapturePermission {
            Button("Open Screen Recording Settings…") {
                appState.openScreenRecordingSettings()
            }
        }

        if appState.lastCaptureURL != nil {
            Button("Reveal Last Capture") {
                appState.revealLastCapture()
            }
        }

        Divider()

        Button("Settings…") {
            openSettings()
        }
        .keyboardShortcut(",")

        Button("Quit PanelLens") {
            NSApplication.shared.terminate(nil)
        }
        .keyboardShortcut("q")
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
                    "Ollama keeps the translation model resident in RAM — it is the main battery and memory cost."
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
