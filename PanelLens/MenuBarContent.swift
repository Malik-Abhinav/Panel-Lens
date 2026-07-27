import AppKit
import SwiftUI

struct MenuBarContent: View {
    @ObservedObject var appState: AppState
    @Environment(\.openSettings) private var openSettings

    var body: some View {
        Text(appState.selectedWindowDescription)
            .foregroundStyle(.secondary)

        Divider()

        Button("Translate Visible Area") {
            // Screen capture is the first implementation milestone.
        }
        .keyboardShortcut("t", modifiers: [.command, .shift])

        Button("Select Window…") {
            // The ScreenCaptureKit window picker will be added next.
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
