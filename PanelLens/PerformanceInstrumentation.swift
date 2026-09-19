import Foundation

/// Opt-in JSONL performance events. Payloads must never include captured pixels,
/// recognized text, or translations.
enum PerformanceInstrumentation {
    private static let lock = NSLock()

    static var isEnabled: Bool {
        let value = ProcessInfo.processInfo.environment[
            "PANELLENS_PERF_ENABLED"
        ]?.lowercased()
        return ["1", "true", "yes", "on"].contains(value)
    }

    static func now() -> UInt64 {
        DispatchTime.now().uptimeNanoseconds
    }

    static func emit(
        _ stage: String,
        requestID: String,
        imageID: String,
        priority: String = "visible",
        cacheLevel: String = "none",
        modelState: String = "unknown",
        timestamp: UInt64? = nil,
        startedAt: UInt64? = nil,
        fields: [String: Any] = [:]
    ) {
        guard isEnabled else { return }
        let timestamp = timestamp ?? now()
        var event: [String: Any] = [
            "schema_version": 1,
            "timestamp_ns": timestamp,
            "stage": stage,
            "request_id": requestID,
            "image_id": imageID,
            "priority": priority,
            "cache_level": cacheLevel,
            "model_state": modelState,
        ]
        if let startedAt {
            event["duration_ms"] = Double(timestamp - startedAt) / 1_000_000
        }
        for (key, value) in fields {
            event[key] = value
        }
        guard let data = try? JSONSerialization.data(withJSONObject: event) else {
            return
        }
        write(data + Data([0x0A]))
    }

    private static func write(_ data: Data) {
        lock.lock()
        defer { lock.unlock() }
        if let path = ProcessInfo.processInfo.environment["PANELLENS_PERF_LOG"],
           !path.isEmpty
        {
            let url = URL(fileURLWithPath: path)
            try? FileManager.default.createDirectory(
                at: url.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            if !FileManager.default.fileExists(atPath: path) {
                FileManager.default.createFile(atPath: path, contents: nil)
            }
            if let handle = try? FileHandle(forWritingTo: url) {
                defer { try? handle.close() }
                _ = try? handle.seekToEnd()
                try? handle.write(contentsOf: data)
            }
        } else {
            try? FileHandle.standardError.write(contentsOf: data)
        }
    }
}
