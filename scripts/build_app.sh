#!/bin/bash
# 构建 quire.app（项目设计.md §3.2、§3.4）：
# Swift 薄壳 + 内嵌精简 Python 3.12（python-build-standalone，universal2）+
# 锁定 core 依赖 + quire 包，ad-hoc 签名。幂等可复跑；产物在 dist/quire.app。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PBS_TAG="20260901"
PYVER="3.12.14"
PBS_BASE="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}"
SHA_ARM64="81a359f1cfadd4da11766534c5913791cea55f26e1bb902cacd2a531bb1e4b2b"
SHA_X86="65b195c9cedc1fef6767f044f9822069adbd1bd9204d424ece4628776fdc04bb"

CACHE="$ROOT/build/pbs"
WORK="$ROOT/build/app"
DIST="$ROOT/dist"
APP="$DIST/quire.app"

VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/src/quire/__init__.py")"
[ -n "$VERSION" ] || { echo "无法读取版本号" >&2; exit 1; }

fetch() { # <url> <sha256> <dest>
    if [ -f "$3" ] && [ "$(shasum -a 256 "$3" | cut -d' ' -f1)" = "$2" ]; then
        return
    fi
    curl -fSL --retry 3 -o "$3" "$1"
    echo "$2  $3" | shasum -a 256 -c - >/dev/null
}

# 逐文件合并两个架构的安装树：Mach-O 用 lipo 合并，其余文件必须字节一致。
merge_trees() { # <arm64 dir> <x86_64 dir> <out dir>
    rm -rf "$3"
    cp -R "$1" "$3"
    (cd "$2" && find . -type f ! -name '.DS_Store') | while read -r rel; do
        if file -b "$2/$rel" | grep -q "Mach-O"; then
            lipo -create "$3/$rel" "$2/$rel" -output "$3/$rel.merged"
            mv "$3/$rel.merged" "$3/$rel"
        elif ! cmp -s "$3/$rel" "$2/$rel"; then
            case "$rel" in
            *.dist-info/RECORD | *.dist-info/WHEEL) ;; # 记录 .so 哈希与 wheel 标签，两架构本就不同
            *) echo "警告：非 Mach-O 文件两架构不一致：$rel" >&2 ;;
            esac
        fi
    done
}

trim_python() { # <python install dir>
    rm -rf "$1"/lib/libpython3.12.dylib \
        "$1"/lib/python3.12/{idlelib,tkinter,turtledemo,ensurepip,venv,pydoc_data,test} \
        "$1"/lib/python3.12/lib-dynload/_tkinter.*.so \
        "$1"/lib/{libtcl*,libtk*,tcl*,tk*,itcl*,thread*,tdbc*} \
        "$1"/include "$1"/share "$1"/lib/python3.12/config-3.12-darwin
    find "$1/bin" -type l -delete
    (cd "$1/bin" && rm -f pip* idle* 2to3* pydoc* python3*-config)
    rm -rf "$1/lib/python3.12/site-packages"
    mkdir -p "$1/lib/python3.12/site-packages"
}

trim_core() { # <core site dir>：删无调用者的二进制（avif/tk 桥接），保留 freetype/webp/icc 链路
    rm -rf "$1"/bin \
        "$1"/PIL/_avif.* "$1"/PIL/.dylibs/libavif* \
        "$1"/PIL/_imagingtk.*
}

# 标准库预编译成 python312.zip（zipimport 只认 .pyc；unchecked-hash 免源校验），
# 删除 .py/.pyc 实体文件；site-packages 保持目录（.pth 必须是真实文件）。
zip_stdlib() { # <python install dir>
    "$1/bin/python3.12" -m compileall -q -b --invalidation-mode=unchecked-hash \
        "$1/lib/python3.12"
    (cd "$1/lib/python3.12" && find . -name "*.pyc" | sed 's|^\./||' \
        | zip -q -@ "$1/lib/python312.zip")
    find "$1/lib/python3.12" -mindepth 1 -maxdepth 1 \
        ! -name lib-dynload ! -name site-packages -exec rm -rf {} +
}

