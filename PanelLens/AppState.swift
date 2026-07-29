import AppKit
import Combine
import CoreGraphics
import ImageIO
import ScreenCaptureKit
import UniformTypeIdentifiers

struct DisplayTranslation: Identifiable {
    let id = UUID()
    let original: String
    let translation: String
}

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
    @Published private(set) var isOverlayVisible = false
    @Published private(set) var sidecarState: SidecarState = .stopped
    @Published private(set) var sidecarMessage = "Python sidecar has not started."
    @Published private(set) var recognizedTexts: [String] = []
    @Published private(set) var translations: [DisplayTranslation] = []
    @Published private(set) var hasReadingArea = false
    @Published private(set) var hasScreenCapturePermission =
        CGPreflightScreenCaptureAccess()

    private var selectedWindow: SCWindow?
    private let overlayController = OverlayController()
    private let sidecarClient = SidecarClient()
    private var windowTrackingTask: Task<Void, Never>?
    private var normalizedReadingArea: CGRect?

    init() {
        sidecarClient.onStateChange = { [weak self] state, message in
            self?.sidecarState = state
            self?.sidecarMessage = message
        }
        sidecarClient.onResponse = { [weak self] response in
            guard let self else { return }

            if let regions = response.regions {
                if response.requestID?.hasPrefix("test-") == true {
                    let resultMessage =
                        "Swift received \(regions.count) fake translation region(s) from Python."
                    message = resultMessage
                    sidecarMessage = "IPC test passed."
                    presentSidecarTestAlert(
                        title: "Python Sidecar Test Passed",
                        message: resultMessage
                    )
                } else {
                    recognizedTexts = regions.map(\.original)
                    translations = regions.map {
                        DisplayTranslation(
                            original: $0.original,
                            translation: $0.translation
                        )
                    }
                    let processingDescription = response.processingTimeMS.map {
                        " in \(String(format: "%.1f", Double($0) / 1_000))s"
                    } ?? ""
                    message =
                        "Translated \(regions.count) Korean text block(s)\(processingDescription)."
                }
            } else if let error = response.error {
                sidecarMessage = "IPC test failed."
                if response.requestID?.hasPrefix("test-") == true {
                    presentSidecarTestAlert(
                        title: "Python Sidecar Test Failed",
                        message: error.message
                    )
                } else {
                    status = .error
                    message = "Python processing failed: \(error.message)"
                }
            }
        }
        sidecarClient.start()
    }

    var canCapture: Bool {
        selectedWindow != nil && status != .working
    }

    func refreshWindows() async {
        guard !isLoadingWindows else { return }

        hasScreenCapturePermission = CGPreflightScreenCaptureAccess()
        if !hasScreenCapturePermission {
            _ = CGRequestScreenCaptureAccess()
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

            // The CoreGraphics preflight value can remain stale after the user
            // enables permission. A successful ScreenCaptureKit request is the
            // authoritative signal that capture access is available.
            hasScreenCapturePermission = true

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
            hasScreenCapturePermission = CGPreflightScreenCaptureAccess()
            status = .error
            message = hasScreenCapturePermission
                ? "Loading windows failed: \(error.localizedDescription)"
                : "Screen Recording access is unavailable. Enable PanelLens in Privacy & Security, quit PanelLens completely, then run it again."
        }
    }

    func select(_ option: WindowOption) {
        selectedWindow = option.window
        selectedWindowID = option.id
        selectedWindowDescription = "\(option.applicationName) — \(option.title)"
        message = "Ready to capture \(option.applicationName)."
        normalizedReadingArea = nil
        hasReadingArea = false
        recognizedTexts = []
        translations = []
        status = .idle

        if isOverlayVisible {
            overlayController.show(over: option.frame)
            startTrackingWindow()
        }
    }

    func showTestOverlay() {
        guard let selectedWindow else {
            status = .error
            message = "Select a browser window before showing the overlay."
            return
        }

        overlayController.show(over: selectedWindow.frame)
        isOverlayVisible = true
        startTrackingWindow()
        status = .idle
        message = "Test overlay is aligned with \(selectedWindowDescription)."
    }

    func hideOverlay() {
        windowTrackingTask?.cancel()
        windowTrackingTask = nil
        overlayController.hide()
        isOverlayVisible = false
        status = .idle
        message = selectedWindow == nil
            ? "Select a browser window to begin."
            : "Overlay hidden."
    }

    func selectReadingArea() {
        guard let selectedWindow else {
            status = .error
            message = "Select a browser window before choosing a reading area."
            return
        }

        isOverlayVisible = false
        recognizedTexts = []
        translations = []
        startTrackingWindow()
        overlayController.selectReadingArea(
            over: selectedWindow.frame,
            onComplete: { [weak self] selection, canvasSize in
                guard let self else { return }

                normalizedReadingArea = CGRect(
                    x: selection.minX / canvasSize.width,
                    y: selection.minY / canvasSize.height,
                    width: selection.width / canvasSize.width,
                    height: selection.height / canvasSize.height
                )
                hasReadingArea = true
                finishReadingAreaSelection(
                    message: "Reading area saved. Only that part of the webpage will be OCRed."
                )
            },
            onCancel: { [weak self] in
                self?.finishReadingAreaSelection(
                    message: "Reading area selection cancelled."
                )
            }
        )
        message = "Drag around only the visible manhwa reader."
    }

    func clearReadingArea() {
        normalizedReadingArea = nil
        hasReadingArea = false
        recognizedTexts = []
        translations = []
        message = "Reading area cleared. Captures will use the full window."
    }

    func testSidecar() {
        if sidecarState == .error || sidecarState == .stopped {
            sidecarClient.start()
        } else {
            sidecarMessage = "Waiting for Python test response…"
            sidecarClient.sendTestTranslation()
        }
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
            let imageForOCR = try cropToReadingArea(image)
            let captureURL = try saveDebugCapture(imageForOCR)
            let captureData = try Data(contentsOf: captureURL)

            lastCaptureURL = captureURL
            sidecarClient.translate(imageData: captureData)
            status = .idle
            message =
                "Captured \(imageForOCR.width)×\(imageForOCR.height) px reading area and sent it to Python."
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

    private func presentSidecarTestAlert(title: String, message: String) {
        NSApplication.shared.activate(ignoringOtherApps: true)

        let alert = NSAlert()
        alert.alertStyle = title.contains("Passed") ? .informational : .warning
        alert.messageText = title
        alert.informativeText = message
        alert.addButton(withTitle: "OK")
        alert.runModal()
    }

    private func finishReadingAreaSelection(message: String) {
        windowTrackingTask?.cancel()
        windowTrackingTask = nil
        overlayController.hide()
        status = .idle
        self.message = message
    }

    private func cropToReadingArea(_ image: CGImage) throws -> CGImage {
        guard let normalizedReadingArea else {
            return image
        }

        let imageBounds = CGRect(
            x: 0,
            y: 0,
            width: image.width,
            height: image.height
        )
        let pixelRect = CGRect(
            x: normalizedReadingArea.minX * CGFloat(image.width),
            y: normalizedReadingArea.minY * CGFloat(image.height),
            width: normalizedReadingArea.width * CGFloat(image.width),
            height: normalizedReadingArea.height * CGFloat(image.height)
        )
        .integral
        .intersection(imageBounds)

        guard
            pixelRect.width > 0,
            pixelRect.height > 0,
            let croppedImage = image.cropping(to: pixelRect)
        else {
            throw CaptureError.couldNotCropReadingArea
        }

        return croppedImage
    }

    private func startTrackingWindow() {
        windowTrackingTask?.cancel()

        guard let selectedWindowID else {
            return
        }

        windowTrackingTask = Task { [weak self] in
            while !Task.isCancelled {
                if let frame = Self.windowFrame(for: selectedWindowID) {
                    self?.overlayController.updateFrame(frame)
                }

                try? await Task.sleep(for: .milliseconds(100))
            }
        }
    }

    nonisolated private static func windowFrame(
        for windowID: CGWindowID
    ) -> CGRect? {
        guard
            let windowInfo = CGWindowListCopyWindowInfo(
                [.optionIncludingWindow, .excludeDesktopElements],
                windowID
            ) as? [[String: Any]],
            let entry = windowInfo.first,
            let boundsDictionary = entry[
                kCGWindowBounds as String
            ] as? NSDictionary
        else {
            return nil
        }

        return CGRect(
            dictionaryRepresentation: boundsDictionary as CFDictionary
        )
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
    case couldNotCropReadingArea
    case couldNotWriteImage

    var errorDescription: String? {
        switch self {
        case .couldNotCreateImageDestination:
            "PanelLens could not create the PNG destination."
        case .couldNotCropReadingArea:
            "PanelLens could not crop the selected reading area."
        case .couldNotWriteImage:
            "PanelLens could not finish writing the PNG."
        }
    }
}
