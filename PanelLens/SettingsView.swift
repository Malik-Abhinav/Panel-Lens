import SwiftUI

struct SettingsView: View {
    var body: some View {
        Form {
            LabeledContent("MVP source language", value: "Korean")
            LabeledContent("Target language", value: "English")

            Text("Window capture and local translation settings will appear here as they are implemented.")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
        .padding(20)
        .frame(width: 440)
    }
}

