import AppKit
import SwiftUI

@MainActor
final class OverlayController {
    private let panel: NSPanel

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
        panel.contentView = NSHostingView(rootView: TestOverlayView())
    }

    func show(over windowFrame: CGRect) {
        panel.ignoresMouseEvents = true
        panel.contentView = NSHostingView(rootView: TestOverlayView())
        updateFrame(windowFrame)
        panel.orderFrontRegardless()
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

private struct TestOverlayView: View {
    var body: some View {
        GeometryReader { geometry in
            ZStack {
                Rectangle()
                    .stroke(Color.cyan, lineWidth: 4)

                marker("TOP LEFT", color: .yellow)
                    .position(x: 78, y: 28)

                marker("CENTER", color: .cyan)
                    .position(
                        x: geometry.size.width / 2,
                        y: geometry.size.height / 2
                    )

                marker("BOTTOM RIGHT", color: .pink)
                    .position(
                        x: max(92, geometry.size.width - 92),
                        y: max(28, geometry.size.height - 28)
                    )
            }
        }
        .background(Color.clear)
    }

    private func marker(_ title: String, color: Color) -> some View {
        Text(title)
            .font(.system(size: 11, weight: .bold, design: .rounded))
            .foregroundStyle(.black)
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(color)
            .clipShape(RoundedRectangle(cornerRadius: 6))
    }
}
