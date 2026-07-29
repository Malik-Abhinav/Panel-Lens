import Foundation

enum SidecarState: String {
    case stopped = "Stopped"
    case starting = "Starting"
    case ready = "Ready"
    case error = "Error"
}

struct SidecarRegion: Decodable {
    let bbox: [Double]
    let original: String
    let translation: String
    let language: String
    let confidence: Double
    let tone: String?
    let translationConfidence: Double?

    enum CodingKeys: String, CodingKey {
        case bbox
        case original
        case translation
        case language
        case confidence
        case tone
        case translationConfidence = "translation_confidence"
    }
}

struct SidecarResponse: Decodable {
    let requestID: String?
    let status: String
    let type: String?
    let regions: [SidecarRegion]?
    let processingTimeMS: Int?
    let ocrProcessingTimeMS: Int?
    let translationProcessingTimeMS: Int?
    let cacheHit: Bool?
    let error: SidecarError?

    enum CodingKeys: String, CodingKey {
        case requestID = "request_id"
        case status
        case type
        case regions
        case processingTimeMS = "processing_time_ms"
        case ocrProcessingTimeMS = "ocr_processing_time_ms"
        case translationProcessingTimeMS = "translation_processing_time_ms"
        case cacheHit = "cache_hit"
        case error
    }
}

struct SidecarError: Decodable {
    let code: String
    let message: String
}

@MainActor
final class SidecarClient {
    var onStateChange: ((SidecarState, String) -> Void)?
    var onResponse: ((SidecarResponse) -> Void)?

    private var process: Process?
    private var inputHandle: FileHandle?
    private var outputBuffer = Data()
    private var intentionalStop = false
    private var restartAttempts = 0

    func start() {
        guard process?.isRunning != true else {
            sendPing()
            return
        }

        guard
            let scriptURL = Self.sidecarScriptURL(),
            let pythonURL = Self.pythonURL(nextTo: scriptURL)
        else {
            publish(
                .error,
                "Python sidecar files were not found. Reopen the Xcode project from the repository."
            )
            return
        }

        intentionalStop = false
        publish(.starting, "Starting local Python sidecar…")

        let process = Process()
        let inputPipe = Pipe()
        let outputPipe = Pipe()
        let errorPipe = Pipe()

        process.executableURL = pythonURL
        process.arguments = ["-u", scriptURL.path]
        process.currentDirectoryURL = scriptURL.deletingLastPathComponent()
        process.standardInput = inputPipe
        process.standardOutput = outputPipe
        process.standardError = errorPipe

        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PANELLENS_SIDECAR_LOG"] = Self.logURL().path
        process.environment = environment

        outputPipe.fileHandleForReading.readabilityHandler = {
            [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }

            Task { @MainActor [weak self] in
                self?.consume(data)
            }
        }

        errorPipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            guard
                !data.isEmpty,
                let text = String(data: data, encoding: .utf8)
            else {
                return
            }

            Self.appendToLog(text)
        }

        process.terminationHandler = { [weak self] process in
            Task { @MainActor [weak self] in
                self?.handleTermination(status: process.terminationStatus)
            }
        }

