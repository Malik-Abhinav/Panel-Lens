import SwiftUI

struct WindowPickerView: View {
    @ObservedObject var appState: AppState
    @Environment(\.dismiss) private var dismiss

    private var browserWindows: [WindowOption] {
        appState.availableWindows.filter(\.isBrowser)
    }

    private var otherWindows: [WindowOption] {
        appState.availableWindows.filter { !$0.isBrowser }
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()

            if appState.isLoadingWindows {
                ProgressView("Loading available windows…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if appState.availableWindows.isEmpty {
                ContentUnavailableView(
                    "No Windows Found",
                    systemImage: "macwindow.badge.plus",
                    description: Text(
                        "Open the browser page you want to translate, then refresh."
                    )
                )
            } else {
                List {
                    if !browserWindows.isEmpty {
                        Section("Browsers") {
                            ForEach(browserWindows) { option in
                                windowRow(option)
                            }
                        }
                    }

                    if !otherWindows.isEmpty {
                        Section("Other Windows") {
                            ForEach(otherWindows) { option in
                                windowRow(option)
                            }
                        }
                    }
                }
                .listStyle(.inset)
            }

            Divider()
            footer
        }
        .task {
            if appState.availableWindows.isEmpty {
                await appState.refreshWindows()
            }
        }
    }

    private var header: some View {
        HStack {
            VStack(alignment: .leading, spacing: 4) {
                Text("Select a window")
                    .font(.title2.weight(.semibold))
                Text("PanelLens captures only the window you choose.")
                    .foregroundStyle(.secondary)
            }

            Spacer()

            Button {
                Task {
                    await appState.refreshWindows()
                }
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
            .disabled(appState.isLoadingWindows)

            if !appState.hasScreenCapturePermission {
                Button("Privacy Settings") {
                    appState.openScreenRecordingSettings()
                }
            }
        }
        .padding()
    }

    private var footer: some View {
        HStack {
            Text(appState.message)
                .font(.caption)
                .foregroundStyle(
                    appState.status == .error ? .red : .secondary
                )
                .lineLimit(2)

            Spacer()

            Button("Cancel") {
                dismiss()
            }
            .keyboardShortcut(.cancelAction)
        }
        .padding()
    }

    private func windowRow(_ option: WindowOption) -> some View {
        Button {
            appState.select(option)
            dismiss()
        } label: {
            HStack(spacing: 12) {
                Image(systemName: option.isBrowser ? "globe" : "macwindow")
                    .font(.title2)
                    .frame(width: 30)
                    .foregroundStyle(option.isBrowser ? .blue : .secondary)

                VStack(alignment: .leading, spacing: 3) {
                    Text(option.title)
                        .lineLimit(1)
                    Text(
                        "\(option.applicationName) • \(Int(option.frame.width))×\(Int(option.frame.height)) pt"
                    )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }

                Spacer()

                if appState.selectedWindowID == option.id {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundStyle(.green)
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .padding(.vertical, 4)
    }
}
