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
