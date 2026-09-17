"""小说 OCR 端到端：mock 图片正文站 + fake 引擎验证管线（文本优先、缓存、复核清单）。

真实引擎（tesseract / PP-OCRv4 模型）的识别准确率不在单元测试里验证；
这里锁死的是管线行为：什么时候触发 OCR、结果怎么进成品、失败怎么恢复。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from quire.core_novel import run_core_novel
from quire.errors import NoTextError, UnsupportedError
from quire.models import NovelOptions
from quire.ocr import capture
from quire.ocr.base import OcrLine, OcrPage
from quire.ocr.tesseract import OcrEngineError
from tests.mock_site.novel_server import novel_site
from tests.mock_site.scan_server import image_bytes, scan_site

OPTIONS = NovelOptions(rate=100.0, retries=0, timeout=10.0, concurrency=3)


def run(site, out: Path, workdir: Path, **kwargs):  # noqa: ANN001, ANN202
    params = {"options": kwargs.pop("options", OPTIONS), "workdir": workdir}
    params.update(kwargs)
    return asyncio.run(run_core_novel(site.url + "/", out, **params))


class FakeEngine:
    """按图片字节识别章节号，返回预置行；``fail_on`` 模拟引擎故障。"""

    name = "fake"
    calls = 0

    def __init__(self, lines_by_chapter: dict[int, list[OcrLine]], fail_on: set[int] | None = None):
        self.lines_by_chapter = lines_by_chapter
        self.fail_on = fail_on or set()

    def recognize(self, image: bytes) -> OcrPage:
        type(self).calls += 1
        for number in self.lines_by_chapter:
            if image == image_bytes(number):
                if number in self.fail_on:
                    raise OcrEngineError("引擎故障")
                return OcrPage(tuple(self.lines_by_chapter[number]), self.name)
        raise OcrEngineError("未知图片")


def prose(chapter: int, confidence: float = 0.92) -> list[OcrLine]:
    # 首行缩进 2 字符（x0=80），续行顶格（x0=0），贴近真实扫描书排版。
    return [
        OcrLine(f"第{chapter}章第一段说完了。", confidence, 80, 0, 400, 20),
        OcrLine(f"第{chapter}章第二段接着写，", confidence, 80, 30, 400, 50),
        OcrLine(f"第{chapter}章到这里结束。", confidence, 0, 60, 400, 80),
    ]


def install_fake_engine(monkeypatch: pytest.MonkeyPatch, engine: FakeEngine) -> None:
    FakeEngine.calls = 0
    monkeypatch.setattr(capture.OcrRunner, "_create_engine", lambda self: engine)


def test_default_model_dir_resolves_under_data_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """API 用户不传 model_dir 时，模型目录取数据根下的 models/。"""
    import quire.cli_console as console

    seen: dict[str, Path] = {}
    monkeypatch.setattr(console, "data_home", lambda: tmp_path / "data")
    engine = FakeEngine({n: prose(n) for n in (1, 2, 3)})

    def fake_create(self):  # noqa: ANN001, ANN202
        seen["model_dir"] = self.model_dir
        return engine

    monkeypatch.setattr(capture.OcrRunner, "_create_engine", fake_create)
    with scan_site() as site:
        run(site, tmp_path / "book.txt", tmp_path / "work", formats=("txt",))
    assert seen["model_dir"] == tmp_path / "data" / "models"


def test_scan_site_exports_ocr_text(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_fake_engine(monkeypatch, FakeEngine({n: prose(n) for n in (1, 2, 3)}))
    with scan_site() as site:
        result = run(site, tmp_path / "book.txt", tmp_path / "work", formats=("txt",))
    assert result.chapters_written == 3 and result.ocr_chapters == 3
    text = (tmp_path / "book.txt").read_text("utf-8")
    assert "第2章第一段说完了。" in text
    assert "第2章第二段接着写，第2章到这里结束。" in text  # 满宽续行合并成段
    report = json.loads((tmp_path / "book.report.json").read_text("utf-8"))
    assert report["ocr_chapters"] == 3


def test_text_first_never_touches_engine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """文本优先：文本能抽出来的站点连引擎都不创建（模型也不会下载）。"""
    created = []
    monkeypatch.setattr(
        capture.OcrRunner,
        "_create_engine",
        lambda self: created.append(1) or FakeEngine({}),
    )
    with novel_site() as site:
        result = run(site, tmp_path / "book.txt", tmp_path / "work", formats=("txt",))
    assert result.chapters_written == 4
    assert result.ocr_chapters == 0
    assert created == []


def test_ocr_always_skips_text_extraction(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_fake_engine(monkeypatch, FakeEngine({n: prose(n) for n in (1, 2, 3)}))
    with scan_site() as site:
        result = run(
            site,
            tmp_path / "book.txt",
            tmp_path / "work",
            formats=("txt",),
            options=NovelOptions(rate=100.0, retries=0, ocr_mode="always"),
        )
    assert result.ocr_chapters == 3
    assert "第3章第二段接着写，第3章到这里结束。" in (tmp_path / "book.txt").read_text("utf-8")


def test_ocr_never_does_not_recognize(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_fake_engine(monkeypatch, FakeEngine({n: prose(n) for n in (1, 2, 3)}))
    with scan_site() as site:
        with pytest.raises(NoTextError):
            run(
                site,
                tmp_path / "book.txt",
                tmp_path / "work",
                formats=("txt",),
                options=NovelOptions(rate=100.0, retries=0, ocr_mode="never"),
            )
    assert FakeEngine.calls == 0


def test_offline_missing_model_exits_6_with_manual_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(capture, "find_tesseract", lambda: None)
    monkeypatch.setattr(capture, "onnx_available", lambda: True)
    model_dir = tmp_path / "models"
    with scan_site() as site:
        with pytest.raises(UnsupportedError) as caught:
            run(
                site,
                tmp_path / "book.txt",
                tmp_path / "work",
                formats=("txt",),
                options=NovelOptions(rate=100.0, retries=0, offline=True, model_dir=model_dir),
            )
    assert caught.value.exit_code == 6
    assert caught.value.hint is not None
    assert str(model_dir) in caught.value.hint and "https://" in caught.value.hint


def test_low_confidence_lines_land_in_review_txt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lines = {n: prose(n) for n in (1, 2, 3)}
    lines[2] = [*prose(2), OcrLine("模糊难辨的一行。", 0.41, 0, 90, 300, 110)]
    install_fake_engine(monkeypatch, FakeEngine(lines))
    with scan_site() as site:
        result = run(site, tmp_path / "book.txt", tmp_path / "work", formats=("txt",))
    review = tmp_path / "book.review.txt"
    assert result.review == review and review.exists()
    content = review.read_text("utf-8")
    assert "第2章 [0.410] 模糊难辨的一行。" in content
    assert any("低置信" in warning for warning in result.warnings)
    # 模糊行仍保留在正文里（复核清单不改写正文）
    assert "模糊难辨的一行。" in (tmp_path / "book.txt").read_text("utf-8")


def test_engine_failure_marks_chapter_and_resume_retries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lines = {n: prose(n) for n in (1, 2, 3)}
    engine = FakeEngine(lines, fail_on={2})
    install_fake_engine(monkeypatch, engine)
    with scan_site() as site:
        first = run(site, tmp_path / "book.txt", tmp_path / "work", formats=("txt",))
        assert first.chapters_written == 2 and first.chapters_failed == 1
        assert any("OCR 失败" in warning for warning in first.warnings)

        engine.fail_on = set()
        FakeEngine.calls = 0
        second = run(site, tmp_path / "book-2.txt", tmp_path / "work", formats=("txt",))
    assert second.chapters_written == 3
    assert FakeEngine.calls == 1  # 只有失败的第二章重新识别，缓存章零识别
    assert "第2章第一段说完了。" in (tmp_path / "book-2.txt").read_text("utf-8")


def test_repeated_header_lines_dropped_across_chapters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lines = {n: [OcrLine("影印书库", 0.95, 0, 0, 100, 10), *prose(n)] for n in (1, 2, 3)}
    install_fake_engine(monkeypatch, FakeEngine(lines))
    with scan_site() as site:
        run(site, tmp_path / "book.txt", tmp_path / "work", formats=("txt",))
    text = (tmp_path / "book.txt").read_text("utf-8")
    assert "影印书库" not in text  # 三章都出现的短行是页眉，整书删除
    assert "第1章第一段说完了。" in text
