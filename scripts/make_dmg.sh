#!/bin/bash
# 用系统自带 hdiutil 把 dist/quire.app 打成 dist/quire-<version>.dmg（§3.4），
# 零额外工具链；幂等可复跑。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$ROOT/dist"
APP="$DIST/quire.app"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/src/quire/__init__.py")"
DMG="$DIST/quire-${VERSION}.dmg"

[ -d "$APP" ] || { echo "先运行 scripts/build_app.sh" >&2; exit 1; }
ICON="$APP/Contents/Resources/AppIcon.png"
[ -f "$ICON" ] || { echo "缺少透明图标，请重新运行 scripts/build_app.sh" >&2; exit 1; }

STAGE="$ROOT/build/dmg"
rm -rf "$STAGE" "$DMG"
mkdir -p "$STAGE"
# Explicitly retain FinderInfo, resource forks (Icon\r) and extended attributes.
ditto --rsrc --extattr "$APP" "$STAGE/quire.app"
codesign --verify --deep "$STAGE/quire.app"
ln -s /Applications "$STAGE/Applications"

hdiutil create -volname "卷帙 Quire" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
hdiutil verify "$DMG" >/dev/null
xcrun swift "$ROOT/scripts/set-macos-icon.swift" "$ICON" "$DMG"
BYTES=$(stat -f%z "$DMG")
echo "构建完成：$DMG"
echo ".dmg 体积 $((BYTES / 1048576)).$((BYTES % 1048576 * 10 / 1048576)) MB（${BYTES} bytes）"
