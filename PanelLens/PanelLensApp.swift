import AppKit
import SwiftUI

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationWillFinishLaunching(
        _ notification: Notification
    ) {
        guard let bundleIdentifier = Bundle.main.bundleIdentifier else {
            return
        }

        let currentProcessIdentifier = ProcessInfo.processInfo.processIdentifier
        for application in NSRunningApplication.runningApplications(
            withBundleIdentifier: bundleIdentifier
        ) where application.processIdentifier != currentProcessIdentifier {
            application.terminate()
        }
    }
}

@main
struct PanelLensApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self)
    private var appDelegate

    @StateObject private var appState = AppState()

    var body: some Scene {
        MenuBarExtra("PanelLens", systemImage: appState.status.systemImage) {
            MenuBarContent(appState: appState)
        }

        Window("Select Browser Window", id: "window-picker") {
            WindowPickerView(appState: appState)
        }
        .defaultSize(width: 620, height: 460)

        Window("PanelLens Setup", id: "setup") {
            SetupView(appState: appState)
        }
        .defaultSize(width: 560, height: 520)

        Settings {
            SettingsView(appState: appState)
        }
    }
}
