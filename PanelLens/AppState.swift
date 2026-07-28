import AppKit
import Combine
import CoreGraphics
import ImageIO
import ScreenCaptureKit
import UniformTypeIdentifiers

@MainActor
final class AppState: ObservableObject {
    enum Status: String {
        case idle = "Idle"
        case working = "Working"
        case error = "Error"

        var systemImage: String {
            switch self {
            case .idle:
                "doc.viewfinder"
            case .working:
                "ellipsis.circle"
            case .error:
                "exclamationmark.circle"
            }
        }
    }

    @Published var status: Status = .idle
    @Published var selectedWindowDescription = "No window selected"
    @Published private(set) var availableWindows: [WindowOption] = []
    @Published private(set) var selectedWindowID: CGWindowID?
    @Published private(set) var isLoadingWindows = false
    @Published private(set) var message = "Select a browser window to begin."
    @Published private(set) var lastCaptureURL: URL?
    @Published private(set) var hasScreenCapturePermission =
        CGPreflightScreenCaptureAccess()

    private var selectedWindow: SCWindow?

    var canCapture: Bool {
        selectedWindow != nil && status != .working
    }

    func refreshWindows() async {
        guard !isLoadingWindows else { return }

        hasScreenCapturePermission = CGPreflightScreenCaptureAccess()
        if !hasScreenCapturePermission {
            let requestAccepted = CGRequestScreenCaptureAccess()
            hasScreenCapturePermission =
                requestAccepted || CGPreflightScreenCaptureAccess()
        }

        guard hasScreenCapturePermission else {
            status = .error
            message = "macOS still reports Screen Recording as off for PanelLens. Enable it, then completely stop and rerun the app."
            return
        }

        isLoadingWindows = true
        message = "Loading available windows…"

        defer {
            isLoadingWindows = false
        }

        do {
            let content = try await SCShareableContent.excludingDesktopWindows(
                true,
                onScreenWindowsOnly: true
            )

            let ownBundleIdentifier = Bundle.main.bundleIdentifier
            availableWindows = content.windows
                .filter { window in
                    guard
                        window.frame.width >= 200,
                        window.frame.height >= 150,
                        let application = window.owningApplication,
                        application.bundleIdentifier != ownBundleIdentifier
                    else {
                        return false
                    }

                    return !(window.title ?? "").trimmingCharacters(
                        in: .whitespacesAndNewlines
                    ).isEmpty
                }
                .map(WindowOption.init)
                .sorted { left, right in
                    if left.isBrowser != right.isBrowser {
                        return left.isBrowser
                    }
                    if left.applicationName != right.applicationName {
                        return left.applicationName.localizedCaseInsensitiveCompare(
                            right.applicationName
                        ) == .orderedAscending
                    }
                    return left.title.localizedCaseInsensitiveCompare(
                        right.title
                    ) == .orderedAscending
                }

            if availableWindows.isEmpty {
                message = "No usable windows found. Open a browser window and refresh."
            } else {
                message = "\(availableWindows.count) windows available."
            }
            status = .idle
        } catch {
            present(error: error, action: "Loading windows")
        }
    }

    func select(_ option: WindowOption) {
        selectedWindow = option.window
        selectedWindowID = option.id
        selectedWindowDescription = "\(option.applicationName) — \(option.title)"
        message = "Ready to capture \(option.applicationName)."
        status = .idle
    }

    func bringWindowPickerForward() async {
        message = "Opening window picker…"

        // A menu-bar-only app has no Dock presence to activate automatically.
        // Give SwiftUI's Window scene a moment to create its NSWindow, then
        // explicitly bring that window to the front.
        for delay in [50, 150, 300, 600] {
            try? await Task.sleep(for: .milliseconds(delay))

            if let pickerWindow = NSApplication.shared.windows.first(where: {
                $0.identifier?.rawValue == "window-picker"
                    || $0.title == "Select Browser Window"
            }) {
                NSApplication.shared.activate(ignoringOtherApps: true)
                pickerWindow.makeKeyAndOrderFront(nil)
                message = "Choose the browser window you want to capture."
                return
            }
        }

        status = .error
        message = "The window picker could not be opened. Try again."
    }

