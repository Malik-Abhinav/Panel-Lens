import SwiftUI

@main
struct PanelLensApp: App {
    @StateObject private var appState = AppState()

    var body: some Scene {
        MenuBarExtra("PanelLens", systemImage: appState.status.systemImage) {
            MenuBarContent(appState: appState)
        }

        Settings {
            SettingsView()
        }
    }
}
