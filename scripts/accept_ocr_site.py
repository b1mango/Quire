"""M5 验收：mock 图片正文站 + 真实 PP-OCRv4 模型的端到端可读性验证（项目设计.md §6.7）。

用法：``.venv/bin/python scripts/accept_ocr_site.py``（需 ``quire-local[ocr]`` 与网络，
首次运行按需下载模型到 ``output/m5-models``；本脚本不进 CI）。

流程：本地回环站点提供目录页与两章「正文区只有一张扫描图」的章节页，
图片是 Pillow 渲染的固定中文文本；用真实 onnx 引擎跑 ``run_core_novel``
完整管线（触发判定 → 取图 → 识别 → 后处理 → TXT 成品），核对成品可读。
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from accept_ocr import MODEL_DIR, render

from quire.core_novel import run_core_novel
from quire.models import NovelOptions

CHAPTERS: dict[int, tuple[str, tuple[str, ...]]] = {
    1: (
        "第一章 雨夜",
        (
            "雨下得很大，他把那本旧书揣进怀里，快步穿过空无一人的长街。",
            "路灯在雨幕里晕开一团团昏黄的光，像谁遗落的灯笼。",
        ),
    ),
    2: (
        "第二章 旧信",
        (
            "信封上的字迹已经模糊，邮戳却还清晰：一九八七年十月三日。",
            "她拆开信封，里面的信纸薄得几乎透明，写满了密密麻麻的小字。",
        ),
    ),
}

REPORT = Path("output/m5-acceptance/site-report.json")


def _image(number: int) -> bytes:
    buffer = BytesIO()
    render(CHAPTERS[number][1], "clean").save(buffer, format="PNG")
    return buffer.getvalue()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib 命名
        path = self.path.split("?", 1)[0]
        if path in ("/", "/book/"):
            items = "".join(
                f'<li><a href="/book/{n}.html">{title}</a></li>'
                for n, (title, _) in CHAPTERS.items()
            )
            self._send(f"<html><body><h1>扫描之书</h1><ul>{items}</ul></body></html>".encode())
            return
        for number, (title, _) in CHAPTERS.items():
            if path == f"/book/{number}.html":
                page = (
                    f"<html><head><title>{title}</title></head><body><h1>{title}</h1>"
                    f'<div id="content"><img src="/img/{number}.png"></div></body></html>'
                )
                self._send(page.encode())
                return
            if path == f"/img/{number}.png":
                self._send(_image(number), "image/png")
                return
        self.send_error(404)

    def _send(self, body: bytes, content_type: str = "text/html") -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@contextmanager
def serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _best_window(flat: str, line: str) -> str:
    """在成品全文里滑窗找与该行编辑距离最小的片段（行可能有个别误字）。"""
    from accept_ocr import levenshtein

    width = len(line)
    if len(flat) <= width:
        return flat
    return min(
        (flat[index : index + width] for index in range(len(flat) - width + 1)),
        key=lambda window: levenshtein(line, window),
    )


def main() -> int:
    out_dir = Path("output/m5-acceptance")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "scan-book.txt"
    for path in out_dir.glob("scan-book*"):
        path.unlink()
    started = time.monotonic()
    with serve() as url:
        result = asyncio.run(
            run_core_novel(
                url + "/",
                out,
                options=NovelOptions(rate=100.0, retries=0, model_dir=MODEL_DIR),
                workdir=out_dir / "work",
                formats=("txt",),
            )
        )
    text = out.read_text("utf-8")
    expected = [line for _, lines in CHAPTERS.values() for line in lines]
    from accept_ocr import levenshtein, normalize_for_compare

    flat = normalize_for_compare(text)
    accuracies = [
        1
        - levenshtein(normalize_for_compare(line), _best_window(flat, normalize_for_compare(line)))
        / max(len(normalize_for_compare(line)), 1)
        for line in expected
    ]
    passed = (
        result.chapters_written == len(CHAPTERS)
        and result.ocr_chapters == len(CHAPTERS)
        and all(accuracy >= 0.9 for accuracy in accuracies)
    )
    REPORT.write_text(
        json.dumps(
            {
                "passed": passed,
                "chapters_written": result.chapters_written,
                "ocr_chapters": result.ocr_chapters,
                "line_accuracies": [round(accuracy, 4) for accuracy in accuracies],
                "review": str(result.review) if result.review else None,
                "elapsed_s": round(time.monotonic() - started, 3),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    sys.stdout.write(
        f"章节 {result.chapters_written}/{len(CHAPTERS)}，OCR 章 {result.ocr_chapters}，成品 {out}"
    )
    for line, accuracy in zip(expected, accuracies, strict=True):
        sys.stdout.write(f"  {accuracy:.0%} {line}\n")
    sys.stdout.write(f"报告 {REPORT}\n")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
