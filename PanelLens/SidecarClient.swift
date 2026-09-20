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
    let regionType: String?
    let tone: String?
    let translationConfidence: Double?

    enum CodingKeys: String, CodingKey {
        case bbox
        case original
        case translation
        case language
        case confidence
        case regionType = "region_type"
        case tone
        case translationConfidence = "translation_confidence"
    }
}

struct SidecarResponse: Decodable {
    let requestID: String?
    let imageID: String?
    let modelState: String?
    let status: String
    let type: String?
    let regions: [SidecarRegion]?
    let processingTimeMS: Int?
    let ocrProcessingTimeMS: Int?
    let translationProcessingTimeMS: Int?
    let cacheHit: Bool?
    let detectedTextCount: Int?
    let filteredTextCount: Int?
    let error: SidecarError?
    let runtime: SidecarRuntime?
    let browserBridge: BrowserBridgeStatus?
    let settings: TranslationSettings?
    let performance: OllamaPerformanceMetrics?

    enum CodingKeys: String, CodingKey {
        case requestID = "request_id"
        case imageID = "image_id"
        case modelState = "model_state"
        case status
        case type
        case regions
        case processingTimeMS = "processing_time_ms"
        case ocrProcessingTimeMS = "ocr_processing_time_ms"
        case translationProcessingTimeMS = "translation_processing_time_ms"
        case cacheHit = "cache_hit"
        case detectedTextCount = "detected_text_count"
        case filteredTextCount = "filtered_text_count"
        case error
        case runtime
        case browserBridge = "browser_bridge"
        case settings
        case performance
    }
}

struct OllamaPerformanceMetrics: Decodable {
    let loadDurationMS: Double?
    let promptEvaluationDurationMS: Double?
    let generationDurationMS: Double?
    let totalDurationMS: Double?
    let promptTokenCount: Int?
    let generatedTokenCount: Int?
    let tokensPerSecond: Double?

    enum CodingKeys: String, CodingKey {
        case loadDurationMS = "ollama_load_duration_ms"
        case promptEvaluationDurationMS = "ollama_prompt_eval_duration_ms"
        case generationDurationMS = "ollama_generation_duration_ms"
        case totalDurationMS = "ollama_total_duration_ms"
        case promptTokenCount = "ollama_prompt_token_count"
        case generatedTokenCount = "ollama_generated_token_count"
        case tokensPerSecond = "ollama_tokens_per_second"
    }
}

struct TranslationSettings: Decodable {
    let provider: String
    let model: String
    let ollamaModels: [String]
    let runtime: SidecarRuntime
    enum CodingKeys: String, CodingKey {
        case provider, model, runtime
        case ollamaModels = "ollama_models"
    }
}

struct BrowserBridgeStatus: Decodable {
    let ready: Bool
    let code: String
    let message: String
}

struct SidecarRuntime: Decodable {
    let ready: Bool
    let code: String
    let model: String
    let message: String
}

struct SidecarError: Decodable {
    let code: String
    let message: String
}

@MainActor
final class SidecarClient {
    var onStateChange: ((SidecarState, String) -> Void)?
    var onResponse: ((SidecarResponse) -> Void)?
    var onBridgeChange: ((BrowserBridgeStatus) -> Void)?
    var onRuntimeChange: ((SidecarRuntime) -> Void)?

    /// The process identifier of the running Python sidecar, if any. Used by
    /// the performance monitor to report how much CPU and memory OCR/translation
    /// consume.
    var runningProcessPID: Int32? { process?.processIdentifier }

    private let resourcesURL: URL?
    private let supportURL: URL
    private var installationTask: Task<Void, Never>?
    private var pendingSelection: (String, String)?
    private var pendingSettingsRead = false

    init(resourcesURL: URL? = Bundle.main.resourceURL, supportURL: URL? = nil) {
        self.resourcesURL = resourcesURL
        self.supportURL = supportURL ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("PanelLens", isDirectory: true)
    }

