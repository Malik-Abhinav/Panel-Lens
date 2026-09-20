import Foundation
import CryptoKit
import Darwin

/// Installs the release's verified, relocatable runtime. Never invokes user Python or pip.
struct RuntimeInstaller {
    struct Manifest: Decodable {
        let version: String
        let platform: String
        let archive: String
        let sha256: String
        let installedBytes: Int64
    }

    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    static func prepare(resources: URL, support: URL, repair: Bool = false,
                        progress: @escaping @Sendable (String) -> Void) async throws -> URL {
        try await Task.detached(priority: .userInitiated) {
            #if !arch(arm64)
            throw Failure(message: "This release supports Apple Silicon Macs only. Intel Macs and Rosetta are not supported.")
            #else
            let files = FileManager.default
            let manifestURL = resources.appendingPathComponent("runtime-manifest.json")
            guard let data = try? Data(contentsOf: manifestURL) else {
                throw Failure(message: "The app's runtime package is missing. Reinstall the complete PanelLens app.")
            }
            let manifest = try JSONDecoder().decode(Manifest.self, from: data)
            guard manifest.platform == "macos-arm64", manifest.archive == "runtime.tar.gz",
                  manifest.sha256.count == 64,
                  manifest.sha256.allSatisfy({ $0.isHexDigit }) else {
                throw Failure(message: "Unsupported or invalid runtime manifest. Reinstall PanelLens.")
            }
            let runtimes = support.appendingPathComponent("runtimes", isDirectory: true)
            try files.createDirectory(at: runtimes, withIntermediateDirectories: true)
            // Serialize concurrent installers/repair across processes sharing this user account.
            let lock = open(runtimes.appendingPathComponent("install.lock").path, O_CREAT | O_RDWR, 0o600)
            guard lock >= 0 else { throw Failure(message: "Cannot create runtime installation lock. Check available disk space and permissions.") }
            defer { flock(lock, LOCK_UN); close(lock) }
            progress("Waiting for local runtime installer…")
            guard flock(lock, LOCK_EX) == 0 else { throw Failure(message: "Cannot lock runtime installation. Restart PanelLens.") }
            let destination = runtimes.appendingPathComponent("arm64-" + manifest.sha256, isDirectory: true)
            let python = destination.appendingPathComponent("python/bin/python3")
            let marker = destination.appendingPathComponent(".complete")
            if !repair, (try? String(contentsOf: marker, encoding: .utf8)) == manifest.sha256,
               files.isExecutableFile(atPath: python.path) {
                progress("Local runtime is installed.")
                return destination
            }
            let archive = resources.appendingPathComponent(manifest.archive)
            progress("Verifying bundled runtime checksum…")
            let handle = try FileHandle(forReadingFrom: archive)
            defer { try? handle.close() }
            var hasher = SHA256()
            while let chunk = try handle.read(upToCount: 8 * 1024 * 1024), !chunk.isEmpty { hasher.update(data: chunk) }
            let actual = hasher.finalize().map { String(format: "%02x", $0) }.joined()
            guard actual == manifest.sha256 else {
                throw Failure(message: "Runtime checksum failed. Download a fresh copy of PanelLens and reinstall it.")
            }
            let volume = try files.attributesOfFileSystem(forPath: support.path)
            if let free = volume[.systemFreeSize] as? NSNumber, free.int64Value < manifest.installedBytes + 512 * 1024 * 1024 {
                throw Failure(message: "Not enough disk space for the runtime. Free at least \((manifest.installedBytes / 1_000_000_000) + 1) GB and click Retry Runtime Installation.")
            }
            // A failed extraction never replaces a previously working version.
            let staging = runtimes.appendingPathComponent("staging-" + UUID().uuidString, isDirectory: true)
            try files.createDirectory(at: staging, withIntermediateDirectories: true)
            defer { try? files.removeItem(at: staging) }
            progress("Installing Python, translation engine, and OCR assets…")
            try run("/usr/bin/tar", ["-xzf", archive.path, "-C", staging.path], timeout: 300)
            let stagedPython = staging.appendingPathComponent("python/bin/python3")
            progress("Checking the installed runtime…")
            try run(stagedPython.path, ["-I", "-c", "import torch, transformers, paddle, paddleocr; assert torch.__version__ == '2.10.0'; assert transformers.__version__ == '5.16.1'; assert paddle.__version__ == '3.2.0'"], timeout: 180)
            try manifest.sha256.write(to: staging.appendingPathComponent(".complete"), atomically: true, encoding: .utf8)
            if files.fileExists(atPath: destination.path) { try files.removeItem(at: destination) }
            try files.moveItem(at: staging, to: destination)
            progress("Local runtime is ready.")
            return destination
            #endif
        }.value
    }

    private static func run(_ executable: String, _ arguments: [String], timeout: TimeInterval) throws {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        // Sanitized environment prevents accidental dependency on developer Python installations.
        process.environment = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": NSHomeDirectory(),
                               "PYTHONNOUSERSITE": "1", "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True"]
        let log = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".log")
        FileManager.default.createFile(atPath: log.path, contents: nil)
        let output = try FileHandle(forWritingTo: log)
        defer { try? output.close(); try? FileManager.default.removeItem(at: log) }
        process.standardOutput = output
        process.standardError = output
        let completed = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in completed.signal() }
        try process.run()
        if completed.wait(timeout: .now() + timeout) == .timedOut {
            process.terminate()
            if completed.wait(timeout: .now() + 5) == .timedOut { kill(process.processIdentifier, SIGKILL) }
            throw Failure(message: "Runtime setup timed out. Restart PanelLens and click Retry Runtime Installation.")
        }
        guard process.terminationStatus == 0 else {
            let detail = (try? String(contentsOf: log, encoding: .utf8)) ?? ""
            throw Failure(message: "Runtime setup failed. Click Retry Runtime Installation. \(detail.suffix(1200))")
        }
    }
}
