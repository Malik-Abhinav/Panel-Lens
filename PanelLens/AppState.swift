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
    @Published private(set) var isTranslationSessionActive = false
    @Published private(set) var hasScreenCapturePermission =
        CGPreflightScreenCaptureAccess()

    private var selectedWindow: SCWindow?
    private let overlayController = OverlayController()
    private let sidecarClient = SidecarClient()
    private var windowTrackingTask: Task<Void, Never>?
    private var normalizedReadingArea: CGRect?
    private var pendingTranslationImageSize: CGSize?
    private var pendingTranslationReadingArea: CGRect?
    private var automaticCaptureTask: Task<Void, Never>?
    private var lastScrollActivityAt: Date?
    private var translationInvalidatedByScroll = false
    private var pendingTranslationFingerprint: [UInt8]?
    private var lastPresentedFingerprint: [UInt8]?
    private var lastPresentedRegions: [SidecarRegion] = []
    private var translationContext: [[String: String]] = []
    private var translationSessionID = UUID()
    private var activeTranslationRequestID: String?
    private var shouldRecoverAfterSidecarRestart = false
    private var overlayHiddenForAppSwitch = false

    private let automaticTranslationDelay = Duration.milliseconds(500)
    private let similarCaptureDifference = 3.0
    private let translationContextLimit = 20

    init() {
        overlayController.onDismissForScroll = { [weak self] in
            guard let self else { return }
            isOverlayVisible = false
            message = "Translation overlay cleared after the page scrolled."
        }
        overlayController.onScrollActivity = { [weak self] in
            self?.handleScrollActivity()
        }
        overlayController.shouldHandleScroll = { [weak self] in
            self?.shouldHandleSelectedBrowserScroll() ?? false
        }
        sidecarClient.onStateChange = { [weak self] state, message in
            self?.handleSidecarStateChange(state, message: message)
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
                    guard response.requestID == activeTranslationRequestID else {
                        return
                    }
                    activeTranslationRequestID = nil
                    guard isSelectedWindowAvailable() else {
                        stopTranslationSession(
                            message: "The selected browser window is no longer available. Translation paused."
                        )
                        return
                    }
                    if translationInvalidatedByScroll {
                        status = .idle
                        message =
                            "Page changed while translating. Waiting for the latest viewport…"
                        scheduleAutomaticCapture()
                        return
                    }
                    recognizedTexts = regions.map(\.original)
                    translations = regions.map {
                        DisplayTranslation(
                            original: $0.original,
                            translation: $0.translation
                        )
                    }
                    lastPresentedFingerprint = pendingTranslationFingerprint
                    lastPresentedRegions = regions
                    rememberTranslationContext(from: regions)
                    showTranslationOverlay(for: regions)
                    status = .idle
                    if response.cacheHit == true {
                        message =
                            "Reused \(regions.count) cached translation(s) instantly."
                    } else {
                        let ocrSeconds = Double(
                            response.ocrProcessingTimeMS ?? 0
                        ) / 1_000
                        let translationSeconds = Double(
                            response.translationProcessingTimeMS ?? 0
                        ) / 1_000
                        let filteredDescription = (
                            response.filteredTextCount ?? 0
                        ) > 0
                            ? " Skipped \(response.filteredTextCount ?? 0) non-dialogue text region(s)."
                            : ""
                        message =
                            "Translated \(regions.count) block(s). OCR: "
                            + "\(String(format: "%.1f", ocrSeconds))s, translation: "
                            + "\(String(format: "%.1f", translationSeconds))s."
                            + filteredDescription
                    }
                }
            } else if let error = response.error {
                if response.requestID?.hasPrefix("test-") == true {
                    sidecarMessage = "IPC test failed."
                    presentSidecarTestAlert(
                        title: "Python Sidecar Test Failed",
                        message: error.message
                    )
                } else {
                    guard response.requestID == activeTranslationRequestID else {
                        return
                    }
                    activeTranslationRequestID = nil
                    if translationInvalidatedByScroll {
                        status = .idle
                        message =
                            "Page changed while translating. Waiting for the latest viewport…"
                        scheduleAutomaticCapture()
                        return
                    }
                    sidecarMessage = "Translation failed."
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

    var translationContextCount: Int {
        translationContext.count
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
        stopTranslationSession(message: nil)
        selectedWindow = option.window
        selectedWindowID = option.id
        selectedWindowDescription = "\(option.applicationName) — \(option.title)"
        message = "Ready to capture \(option.applicationName)."
        isOverlayVisible = false
        normalizedReadingArea = nil
        hasReadingArea = false
        recognizedTexts = []
        translations = []
        pendingTranslationImageSize = nil
        pendingTranslationReadingArea = nil
        cancelAutomaticCapture()
        clearTranslationHistory()
        overlayController.clearTranslations()
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
        message = translations.isEmpty
            ? "No translations are available yet."
            : "Translation overlay is aligned with \(selectedWindowDescription)."
    }

    func hideOverlay() {
        let pausedSession = isTranslationSessionActive
        if pausedSession {
            stopTranslationSession(message: nil)
        }
        windowTrackingTask?.cancel()
        windowTrackingTask = nil
        cancelAutomaticCapture()
        overlayController.hide()
        isOverlayVisible = false
        status = .idle
        message = selectedWindow == nil
            ? "Select a browser window to begin."
            : pausedSession
                ? "Overlay hidden. Automatic translation paused."
                : "Overlay hidden."
    }

    func startTranslationSession() {
        guard selectedWindow != nil else {
            status = .error
            message = "Select a browser window before starting translation."
            return
        }
        guard isSelectedWindowAvailable() else {
            status = .error
            message = "The selected browser window is hidden or unavailable."
            return
        }

        translationSessionID = UUID()
        activeTranslationRequestID = nil
        isTranslationSessionActive = true
        status = .idle
        message = "Starting translation session…"

        if sidecarState == .ready {
            Task { await captureSelectedWindow(automatically: true) }
        } else {
            shouldRecoverAfterSidecarRestart = true
            sidecarClient.start()
            message = "Waiting for the local translation sidecar…"
        }
    }

    func pauseTranslationSession() {
        stopTranslationSession(message: "Automatic translation paused.")
    }

    func clearTranslationContext() {
        translationContext = []
        message = "Translation context cleared for this reading session."
    }

    private func stopTranslationSession(message: String?) {
        isTranslationSessionActive = false
        translationSessionID = UUID()
        activeTranslationRequestID = nil
        shouldRecoverAfterSidecarRestart = false
        cancelAutomaticCapture()
        overlayController.pauseScrollMonitoring()
        if status == .working {
            status = .idle
        }
        if let message {
            self.message = message
        }
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
        clearTranslationHistory()
        overlayController.clearTranslations()
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
                captureFirstSessionViewportIfNeeded()
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
        pendingTranslationImageSize = nil
        pendingTranslationReadingArea = nil
        cancelAutomaticCapture()
        clearTranslationHistory()
        overlayController.clearTranslations()
        message = "Reading area cleared. Captures will use the full window."
        captureFirstSessionViewportIfNeeded()
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
        await captureSelectedWindow(automatically: false)
    }

    private func captureSelectedWindow(automatically: Bool) async {
        guard let selectedWindow else {
            status = .error
            message = "Select a window before capturing."
            return
        }
        if automatically, !isTranslationSessionActive {
            return
        }
        guard isSelectedWindowAvailable() else {
            if automatically {
                stopTranslationSession(
                    message: "The selected browser window is hidden or unavailable. Translation paused."
                )
            } else {
                status = .error
                message = "The selected browser window is hidden or unavailable."
            }
            return
        }
        guard sidecarState == .ready else {
            status = .idle
            if automatically {
                shouldRecoverAfterSidecarRestart = true
                sidecarClient.start()
                message = "Waiting for the local translation sidecar…"
            } else {
                status = .error
                message = "The local translation sidecar is not ready."
            }
            return
        }
        let captureSessionID = translationSessionID

        status = .working
        translationInvalidatedByScroll = false
        if automatically {
            overlayController.hideTranslationsWhileMonitoringScroll()
        } else {
            cancelAutomaticCapture()
            overlayController.hide()
        }
        isOverlayVisible = false
        message = automatically
            ? "Page settled. Capturing the new viewport…"
            : "Capturing \(selectedWindowDescription)…"

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
            guard captureSessionID == translationSessionID else { return }
            if automatically, !isTranslationSessionActive { return }

            let imageForOCR = try cropToReadingArea(image)
            let fingerprint = try captureFingerprint(imageForOCR)
            pendingTranslationImageSize = CGSize(
                width: imageForOCR.width,
                height: imageForOCR.height
            )
            pendingTranslationReadingArea = normalizedReadingArea
            pendingTranslationFingerprint = fingerprint
            if automatically,
               let lastPresentedFingerprint,
               !lastPresentedRegions.isEmpty,
               fingerprintDifference(
                   fingerprint,
                   lastPresentedFingerprint
               ) <= similarCaptureDifference
            {
                recognizedTexts = lastPresentedRegions.map(\.original)
                translations = lastPresentedRegions.map {
                    DisplayTranslation(
                        original: $0.original,
                        translation: $0.translation
                    )
                }
                showTranslationOverlay(for: lastPresentedRegions)
                status = .idle
                message =
                    "Viewport barely changed. Reused the current translations."
                return
            }
            let captureURL = try saveDebugCapture(imageForOCR)
            let captureData = try Data(contentsOf: captureURL)
            guard captureSessionID == translationSessionID else { return }

            guard sidecarState == .ready else {
                status = .idle
                if automatically {
                    shouldRecoverAfterSidecarRestart = true
                    sidecarClient.start()
                    message = "The sidecar restarted. Waiting to recapture…"
                } else {
                    status = .error
                    message = "The local translation sidecar stopped before translation began."
                }
                return
            }

            lastCaptureURL = captureURL
            let requestID = "session-\(translationSessionID.uuidString)-\(UUID().uuidString)"
            activeTranslationRequestID = requestID
            let sent = sidecarClient.translate(
                imageData: captureData,
                requestID: requestID,
                context: translationContext
            )
            guard sent else {
                activeTranslationRequestID = nil
                status = .error
                message = "Could not send the viewport to the local sidecar."
                return
            }
            message =
                "Reading Korean and translating locally…"
        } catch {
            guard captureSessionID == translationSessionID else { return }
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

    private func handleScrollActivity() {
        guard isTranslationSessionActive, selectedWindow != nil else {
            return
        }

        lastScrollActivityAt = Date()
        if status == .working {
            translationInvalidatedByScroll = true
        }
        scheduleAutomaticCapture()
    }

    private func scheduleAutomaticCapture() {
        automaticCaptureTask?.cancel()
        automaticCaptureTask = Task { [weak self] in
            guard let self else { return }
            try? await Task.sleep(for: automaticTranslationDelay)
            guard !Task.isCancelled else { return }
            guard isTranslationSessionActive else { return }

            if status == .working {
                return
            }

            if let lastScrollActivityAt {
                let elapsed = Date().timeIntervalSince(lastScrollActivityAt)
                if elapsed < 0.5 {
                    scheduleAutomaticCapture()
                    return
                }
            }

            await captureSelectedWindow(automatically: true)
        }
    }

    private func cancelAutomaticCapture() {
        automaticCaptureTask?.cancel()
        automaticCaptureTask = nil
        lastScrollActivityAt = nil
        translationInvalidatedByScroll = false
    }

    private func clearTranslationHistory() {
        pendingTranslationFingerprint = nil
        lastPresentedFingerprint = nil
        lastPresentedRegions = []
        translationContext = []
    }

    private func captureFirstSessionViewportIfNeeded() {
        guard isTranslationSessionActive else { return }
        translationSessionID = UUID()
        activeTranslationRequestID = nil
        status = .idle
        Task { await captureSelectedWindow(automatically: true) }
    }

    private func handleSidecarStateChange(
        _ state: SidecarState,
        message: String
    ) {
        sidecarState = state
        sidecarMessage = message

        if state == .starting,
           isTranslationSessionActive,
           (activeTranslationRequestID != nil || status == .working)
        {
            shouldRecoverAfterSidecarRestart = true
            activeTranslationRequestID = nil
            status = .idle
        }

        if state == .ready,
           isTranslationSessionActive,
           shouldRecoverAfterSidecarRestart
        {
            shouldRecoverAfterSidecarRestart = false
            status = .idle
            Task { await captureSelectedWindow(automatically: true) }
        }
    }

    private func shouldHandleSelectedBrowserScroll() -> Bool {
        guard
            isTranslationSessionActive,
            let selectedWindow,
            let application = selectedWindow.owningApplication,
            NSWorkspace.shared.frontmostApplication?.processIdentifier
                == application.processID,
            isSelectedWindowAvailable()
        else {
            return false
        }

        guard
            let selectedWindowID,
            let currentFrame = Self.windowFrame(for: selectedWindowID),
            let cursorLocation = CGEvent(source: nil)?.location
        else {
            return true
        }
        return currentFrame.insetBy(dx: -4, dy: -4).contains(
            cursorLocation
        )
    }

    private func isSelectedWindowAvailable() -> Bool {
        guard let selectedWindowID else { return false }
        return Self.windowFrame(for: selectedWindowID) != nil
    }

    private func rememberTranslationContext(from regions: [SidecarRegion]) {
        for region in regions {
            let korean = region.original.trimmingCharacters(
                in: .whitespacesAndNewlines
            )
            let english = region.translation.trimmingCharacters(
                in: .whitespacesAndNewlines
            )
            guard !korean.isEmpty, !english.isEmpty else { continue }

            translationContext.removeAll {
                $0["korean"] == korean && $0["english"] == english
            }
            translationContext.append([
                "korean": korean,
                "english": english,
            ])
        }

        if translationContext.count > translationContextLimit {
            translationContext.removeFirst(
                translationContext.count - translationContextLimit
            )
        }
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
                guard let self else { return }
                let isFrontmost = self.isSelectedApplicationFrontmost()
                if !isFrontmost {
                    if self.isOverlayVisible && !self.overlayHiddenForAppSwitch {
                        self.overlayController.temporarilyHide()
                        self.overlayHiddenForAppSwitch = true
                    }
                } else if let frame = Self.windowFrame(for: selectedWindowID) {
                    self.overlayController.updateFrame(frame)
                    if self.isOverlayVisible && self.overlayHiddenForAppSwitch {
                        self.overlayController.show(over: frame)
                        self.overlayHiddenForAppSwitch = false
                    }
                }

                try? await Task.sleep(for: .milliseconds(100))
            }
        }
    }

    private func isSelectedApplicationFrontmost() -> Bool {
        guard let processID = selectedWindow?.owningApplication?.processID else {
            return false
        }
        return NSWorkspace.shared.frontmostApplication?.processIdentifier
            == processID
    }

    private func showTranslationOverlay(for regions: [SidecarRegion]) {
        guard
            let selectedWindow,
            let imageSize = pendingTranslationImageSize,
            imageSize.width > 0,
            imageSize.height > 0
        else {
            return
        }

        let readingArea = pendingTranslationReadingArea ?? CGRect(
            x: 0,
            y: 0,
            width: 1,
            height: 1
        )
        let overlayTranslations: [OverlayTranslation] = regions.compactMap {
            region in
            guard region.bbox.count == 4 else { return nil }

            let cropFrame = CGRect(
                x: CGFloat(region.bbox[0]) / imageSize.width,
                y: CGFloat(region.bbox[1]) / imageSize.height,
                width: CGFloat(region.bbox[2]) / imageSize.width,
                height: CGFloat(region.bbox[3]) / imageSize.height
            )
            let windowFrame = CGRect(
                x: readingArea.minX + cropFrame.minX * readingArea.width,
                y: readingArea.minY + cropFrame.minY * readingArea.height,
                width: cropFrame.width * readingArea.width,
                height: cropFrame.height * readingArea.height
            )
            .intersection(CGRect(x: 0, y: 0, width: 1, height: 1))

            guard
                windowFrame.width > 0,
                windowFrame.height > 0,
                !region.translation.trimmingCharacters(
                    in: .whitespacesAndNewlines
                ).isEmpty
            else {
                return nil
            }

            return OverlayTranslation(
                normalizedFrame: windowFrame,
                text: region.translation,
                regionType: region.regionType
            )
        }

        overlayController.showTranslations(
            overlayTranslations,
            over: selectedWindow.frame,
            monitorScroll: isTranslationSessionActive
        )
        isOverlayVisible = !overlayTranslations.isEmpty
        if isOverlayVisible && !isSelectedApplicationFrontmost() {
            overlayController.temporarilyHide()
            overlayHiddenForAppSwitch = true
        } else {
            overlayHiddenForAppSwitch = false
        }
        if isOverlayVisible {
            startTrackingWindow()
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
            (entry[kCGWindowIsOnscreen as String] as? Bool) == true,
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

    private func captureFingerprint(_ image: CGImage) throws -> [UInt8] {
        let width = 16
        let height = 16
        var pixels = [UInt8](repeating: 0, count: width * height)
        guard
            let context = CGContext(
                data: &pixels,
                width: width,
                height: height,
                bitsPerComponent: 8,
                bytesPerRow: width,
                space: CGColorSpaceCreateDeviceGray(),
                bitmapInfo: CGImageAlphaInfo.none.rawValue
            )
        else {
            throw CaptureError.couldNotCreateImageDestination
        }

        context.interpolationQuality = .low
        context.draw(
            image,
            in: CGRect(x: 0, y: 0, width: width, height: height)
        )
        return pixels
    }

    private func fingerprintDifference(
        _ left: [UInt8],
        _ right: [UInt8]
    ) -> Double {
        guard left.count == right.count, !left.isEmpty else {
            return .infinity
        }

        let totalDifference = zip(left, right).reduce(0) {
            $0 + abs(Int($1.0) - Int($1.1))
        }
        return Double(totalDifference) / Double(left.count)
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