    private var process: Process?
    private var inputHandle: FileHandle?
    private var outputBuffer = Data()
    private var activeProcessID = UUID()
    private var intentionalStop = false
    private var restartAttempts = 0

    func start() {
        guard process?.isRunning != true else {
            sendPing()
            return
        }

        guard installationTask == nil else { return }
        guard let resourcesURL else {
            publish(.error, "PanelLens resources are missing. Reinstall the app.")
            return
        }
        installationTask = Task { [weak self] in
            guard let self else { return }
            defer { installationTask = nil }
            do {
                let runtime = try await RuntimeInstaller.prepare(resources: resourcesURL, support: supportURL) { [weak self] message in
                    Task { @MainActor in self?.publish(.starting, message) }
                }
                guard !Task.isCancelled else { return }
                launch(runtime: runtime, resources: resourcesURL)
            } catch {
                publish(.error, error.localizedDescription)
            }
        }
    }

    func repairRuntime() {
        guard installationTask == nil, let resourcesURL else { return }
        stop()
        installationTask = Task { [weak self] in
            guard let self else { return }
            defer { installationTask = nil }
            do {
                let runtime = try await RuntimeInstaller.prepare(resources: resourcesURL, support: supportURL, repair: true) { [weak self] message in
                    Task { @MainActor in self?.publish(.starting, message) }
                }
                guard !Task.isCancelled else { return }
                launch(runtime: runtime, resources: resourcesURL)
            } catch { publish(.error, error.localizedDescription) }
        }
    }