        do {
            try process.run()
            self.process = process
            inputHandle = inputPipe.fileHandleForWriting
            sendPing()
        } catch {
            publish(
                .error,
                "Could not start Python sidecar: \(error.localizedDescription)"
            )
        }
    }

    func sendTestTranslation() {
        send(
            type: "translate",
            requestID: "test-\(UUID().uuidString)",
            payload: [
                "image_base64": "",
                "series": "PanelLens IPC Test",
                "chapter": 1,
            ]
        )
    }

    func translate(
        imageData: Data,
        series: String = "",
        chapter: Int? = nil
    ) {
        var payload: [String: Any] = [
            "image_base64": imageData.base64EncodedString(),
            "series": series,
        ]
        if let chapter {
            payload["chapter"] = chapter
        }

        send(type: "translate", payload: payload)
    }

    func stop() {
        intentionalStop = true
        inputHandle?.closeFile()
        process?.terminate()
        process = nil
        inputHandle = nil
        publish(.stopped, "Python sidecar stopped.")
    }

    private func sendPing() {
        send(type: "ping")
    }

    private func send(
        type: String,
        requestID: String = UUID().uuidString,
        payload: [String: Any] = [:]
    ) {
        guard process?.isRunning == true, let inputHandle else {
            publish(.error, "Python sidecar is not running.")
            return
        }

        var message = payload
        message["protocol_version"] = 1
        message["request_id"] = requestID
        message["type"] = type

        do {
            var data = try JSONSerialization.data(withJSONObject: message)
            data.append(0x0A)
            try inputHandle.write(contentsOf: data)
        } catch {
            publish(
                .error,
                "Sending to Python failed: \(error.localizedDescription)"
            )
        }
    }

    private func consume(_ data: Data) {
        outputBuffer.append(data)

        while let newlineIndex = outputBuffer.firstIndex(of: 0x0A) {
            let line = outputBuffer[..<newlineIndex]
            outputBuffer.removeSubrange(...newlineIndex)

            guard !line.isEmpty else { continue }

            do {
                let response = try JSONDecoder().decode(
                    SidecarResponse.self,
                    from: line
                )
                handle(response)
            } catch {
                publish(
                    .error,
                    "Python returned invalid data: \(error.localizedDescription)"
                )
            }
        }
    }

    private func handle(_ response: SidecarResponse) {
        if response.status == "ok", response.type == "pong" {
            restartAttempts = 0
            publish(.ready, "Python sidecar is ready.")
        } else if response.status == "error" {
            publish(
                .error,
                response.error?.message ?? "Python sidecar reported an error."
            )
        }

        onResponse?(response)
    }

    private func handleTermination(status: Int32) {
        process = nil
        inputHandle = nil
        outputBuffer.removeAll(keepingCapacity: true)

        if intentionalStop {
            publish(.stopped, "Python sidecar stopped.")
        } else {
            restartAttempts += 1

            guard restartAttempts <= 3 else {
                publish(
                    .error,
                    "Python sidecar repeatedly crashed and could not be restarted."
                )
                return
            }

            publish(
                .starting,
                "Python sidecar exited with status \(status). Restarting…"
            )

            Task { [weak self] in
                try? await Task.sleep(for: .milliseconds(500))
                guard !Task.isCancelled else { return }
                self?.start()
            }
        }
    }

    private func publish(_ state: SidecarState, _ message: String) {
        onStateChange?(state, message)
    }

    private static func sidecarScriptURL() -> URL? {
        if let bundledURL = Bundle.main.url(
            forResource: "main",
            withExtension: "py",
            subdirectory: "sidecar"
        ) {
            return bundledURL
        }

        let sourceFile = URL(fileURLWithPath: #filePath)
        let repositoryRoot = sourceFile
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let developmentURL = repositoryRoot
            .appendingPathComponent("sidecar")
            .appendingPathComponent("main.py")

        return FileManager.default.fileExists(atPath: developmentURL.path)
            ? developmentURL
            : nil
    }

    private static func pythonURL(nextTo scriptURL: URL) -> URL? {
        let virtualEnvironmentPython = scriptURL
            .deletingLastPathComponent()
            .appendingPathComponent(".venv/bin/python3")
        if FileManager.default.isExecutableFile(
            atPath: virtualEnvironmentPython.path
        ) {
            return virtualEnvironmentPython
        }

        let systemPython = URL(fileURLWithPath: "/usr/bin/python3")
        return FileManager.default.isExecutableFile(atPath: systemPython.path)
            ? systemPython
            : nil
    }

    nonisolated private static func logURL() -> URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/PanelLens", isDirectory: true)
            .appendingPathComponent("sidecar.log")
    }

    nonisolated private static func appendToLog(_ text: String) {
        let logURL = logURL()

        do {
            try FileManager.default.createDirectory(
                at: logURL.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )

            guard let data = text.data(using: .utf8) else { return }
            if FileManager.default.fileExists(atPath: logURL.path) {
                let handle = try FileHandle(forWritingTo: logURL)
                try handle.seekToEnd()
                try handle.write(contentsOf: data)
                try handle.close()
            } else {
                try data.write(to: logURL)
            }
        } catch {
            // Logging cannot use stdout because stdout is reserved for IPC.
        }
    }
}
