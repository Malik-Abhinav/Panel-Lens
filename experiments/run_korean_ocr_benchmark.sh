#!/bin/sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export PADDLE_PDX_CACHE_HOME="$project_root/.cache/paddlex"
export PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True

exec "$project_root/sidecar/.venv/bin/python" \
  "$project_root/experiments/ocr_benchmark.py" "$@"
