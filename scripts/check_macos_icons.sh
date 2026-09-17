#!/bin/bash
# Exercises the real packaging script on a disposable signed fixture, never the user's app.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHECK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/quire-icon-check.XXXXXX")"
MOUNT=""
cleanup() {
    if [ -n "$MOUNT" ]; then
        hdiutil detach "$MOUNT" >/dev/null || return
    fi
    rm -rf "$CHECK_DIR"
}
trap cleanup EXIT

FIXTURE="$CHECK_DIR/project"
APP="$FIXTURE/dist/quire.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" "$FIXTURE/scripts" "$FIXTURE/src/quire"
cp "$ROOT/app/Resources/Info.plist" "$APP/Contents/Info.plist"
cp "$ROOT/docs/assets/quire-icon.png" "$APP/Contents/Resources/AppIcon.png"
cp "$ROOT/scripts/make_dmg.sh" "$ROOT/scripts/set-macos-icon.swift" "$FIXTURE/scripts/"
printf '%s\n' '__version__ = "fixture"' > "$FIXTURE/src/quire/__init__.py"
printf '%s\n' 'int main(void) { return 0; }' > "$CHECK_DIR/stub.c"
xcrun clang "$CHECK_DIR/stub.c" -o "$APP/Contents/MacOS/quire"
codesign --force --sign - "$APP"
codesign --verify --deep --strict "$APP"
xcrun swift "$ROOT/scripts/set-macos-icon.swift" "$APP/Contents/Resources/AppIcon.png" "$APP"
codesign --verify --deep "$APP"
bash "$FIXTURE/scripts/make_dmg.sh"
DMG="$FIXTURE/dist/quire-fixture.dmg"
mkdir "$CHECK_DIR/mount"
hdiutil attach "$DMG" -readonly -nobrowse -noautoopen -mountpoint "$CHECK_DIR/mount" >/dev/null
MOUNT="$CHECK_DIR/mount"
codesign --verify --deep "$MOUNT/quire.app"
ditto --rsrc --extattr "$MOUNT/quire.app" "$CHECK_DIR/installed.app"
codesign --verify --deep "$CHECK_DIR/installed.app"
python3 - "$APP" "$FIXTURE/build/dmg/quire.app" "$MOUNT/quire.app" "$CHECK_DIR/installed.app" "$DMG" <<'PY'
import os
import sys
import subprocess
from pathlib import Path

def attribute(path, name):
    return bytes.fromhex(subprocess.check_output(['xattr', '-px', name, str(path)], text=True))

apps = [Path(path) for path in sys.argv[1:-1]]
reference = attribute(apps[0] / 'Icon\r', 'com.apple.ResourceFork')
assert reference, 'Custom icon resource fork is empty'
for app in apps:
    finder_info = attribute(app, 'com.apple.FinderInfo')
    assert int.from_bytes(finder_info[8:10], 'big') & 0x0400, 'Missing kHasCustomIcon'
    assert attribute(app / 'Icon\r', 'com.apple.ResourceFork') == reference
    assert (app / 'Contents/Resources/AppIcon.png').read_bytes() == (apps[0] / 'Contents/Resources/AppIcon.png').read_bytes()
assert (apps[2].parent / 'Applications').is_symlink()
assert os.readlink(apps[2].parent / 'Applications') == '/Applications'
assert attribute(sys.argv[-1], 'com.apple.ResourceFork'), 'DMG file custom icon missing'
print('PASS: custom-icon flag/resource fork and Dock PNG preserved through staging, DMG and install copy')
PY
# Normal verification must still reject changes to sealed resources after custom-icon setup.
printf '%s' 'tampered' >> "$CHECK_DIR/installed.app/Contents/Resources/AppIcon.png"
if codesign --verify --deep "$CHECK_DIR/installed.app" > "$CHECK_DIR/tamper.log" 2>&1; then
    echo "ERROR: signature verification accepted a tampered resource" >&2
    exit 1
fi
python3 - "$CHECK_DIR/tamper.log" <<'PY'
import sys
from pathlib import Path
result = Path(sys.argv[1]).read_text()
assert 'a sealed resource is missing or invalid' in result, result
print('PASS: ordinary signature verification rejects a tampered sealed resource')
PY