    func captureSelectedWindow() async {
        guard let selectedWindow else {
            status = .error
            message = "Select a window before capturing."
            return
        }

        status = .working
        message = "Capturing \(selectedWindowDescription)…"

        do {
            let filter = SCContentFilter(
                desktopIndependentWindow: selectedWindow
            )
            let configuration = SCStreamConfiguration()
            let scale = max(1, CGFloat(filter.pointPixelScale))
            configuration.width = max(
                1,
                Int((selectedWindow.frame.width * scale).rounded())
            )
            configuration.height = max(
                1,
                Int((selectedWindow.frame.height * scale).rounded())
            )
            configuration.showsCursor = false
            configuration.captureResolution = .best

            let image = try await SCScreenshotManager.captureImage(
                contentFilter: filter,
                configuration: configuration
            )
            let captureURL = try saveDebugCapture(image)

            lastCaptureURL = captureURL
            status = .idle
            message = "Captured \(image.width)×\(image.height) px."
        } catch {
            present(error: error, action: "Capturing the selected window")
        }
    }

    func revealLastCapture() {
        guard let lastCaptureURL else { return }
        NSWorkspace.shared.activateFileViewerSelecting([lastCaptureURL])
    }

    func openScreenRecordingSettings() {
        guard
            let settingsURL = URL(
                string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
            )
        else {
            return
        }

        NSWorkspace.shared.open(settingsURL)
    }

    private func saveDebugCapture(_ image: CGImage) throws -> URL {
        let fileManager = FileManager.default
        let applicationSupport = try fileManager.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        )
        let capturesDirectory = applicationSupport
            .appendingPathComponent("PanelLens", isDirectory: true)
            .appendingPathComponent("Captures", isDirectory: true)
        try fileManager.createDirectory(
            at: capturesDirectory,
            withIntermediateDirectories: true
        )

        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyyMMdd-HHmmss"
        let fileURL = capturesDirectory.appendingPathComponent(
            "capture-\(formatter.string(from: Date())).png"
        )

        guard
            let destination = CGImageDestinationCreateWithURL(
                fileURL as CFURL,
                UTType.png.identifier as CFString,
                1,
                nil
            )
        else {
            throw CaptureError.couldNotCreateImageDestination
        }

        CGImageDestinationAddImage(destination, image, nil)
        guard CGImageDestinationFinalize(destination) else {
            throw CaptureError.couldNotWriteImage
        }

        return fileURL
    }

    private func present(error: Error, action: String) {
        status = .error
        message = "\(action) failed: \(error.localizedDescription)"
    }
}

struct WindowOption: Identifiable {
    let window: SCWindow
    let id: CGWindowID
    let title: String
    let applicationName: String
    let bundleIdentifier: String
    let frame: CGRect
    let isBrowser: Bool

    init(window: SCWindow) {
        let application = window.owningApplication
        let bundleIdentifier = application?.bundleIdentifier ?? ""

        self.window = window
        id = window.windowID
        title = window.title ?? "Untitled Window"
        applicationName = application?.applicationName ?? "Unknown Application"
        self.bundleIdentifier = bundleIdentifier
        frame = window.frame
        isBrowser = BrowserIdentifiers.all.contains(bundleIdentifier)
    }
}

private enum BrowserIdentifiers {
    static let all: Set<String> = [
        "com.apple.Safari",
        "com.brave.Browser",
        "com.google.Chrome",
        "com.google.Chrome.canary",
        "com.microsoft.edgemac",
        "com.operasoftware.Opera",
        "com.vivaldi.Vivaldi",
        "company.thebrowser.Browser",
        "org.mozilla.firefox",
    ]
}

private enum CaptureError: LocalizedError {
    case couldNotCreateImageDestination
    case couldNotWriteImage

    var errorDescription: String? {
        switch self {
        case .couldNotCreateImageDestination:
            "PanelLens could not create the PNG destination."
        case .couldNotWriteImage:
            "PanelLens could not finish writing the PNG."
        }
    }
}
