#!/bin/bash
# Isolated native checks; never starts quire or reads/writes the user's clipboard.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHECK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/quire-native-check.XXXXXX")"
trap 'rm -rf "$CHECK_DIR"' EXIT

for arch in arm64 x86_64; do
    xcrun swiftc -warnings-as-errors -O -target "$arch-apple-macos13.0" \
        "$ROOT"/app/Sources/QuireApp/*.swift -o "$CHECK_DIR/quire-$arch"
done
lipo -create "$CHECK_DIR/quire-arm64" "$CHECK_DIR/quire-x86_64" -output "$CHECK_DIR/quire"
lipo -archs "$CHECK_DIR/quire"
xcrun swiftc -warnings-as-errors \
    "$ROOT/app/Sources/QuireApp/LoopbackOrigin.swift" \
    "$ROOT/app/Sources/QuireApp/WindowDragView.swift" \
    "$ROOT/app/Sources/QuireApp/ApplicationMenu.swift" \
    "$ROOT/app/Tests/NativeShellTests.swift" -o "$CHECK_DIR/native-tests"
"$CHECK_DIR/native-tests"
# Validate the embedded isolated-world script without opening a browser.
sed -n '/static let geometryScript = #"""/,/"""#/p' \
    "$ROOT/app/Sources/QuireApp/WindowDragView.swift" | sed '1d;$d' > "$CHECK_DIR/drag.js"
node --check "$CHECK_DIR/drag.js"
printf '%s\n' '{"indentation":{"spaces":4}}' > "$CHECK_DIR/swift-format.json"
xcrun swift-format lint --strict --configuration "$CHECK_DIR/swift-format.json" \
    "$ROOT"/app/Sources/QuireApp/{ApplicationMenu,LoopbackOrigin,NativeBridge,WindowDragView,WebView,main}.swift \
    "$ROOT/app/Tests/NativeShellTests.swift" "$ROOT/scripts/set-macos-icon.swift"
bash -n "$ROOT/scripts/build_app.sh" "$ROOT/scripts/make_dmg.sh" "$0"
