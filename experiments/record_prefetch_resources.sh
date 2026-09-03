#!/bin/zsh

set -eu

duration_seconds="${1:-300}"
interval_seconds="${2:-2}"
timestamp="$(date +%Y%m%d-%H%M%S)"
output_directory="experiments/results/prefetch"
output_path="${output_directory}/resources-${timestamp}.csv"

mkdir -p "${output_directory}"
print 'timestamp,sidecar_cpu_percent,sidecar_rss_mb,ollama_cpu_percent,ollama_rss_mb,panellens_cpu_percent,panellens_rss_mb' > "${output_path}"

sample_processes() {
    local pattern="$1"
    local pids
    pids="$(pgrep -f "${pattern}" 2>/dev/null | paste -sd, - || true)"
    if [[ -z "${pids}" ]]; then
        print '0,0'
        return
    fi
    ps -p "${pids}" -o %cpu=,rss= | awk '{ cpu += $1; rss += $2 } END { printf "%.1f,%.1f\n", cpu, rss / 1024 }'
}

started_at="$(date +%s)"
while (( $(date +%s) - started_at < duration_seconds )); do
    sidecar="$(sample_processes 'sidecar/http_server.py')"
    ollama="$(sample_processes '[o]llama serve|[l]lama-server')"
    panellens="$(sample_processes 'PanelLens.app/Contents/MacOS/PanelLens')"
    print "$(date -Iseconds),${sidecar},${ollama},${panellens}" >> "${output_path}"
    sleep "${interval_seconds}"
done

print "Recorded resource usage to ${output_path}"