    private func launch(runtime: URL, resources: URL) {
        let scriptURL = resources.appendingPathComponent("sidecar/main.py")
        let pythonURL = runtime.appendingPathComponent("python/bin/python3")
        guard FileManager.default.fileExists(atPath: scriptURL.path) else {
            publish(.error, "PanelLens engine files are missing. Reinstall the app.")
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
        for name in ["PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "PANELLENS_TRANSLATION_RUNTIME"] { environment.removeValue(forKey: name) }
        environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PANELLENS_DEFER_WARMUP"] = "1"
        environment["PANELLENS_BROWSER_BRIDGE"] = "1"
        environment["PANELLENS_HTTP_PORT"] = "8765"
        do { environment["PANELLENS_HTTP_TOKEN"] = try connectionKey() }
        catch { publish(.error, "Could not prepare browser connection: \(error.localizedDescription)"); return }
        environment["PANELLENS_MODEL_CACHE"] = supportURL.appendingPathComponent("models").path
        environment["PANELLENS_SETTINGS_PATH"] = supportURL.appendingPathComponent("translation.json").path
        environment["PANELLENS_CACHE_DB"] = supportURL.appendingPathComponent("cache.sqlite").path
        environment["PANELLENS_OCR_ASSETS"] = runtime.appendingPathComponent("ocr").path
        environment["PADDLE_PDX_CACHE_HOME"] = supportURL.appendingPathComponent("ocr-cache").path
        environment["HF_HOME"] = supportURL.appendingPathComponent("huggingface").path
        environment["SSL_CERT_FILE"] = environment["SSL_CERT_FILE"] ?? runtime.appendingPathComponent("python/lib/python3.12/site-packages/certifi/cacert.pem").path
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

        let launchedPID = UUID()
        activeProcessID = launchedPID
        process.terminationHandler = { [weak self] process in
            Task { @MainActor [weak self] in
                guard self?.activeProcessID == launchedPID else { return }
                self?.handleTermination(status: process.terminationStatus)
            }
        }

        do {
            try process.run()
            self.process = process
            inputHandle = inputPipe.fileHandleForWriting
            sendPing()
            if let selection = pendingSelection {
                pendingSelection = nil
                configureTranslation(provider: selection.0, model: selection.1)
            } else if pendingSettingsRead {
                pendingSettingsRead = false
                readTranslationSettings()
            }
        } catch {
            publish(
                .error,
                "Could not start Python sidecar: \(error.localizedDescription)"
            )
        }
    }

    func connectionKey() throws -> String {
        let file = supportURL.appendingPathComponent("browser-key")
        if let key = try? String(contentsOf: file, encoding: .utf8), key.count == 64 {
            return key
        }
        try FileManager.default.createDirectory(at: supportURL, withIntermediateDirectories: true)
        let key = (UUID().uuidString + UUID().uuidString).replacingOccurrences(of: "-", with: "").lowercased()
        try key.write(to: file, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
        return key
    }

    func revealExtension() throws {
        guard let resourcesURL else { return }
        let source = resourcesURL.appendingPathComponent("extension")
        let destination = supportURL.appendingPathComponent("BrowserExtension")
        try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)
        for name in ["manifest.json", "popup.html", "popup.js", "service-worker.js", "content-script.js", "prefetch-core.js", "overlay.css"] {
            let target = destination.appendingPathComponent(name)
            if FileManager.default.fileExists(atPath: target.path) { try FileManager.default.removeItem(at: target) }
            try FileManager.default.copyItem(at: source.appendingPathComponent(name), to: target)
        }
        // Return the stable directory; the UI reveals it using Finder.
    }

    var extensionDirectory: URL { supportURL.appendingPathComponent("BrowserExtension") }

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

    func readTranslationSettings() {
        guard process?.isRunning == true else {
            pendingSettingsRead = true
            start()
            return
        }
        send(type: "translation_settings")
    }

    func configureTranslation(provider: String, model: String) {
        guard process?.isRunning == true else {
            pendingSelection = (provider, model)
            start()
            return
        }
        send(type: "configure_translation", payload: ["provider": provider, "model": model])
    }

    func checkRuntime() {
        if process?.isRunning == true {
            sendPing()
        } else {
            start()
        }
    }

    func translate(
        imageData: Data,
        requestID: String,
        imageID: String,
        priority: String = "visible",
        series: String = "",
        chapter: Int? = nil,
        context: [[String: String]] = []
    ) -> Bool {
        var payload: [String: Any] = [
            "image_base64": imageData.base64EncodedString(),
            "image_id": imageID,
            "priority": priority,
            "series": series,
            "context": context,
        ]
        if let chapter {
            payload["chapter"] = chapter
        }

        return send(
            type: "translate",
            requestID: requestID,
            payload: payload
        )
    }

    func stop() {
        intentionalStop = true
        activeProcessID = UUID()
        inputHandle?.closeFile()
        process?.terminate()
        process = nil
        inputHandle = nil
        publish(.stopped, "Python sidecar stopped.")
    }

    private func sendPing() {
        send(type: "ping")
    }

    @discardableResult
    private func send(
        type: String,
        requestID: String = UUID().uuidString,
        payload: [String: Any] = [:]
    ) -> Bool {
        guard process?.isRunning == true, let inputHandle else {
            publish(.error, "Python sidecar is not running.")
            return false
        }

        var message = payload
        message["protocol_version"] = 1
        message["request_id"] = requestID
        message["type"] = type

        do {
            var data = try JSONSerialization.data(withJSONObject: message)
            data.append(0x0A)
            try inputHandle.write(contentsOf: data)
            return true
        } catch {
            publish(
                .error,
                "Sending to Python failed: \(error.localizedDescription)"
            )
            return false
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
        if let bridge = response.browserBridge { onBridgeChange?(bridge) }
        if response.status == "ok", response.type == "pong" {
            restartAttempts = 0
            if let runtime = response.runtime {
                onRuntimeChange?(runtime)
            }
            if let runtime = response.runtime, !runtime.ready {
                publish(["loading", "downloading", "verifying"].contains(runtime.code) ? .starting : .error, runtime.message)
            } else {
                publish(
                    .ready,
                    response.runtime?.message ?? "Python sidecar is ready."
                )
            }
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
