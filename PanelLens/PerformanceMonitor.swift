import Darwin
import Foundation
import IOKit.ps
import IOKit

/// A snapshot of the resource usage that matters for a local OCR/translation
/// app: how much memory the app, the Python sidecar, and the Ollama model
/// server are using, plus system memory pressure and battery state.
///
/// `cpuPercent` values are best-effort estimates derived from per-process CPU
/// time deltas; treat them as approximate. The memory and battery figures come
/// directly from the kernel and IOKit and are reliable.
struct ResourceSnapshot {
    let appMemoryMB: Double
    let sidecarMemoryMB: Double
    let ollamaMemoryMB: Double

    let systemUsedGB: Double
    let systemTotalGB: Double

    let batteryPercent: Int?
    let isCharging: Bool

    let appCPUPercent: Double
    let sidecarCPUPercent: Double
    let ollamaCPUPercent: Double

    /// True when total RAM usage is high enough to risk swapping/throttling.
    var memoryPressureHigh: Bool {
        systemTotalGB > 0 && (systemUsedGB / systemTotalGB) >= 0.85
    }

    /// The two Ollama helper processes that host the loaded model. The model
    /// server process can differ slightly between versions, so match loosely.
    static let ollamaProcessNames = ["ollama", "ollama_llama_server", "ollama_runner"]
}

/// Samples resource usage for the current process and for processes we care
/// about (the sidecar we launched, and the Ollama model server).
enum PerformanceMonitor {

    struct TaskSample {
        var residentBytes: UInt64 = 0
        var cpuTotal: UInt64 = 0
    }

    /// Reads resident memory and total CPU time for a process via the kernel.
    static func taskSample(pid: Int32) -> TaskSample? {
        var info = proc_taskinfo()
        let size = MemoryLayout<proc_taskinfo>.size
        let result = withUnsafeMutablePointer(to: &info) { pointer in
            pointer.withMemoryRebound(to: Int8.self, capacity: size) { raw in
                proc_pidinfo(pid, PROC_PIDTASKINFO, 0, raw, Int32(size))
            }
        }
        guard result > 0 else { return nil }
        var sample = TaskSample()
        sample.residentBytes = UInt64(info.pti_resident_size)
        // Kernels report these in the same fixed timebase, so a delta ratio to
        // wall clock yields an approximate CPU percentage.
        sample.cpuTotal = info.pti_total_user + info.pti_total_system
        return sample
    }

    static func memoryMB(_ bytes: UInt64) -> Double {
        Double(bytes) / (1024 * 1024)
    }

    /// All PIDs whose process name equals `name`.
    static func pids(named name: String) -> [Int32] {
        var pids = [pid_t](repeating: 0, count: 2048)
        let result = proc_listallpids(
            &pids,
            Int32(MemoryLayout<pid_t>.size * pids.count)
        )
        guard result > 0 else { return [] }

        var matches: [Int32] = []
        for index in 0..<Int(result) where pids[index] > 0 {
            var buffer = [CChar](repeating: 0, count: Int(MAXCOMLEN) + 1)
            let length = proc_name(pids[index], &buffer, UInt32(buffer.count))
            if length > 0, String(cString: buffer) == name {
                matches.append(pids[index])
            }
        }
        return matches
    }

    /// Combined resident memory for a set of process names (Ollama).
    static func ollamaMemory() -> (memoryMB: Double, pidCount: Int) {
        var total: UInt64 = 0
        var count = 0
        for name in ResourceSnapshot.ollamaProcessNames {
            for pid in pids(named: name) {
                if let sample = taskSample(pid: pid) {
                    total += sample.residentBytes
                    count += 1
                }
            }
        }
        return (memoryMB(total), count)
    }

    /// System RAM usage from vm_statistics64 (active + inactive + wired).
    static func systemMemory() -> (usedGB: Double, totalGB: Double) {
        let total = Double(ProcessInfo.processInfo.physicalMemory)
            / (1024 * 1024 * 1024)

        var stats = vm_statistics64()
        var count = mach_msg_type_number_t(
            MemoryLayout<vm_statistics64_data_t>.size
                / MemoryLayout<integer_t>.size
        )
        let kr = withUnsafeMutablePointer(to: &stats) { pointer in
            pointer.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
                inner in
                host_statistics64(
                    mach_host_self(),
                    HOST_VM_INFO64,
                    inner,
                    &count
                )
            }
        }
        guard kr == KERN_SUCCESS else { return (0, total) }

        let pageSize = Double(vm_kernel_page_size)
        let usedPages = Double(
            stats.active_count + stats.inactive_count + stats.wire_count
        )
        let usedGB = usedPages * pageSize / (1024 * 1024 * 1024)
        return (usedGB, total)
    }

    /// Battery percentage and whether the Mac is currently charging, via IOKit.
    static func battery() -> (percent: Int?, isCharging: Bool) {
        guard
            let blob = IOPSCopyPowerSourcesInfo()?.takeUnretainedValue(),
            let list = IOPSCopyPowerSourcesList(blob)?.takeUnretainedValue()
        else {
            return (nil, false)
        }

        var percent: Int?
        var charging = false
        for index in 0..<CFArrayGetCount(list) {
            guard
                let source = CFArrayGetValueAtIndex(list, index),
                let description = IOPSGetPowerSourceDescription(
                    blob,
                    Unmanaged<CFTypeRef>.fromOpaque(source).takeUnretainedValue()
                )?.takeUnretainedValue() as? [String: Any]
            else {
                continue
            }
            if let capacity = description[kIOPSCurrentCapacityKey] as? Int {
                percent = capacity
            }
            if let isCharging = description[kIOPSIsChargingKey] as? Bool {
                charging = isCharging
            }
        }
        return (percent, charging)
    }
}
