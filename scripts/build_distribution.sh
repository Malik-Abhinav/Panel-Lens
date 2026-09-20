#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
: "${DEVELOPER_DIR:=/Applications/Xcode.app/Contents/Developer}"
export DEVELOPER_DIR
# Run scripts/package_runtime.py first. Signing that runtime uses the same identity.
if [ -n "${PANELLENS_SIGN_IDENTITY:-}" ]; then
  runtime_identity=$(/usr/bin/plutil -extract signingIdentity raw -o - "$root/build/distribution/resources/runtime-manifest.json")
  if [ "$runtime_identity" != "$PANELLENS_SIGN_IDENTITY" ]; then
    echo 'Rebuild the runtime with PANELLENS_SIGN_IDENTITY before signing the app.' >&2
    exit 1
  fi
fi
xcodegen generate
xcodebuild -project PanelLens.xcodeproj -scheme PanelLens -configuration Release \
  -derivedDataPath "$root/build/release" CODE_SIGNING_ALLOWED=NO build
app="$root/build/release/Build/Products/Release/PanelLens.app"
if [ -n "${PANELLENS_SIGN_IDENTITY:-}" ]; then
  /usr/bin/codesign --force --options runtime --timestamp --sign "$PANELLENS_SIGN_IDENTITY" "$app"
  /usr/bin/codesign --verify --deep --strict "$app"
else
  echo 'UNSIGNED BUILD: verify GitHub checksum and macOS first-open behavior before public release.' >&2
  /usr/bin/codesign --force --sign - "$app"
  /usr/bin/codesign --verify --deep --strict "$app"
fi
mkdir -p "$root/build/distribution/delivery"
zip="$root/build/distribution/delivery/PanelLens-0.3.1-macos-arm64.zip"
/usr/bin/ditto -c -k --keepParent "$app" "$zip"
if [ -n "${PANELLENS_NOTARY_PROFILE:-}" ]; then
  test -n "${PANELLENS_SIGN_IDENTITY:-}"
  xcrun notarytool submit "$zip" \
    --keychain-profile "$PANELLENS_NOTARY_PROFILE" --wait
  xcrun stapler staple "$app"
  /usr/bin/ditto -c -k --keepParent "$app" "$zip"
fi
(cd "$root/build/distribution/delivery" && /usr/bin/shasum -a 256 "$(basename "$zip")" > SHA256SUMS)
