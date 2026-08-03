import AppKit
import SwiftUI

struct OverlayTranslation: Identifiable {
    let id = UUID()
    let normalizedFrame: CGRect
    let text: String
    let regionType: String?
}

@MainActor
final class OverlayController {
    var onDismissForScroll: (() -> Void)?
    var onScrollActivity: (() -> Void)?
    var shouldHandleScroll: (() -> Bool)?

    private let panel: NSPanel
    private var translations: [OverlayTranslation] = []
    private var scrollMonitor: Any?
    private var accumulatedScroll: CGFloat = 0
    private var lastScrollEventAt: Date?
    private var dismissedForCurrentScroll = false

    private let scrollDismissThreshold: CGFloat = 24
    private let scrollSequenceTimeout: TimeInterval = 0.6

    init() {
        panel = NSPanel(
            contentRect: .zero,
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )

        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = false
        panel.ignoresMouseEvents = true
        panel.hidesOnDeactivate = false
        panel.level = .floating
        panel.collectionBehavior = [
            .canJoinAllSpaces,
            .fullScreenAuxiliary,
            .stationary
        ]
        panel.contentView = NSHostingView(
            rootView: TranslationOverlayView(translations: [])
        )
    }

    func show(over windowFrame: CGRect) {
        panel.ignoresMouseEvents = true
        renderTranslations()
        updateFrame(windowFrame)
        panel.orderFrontRegardless()
    }

    func showTranslations(
        _ translations: [OverlayTranslation],
        over windowFrame: CGRect,
        monitorScroll: Bool = true
    ) {
        self.translations = translations
        if monitorScroll {
            startMonitoringScroll()
        } else {
            stopMonitoringScroll()
        }
        show(over: windowFrame)
    }

    func pauseScrollMonitoring() {
        stopMonitoringScroll()
    }

    func clearTranslations() {
        translations = []
        stopMonitoringScroll()
        renderTranslations()
    }

    func hideTranslationsWhileMonitoringScroll() {
        translations = []
        renderTranslations()
        panel.orderOut(nil)
    }

    func temporarilyHide() {
        panel.orderOut(nil)
    }

    func selectReadingArea(
        over windowFrame: CGRect,
        onComplete: @escaping (CGRect, CGSize) -> Void,
        onCancel: @escaping () -> Void
    ) {
        panel.ignoresMouseEvents = false
        panel.contentView = NSHostingView(
            rootView: ReadingAreaSelectionView(
                onComplete: onComplete,
                onCancel: onCancel
            )
        )
        updateFrame(windowFrame)
        panel.orderFrontRegardless()
    }

    func updateFrame(_ windowFrame: CGRect) {
        guard let appKitFrame = appKitFrame(for: windowFrame) else {
            return
        }

        panel.setFrame(appKitFrame, display: true)
    }

    func hide() {
        panel.ignoresMouseEvents = true
        panel.orderOut(nil)
        stopMonitoringScroll()
    }

    private func renderTranslations() {
        panel.contentView = NSHostingView(
            rootView: TranslationOverlayView(translations: translations)
        )
    }

    private func startMonitoringScroll() {
        stopMonitoringScroll()
        accumulatedScroll = 0
        lastScrollEventAt = nil
        dismissedForCurrentScroll = false
        scrollMonitor = NSEvent.addGlobalMonitorForEvents(
            matching: .scrollWheel
        ) { [weak self] event in
            Task { @MainActor [weak self] in
                self?.handleScroll(event)
            }
        }
    }

    private func stopMonitoringScroll() {
        if let scrollMonitor {
            NSEvent.removeMonitor(scrollMonitor)
            self.scrollMonitor = nil
        }
        accumulatedScroll = 0
        lastScrollEventAt = nil
        dismissedForCurrentScroll = false
    }

    private func handleScroll(_ event: NSEvent) {
        guard shouldHandleScroll?() != false else { return }

        let now = Date()
        if dismissedForCurrentScroll {
            lastScrollEventAt = now
            onScrollActivity?()
            return
        }

        guard panel.isVisible, !translations.isEmpty else { return }

        if let lastScrollEventAt,
           now.timeIntervalSince(lastScrollEventAt) > scrollSequenceTimeout
        {
            accumulatedScroll = 0
        }
        lastScrollEventAt = now

        let delta = event.hasPreciseScrollingDeltas
            ? event.scrollingDeltaY
            : event.scrollingDeltaY * 12
        if accumulatedScroll.sign != delta.sign, accumulatedScroll != 0 {
            accumulatedScroll = delta
        } else {
            accumulatedScroll += delta
        }

        guard abs(accumulatedScroll) >= scrollDismissThreshold else {
            return
        }

        translations = []
        panel.orderOut(nil)
        dismissedForCurrentScroll = true
        onDismissForScroll?()
        onScrollActivity?()
    }

