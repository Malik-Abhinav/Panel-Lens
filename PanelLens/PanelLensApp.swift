import SwiftUI

@main
struct PanelLensApp: App {
    @StateObject private var appState = AppState()

    var body: some Scene {
        MenuBarExtra("PanelLens", systemImage: appState.status.systemImage) {
            MenuBarContent(appState: appState)
        }

        Window("Select Browser Window", id: "window-picker") {
            WindowPickerView(appState: appState)
        }
        .defaultSize(width: 620, height: 460)

        Settings {
            SettingsView()
        }
    }
}