mkdir -p "$CACHE" "$DIST"
fetch "$PBS_BASE/cpython-${PYVER}+${PBS_TAG}-aarch64-apple-darwin-install_only_stripped.tar.gz" \
    "$SHA_ARM64" "$CACHE/py-arm64.tar.gz"
fetch "$PBS_BASE/cpython-${PYVER}+${PBS_TAG}-x86_64-apple-darwin-install_only_stripped.tar.gz" \
    "$SHA_X86" "$CACHE/py-x86_64.tar.gz"

rm -rf "$WORK" "$APP"
mkdir -p "$WORK"
for arch in arm64 x86_64; do
    tar xzf "$CACHE/py-$arch.tar.gz" -C "$WORK"
    mv "$WORK/python" "$WORK/py-$arch"
    trim_python "$WORK/py-$arch"
done

RES="$APP/Contents/Resources"
mkdir -p "$APP/Contents/MacOS" "$RES"
merge_trees "$WORK/py-arm64" "$WORK/py-x86_64" "$RES/python"
zip_stdlib "$RES/python"

# 锁定的 core 依赖：arm64/x86_64 各装一份再 lipo 合并（Pillow 无 universal2 wheel）。
REQS="$WORK/requirements-core.txt"
uv export --project "$ROOT" --locked --no-dev --extra core --no-hashes --no-emit-project -o "$REQS"
uv pip install --python "$RES/python/bin/python3.12" --target "$WORK/core-arm64" -r "$REQS" --quiet
uv pip install --python "$RES/python/bin/python3.12" --python-platform x86_64-apple-darwin \
    --target "$WORK/core-x86_64" -r "$REQS" --quiet
trim_core "$WORK/core-arm64"
trim_core "$WORK/core-x86_64"
merge_trees "$WORK/core-arm64" "$WORK/core-x86_64" "$RES/core"
cp -R "$ROOT/src/quire" "$RES/core/quire"
echo "../../../../core" >"$RES/python/lib/python3.12/site-packages/quire-core.pth"
"$RES/python/bin/python3.12" -m compileall -q "$RES/core"

# Swift 薄壳：两个架构分别编译后 lipo。
for arch in arm64 x86_64; do
    swiftc -O -target "$arch-apple-macos13.0" \
        "$ROOT"/app/Sources/QuireApp/*.swift -o "$WORK/quire-$arch"
done
lipo -create "$WORK/quire-arm64" "$WORK/quire-x86_64" -output "$APP/Contents/MacOS/quire"

cp "$ROOT/app/Resources/Info.plist" "$APP/Contents/Info.plist"
plutil -replace CFBundleShortVersionString -string "$VERSION" "$APP/Contents/Info.plist"
plutil -replace CFBundleVersion -string "$VERSION" "$APP/Contents/Info.plist"

ICONSET="$WORK/AppIcon.iconset"
mkdir -p "$ICONSET"
qlmanage -t -s 1024 -o "$WORK" "$ROOT/docs/assets/quire.svg" >/dev/null 2>&1
for size in 16 32 128 256 512; do
    double=$((size * 2))
    sips -z "$size" "$size" "$WORK/quire.svg.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    sips -z "$double" "$double" "$WORK/quire.svg.png" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$RES/AppIcon.icns"

# ad-hoc 签名（§3.4）：先内后外，否则 Apple Silicon 内核拒绝执行。
find "$RES/python" "$RES/core" \( -name "*.so" -o -name "*.dylib" \) -print0 \
    | xargs -0 codesign --force --sign - >/dev/null 2>&1
codesign --force --sign - "$RES/python/bin/python3.12"
codesign --force --sign - "$APP/Contents/MacOS/quire"
codesign --force --sign - "$APP"
codesign --verify --deep --strict "$APP"

BYTES=$(du -sk "$APP" | cut -f1)
echo "构建完成：$APP"
echo "版本 ${VERSION}，.app 体积 $((BYTES / 1024)).$((BYTES % 1024 * 10 / 1024)) MB（$((BYTES * 1024)) bytes）"
