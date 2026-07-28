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
}
