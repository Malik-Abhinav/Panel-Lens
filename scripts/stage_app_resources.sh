#!/bin/sh
set -eu
# Xcode release build phase. No downloads or pip execution during app startup.
source_root="${SRCROOT:?}"
destination="${TARGET_BUILD_DIR:?}/${UNLOCALIZED_RESOURCES_FOLDER_PATH:?}"
assets="$source_root/build/distribution/resources"
if [ ! -f "$assets/runtime-manifest.json" ] || [ ! -f "$assets/runtime.tar.gz" ]; then
  echo 'error: Build the release runtime first: python3 scripts/package_runtime.py' >&2
  exit 1
fi
rm -rf "$destination/sidecar" "$destination/extension"
mkdir -p "$destination/sidecar"
cp "$assets/runtime-manifest.json" "$assets/runtime.tar.gz" "$destination/"
cp "$source_root"/sidecar/*.py "$destination/sidecar/"
cp "$source_root/distribution/runtime-requirements.lock" "$destination/"
cp "$source_root/distribution/runtime-wheel-sources.json" "$destination/"

mkdir -p "$destination/extension"
for file in manifest.json popup.html popup.js service-worker.js content-script.js prefetch-core.js overlay.css; do
  cp "$source_root/extension/$file" "$destination/extension/$file"
done