    private func appKitFrame(for coreGraphicsFrame: CGRect) -> CGRect? {
        for screen in NSScreen.screens {
            guard
                let screenNumber = screen.deviceDescription[
                    NSDeviceDescriptionKey("NSScreenNumber")
                ] as? NSNumber
            else {
                continue
            }

            let displayID = CGDirectDisplayID(screenNumber.uint32Value)
            let displayBounds = CGDisplayBounds(displayID)

            guard displayBounds.intersects(coreGraphicsFrame) else {
                continue
            }

            let localX = coreGraphicsFrame.minX - displayBounds.minX
            let localYFromTop = coreGraphicsFrame.minY - displayBounds.minY

            return CGRect(
                x: screen.frame.minX + localX,
                y: screen.frame.maxY - localYFromTop - coreGraphicsFrame.height,
                width: coreGraphicsFrame.width,
                height: coreGraphicsFrame.height
            )
        }

        guard let desktopTop = NSScreen.screens.map(\.frame.maxY).max() else {
            return nil
        }

        return CGRect(
            x: coreGraphicsFrame.minX,
            y: desktopTop - coreGraphicsFrame.maxY,
            width: coreGraphicsFrame.width,
            height: coreGraphicsFrame.height
        )
    }
}

private struct ReadingAreaSelectionView: View {
    let onComplete: (CGRect, CGSize) -> Void
    let onCancel: () -> Void

    @State private var dragStart: CGPoint?
    @State private var selection: CGRect?

    var body: some View {
        GeometryReader { geometry in
            ZStack {
                Rectangle()
                    .fill(Color.black.opacity(0.42))
                    .contentShape(Rectangle())
                    .gesture(selectionGesture)

                if let selection {
                    Rectangle()
                        .fill(Color.white.opacity(0.12))
                        .overlay {
                            Rectangle()
                                .stroke(Color.cyan, lineWidth: 3)
                        }
                        .frame(
                            width: selection.width,
                            height: selection.height
                        )
                        .position(
                            x: selection.midX,
                            y: selection.midY
                        )
                        .allowsHitTesting(false)
                }

                VStack {
                    Text("Drag around only the manhwa reader")
                        .font(.headline)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 9)
                        .background(.black.opacity(0.8))
                        .foregroundStyle(.white)
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                        .padding(.top, 18)

                    Spacer()

                    HStack {
                        Button("Cancel") {
                            onCancel()
                        }

                        Button("Use Selected Area") {
                            guard let selection else { return }
                            onComplete(selection, geometry.size)
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(!hasUsableSelection)
                    }
                    .padding(12)
                    .background(.black.opacity(0.8))
                    .clipShape(RoundedRectangle(cornerRadius: 10))
                    .padding(.bottom, 18)
                }
            }
        }
    }

    private var selectionGesture: some Gesture {
        DragGesture(minimumDistance: 0)
            .onChanged { value in
                let start = dragStart ?? value.startLocation
                dragStart = start
                selection = CGRect(
                    x: min(start.x, value.location.x),
                    y: min(start.y, value.location.y),
                    width: abs(value.location.x - start.x),
                    height: abs(value.location.y - start.y)
                )
            }
            .onEnded { _ in
                dragStart = nil
            }
    }

    private var hasUsableSelection: Bool {
        guard let selection else { return false }
        return selection.width >= 100 && selection.height >= 100
    }
}

private struct TranslationOverlayView: View {
    let translations: [OverlayTranslation]

    var body: some View {
        GeometryReader { geometry in
            ZStack {
                ForEach(translations) { translation in
                    translationCard(translation, in: geometry.size)
                }
            }
        }
        .background(Color.clear)
        .allowsHitTesting(false)
    }

    private func translationCard(
        _ translation: OverlayTranslation,
        in canvasSize: CGSize
    ) -> some View {
        let frame = CGRect(
            x: translation.normalizedFrame.minX * canvasSize.width,
            y: translation.normalizedFrame.minY * canvasSize.height,
            width: translation.normalizedFrame.width * canvasSize.width,
            height: translation.normalizedFrame.height * canvasSize.height
        )
        let expandedFrame = frame.insetBy(dx: -5, dy: -4)
        let fontSize = min(
            28,
            max(10, min(expandedFrame.height * 0.28, expandedFrame.width * 0.12))
        )

        return Text(translation.text)
            .font(.system(size: fontSize, weight: .semibold, design: .rounded))
            .multilineTextAlignment(.center)
            .lineLimit(6)
            .minimumScaleFactor(0.45)
            .foregroundStyle(.black)
            .padding(.horizontal, 5)
            .padding(.vertical, 3)
            .frame(
                width: max(36, expandedFrame.width),
                height: max(24, expandedFrame.height)
            )
            .background {
                RoundedRectangle(cornerRadius: 8)
                    .fill(Color.white.opacity(0.94))
                    .shadow(color: .black.opacity(0.22), radius: 2, y: 1)
            }
            .overlay {
                RoundedRectangle(cornerRadius: 8)
                    .stroke(Color.black.opacity(0.16), lineWidth: 1)
            }
            .position(
                x: expandedFrame.midX,
                y: expandedFrame.midY
            )
    }
}
