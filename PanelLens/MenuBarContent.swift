import AppKit
import SwiftUI

struct MenuBarContent: View {
    @ObservedObject var appState: AppState
    @Environment(\.openSettings) private var openSettings
    @Environment(\.openWindow) private var openWindow

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

        if !appState.recognizedTexts.isEmpty {
            Divider()

            Text("Detected Korean Blocks")
                .font(.caption.weight(.semibold))

            ForEach(
                Array(appState.recognizedTexts.prefix(6).enumerated()),
                id: \.offset
            ) { _, text in
                Text(text)
                    .lineLimit(1)
            }
        }

        Divider()

        Button("Translate Visible Area") {
            Task {
                await appState.captureSelectedWindow()
            }
        }
        .keyboardShortcut("t", modifiers: [.command, .shift])
        .disabled(!appState.canCapture)

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
                ? "Hide Test Overlay"
                : "Show Test Overlay"
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
                ? "Test Python Sidecar"
                : "Start Python Sidecar"
        ) {
            appState.testSidecar()
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
