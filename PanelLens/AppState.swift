import Combine

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
}
