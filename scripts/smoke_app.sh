#!/bin/bash
# .app 冒烟（项目设计.md §35 M7）：以无 python3 的干净 PATH 启动应用，
# 驱动一条 mock 站任务流程（probe → 提交 → 完成 → 书库），断言数据落在
# 本地数据根、退出后无残留进程。用法：scripts/smoke_app.sh [--quarantine] [quire.app 路径]
# --quarantine：复制应用并对内嵌文件打下载隔离标记，验证壳进程内自清除
# quarantine 后核心正常拉起（根级标记会触发 Gatekeeper 挂起壳，无法在
# 无辅助功能权限的自动化中复现用户批准态，故只打文件级标记）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
QUARANTINE=0
APP=""
for arg in "$@"; do
    case "$arg" in
        --quarantine) QUARANTINE=1 ;;
        *) APP="$arg" ;;
    esac
done
APP="${APP:-$ROOT/dist/quire.app}"
WORK="$ROOT/output/app-smoke"
APP_LOG="$WORK/app.log"
MOCK_LOG="$WORK/mock.log"
MOCK_PID=""
SHELL_PID=""

cleanup() {
    [ -n "$SHELL_PID" ] && kill "$SHELL_PID" 2>/dev/null || true
    [ -n "$MOCK_PID" ] && kill "$MOCK_PID" 2>/dev/null || true
    return 0
}
trap cleanup EXIT
fail() { echo "冒烟失败：$1" >&2; echo "--- app.log ---"; cat "$APP_LOG" 2>/dev/null; exit 1; }

rm -rf "$WORK"
mkdir -p "$WORK"

QUAR_FILES=()
if [ "$QUARANTINE" = 1 ]; then
    cp -R "$APP" "$WORK/quire-quar.app"
    APP="$WORK/quire-quar.app"
    QUAR_FILES+=("$APP/Contents/Info.plist" "$APP/Contents/MacOS/quire" \
        "$APP/Contents/Resources/python/bin/python3.12")
    while IFS= read -r f; do QUAR_FILES+=("$f"); done < <(
        find "$APP/Contents/Resources/python" \( -name '*.so' -o -name '*.dylib' \) | head -3)
    for f in "${QUAR_FILES[@]}"; do
        xattr -w com.apple.quarantine "0083;65f00000;smoke;" "$f"
    done
fi

# mock 站（测试夹具，用开发 venv 起；被测对象只有 .app）
DEV_PYTHON="${QUIRE_DEV_PYTHON:-$ROOT/.venv/bin/python}"
"$DEV_PYTHON" "$ROOT/scripts/dev_mock_server.py" >"$MOCK_LOG" 2>&1 &
MOCK_PID=$!
for _ in $(seq 1 50); do [ -s "$MOCK_LOG" ] && break; sleep 0.1; done
MOCK_URL="$(head -1 "$MOCK_LOG")"
[ -n "$MOCK_URL" ] || fail "mock 站未启动"

# 干净环境：PATH 只有系统目录（无 python3），数据根隔离到 output/
env -i PATH=/usr/bin:/bin HOME="$HOME" QUIRE_HOME="$WORK/data" \
    "$APP/Contents/MacOS/quire" >"$APP_LOG" 2>&1 &
SHELL_PID=$!

URL=""
for _ in $(seq 1 100); do
    URL="$(grep -o 'http://127\.0\.0\.1:[0-9]*/?token=[^ ]*' "$APP_LOG" 2>/dev/null | head -1 || true)"
    [ -n "$URL" ] && break
    kill -0 "$SHELL_PID" 2>/dev/null || fail "壳进程提前退出"
    sleep 0.3
done
[ -n "$URL" ] || fail "30 秒内未等到服务 URL"

if [ "$QUARANTINE" = 1 ]; then
    for f in "${QUAR_FILES[@]}"; do
        if xattr -p com.apple.quarantine "$f" >/dev/null 2>&1; then
            fail "壳未清除隔离标记：$f"
        fi
    done
fi
BASE="${URL%%/?token=*}"
TOKEN="${URL##*token=}"
AUTH="token=$TOKEN"

curl -sf "$URL" | grep -q "卷帙" || fail "首页未返回 Web UI"

# 一条 mock 站漫画任务流程
curl -sf -X POST "$BASE/api/probe?$AUTH" -H 'Content-Type: application/json' \
    -d "{\"url\":\"$MOCK_URL\"}" >"$WORK/probe.json" || fail "probe 请求失败"
grep -q '"kind": "manga"' "$WORK/probe.json" || fail "probe 未识别漫画：$(cat "$WORK/probe.json")"

JOB="$(curl -sf -X POST "$BASE/api/jobs?$AUTH" -H 'Content-Type: application/json' \
    -d "{\"kind\":\"manga\",\"url\":\"$MOCK_URL\",\"title\":\"smoke\",\"formats\":[\"pdf\"]}")"
JOB_ID="$(echo "$JOB" | sed -n 's/.*"id": "\([^"]*\)".*/\1/p')"
[ -n "$JOB_ID" ] || fail "任务提交失败：$JOB"

STATUS=""
for _ in $(seq 1 100); do
    SNAP="$(curl -sf "$BASE/api/jobs/$JOB_ID?$AUTH" || true)"
    STATUS="$(echo "$SNAP" | sed -n 's/.*"status": "\([^"]*\)".*/\1/p')"
    case "$STATUS" in done | partial | failed | cancelled) break ;; esac
    sleep 0.3
done
[ "$STATUS" = "done" ] || fail "任务状态 $STATUS（期望 done）：$SNAP"

BOOKS="$(curl -sf "$BASE/api/books?$AUTH")"
echo "$BOOKS" | grep -q '"id": "' || fail "书库没有成品：$BOOKS"
[ -n "$(find "$WORK/data" -name '*.pdf' -print -quit)" ] || fail "数据根没有产出 PDF"
[ -f "$WORK/data/library.db" ] || fail "数据根没有书库索引"

# 关闭：SIGTERM 壳进程（等价于退出应用），断言窗口/进程无残留
kill "$SHELL_PID" 2>/dev/null
for _ in $(seq 1 40); do kill -0 "$SHELL_PID" 2>/dev/null || break; sleep 0.25; done
kill -0 "$SHELL_PID" 2>/dev/null && fail "壳进程 10 秒未退出"
sleep 1
if pgrep -f "$APP/Contents/Resources/python/bin/python3.12" >/dev/null; then
    fail "壳退出后内嵌 Python 仍有残留进程"
fi

echo "冒烟通过：Web UI 可用、mock 站任务完成、书库有成品、数据本地保存、退出无残留进程"
