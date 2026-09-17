"""OCR 引擎层：tesseract TSV 解析、子进程故障、引擎选择与编排（fake 引擎）。"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from quire.errors import UnsupportedError
from quire.ocr import capture
from quire.ocr.base import OcrLine, OcrPage
from quire.ocr.capture import OcrRunner
from quire.ocr.tesseract import OcrEngineError, TesseractEngine, _parse_tsv
from quire.parse.minidom import parse

TSV = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
    "5\t1\t1\t1\t1\t1\t10\t10\t40\t20\t96\t他抬头\n"
    "5\t1\t1\t1\t1\t2\t55\t10\t40\t20\t90\t看见\n"
    "5\t1\t1\t1\t2\t1\t10\t40\t80\t20\t88\t远山\n"
    "5\t1\t1\t2\t1\t1\t10\t70\t50\t20\t95\tChapter\n"
    "5\t1\t1\t2\t1\t2\t65\t70\t30\t20\t91\t3\n"
    "4\t1\t1\t1\t1\t1\t0\t0\t0\t0\t-1\t\n"  # 非词级行忽略
    "5\t1\t1\t1\t3\t1\t10\t90\t10\t10\t50\t\n"  # 空文本忽略
)


def test_parse_tsv_groups_words_into_lines() -> None:
    lines = _parse_tsv(TSV)
    assert [(line.text, line.confidence) for line in lines] == [
        ("他抬头看见", 0.93),
        ("远山", 0.88),
        ("Chapter 3", 0.93),
    ]
    first = lines[0]
    assert (first.x0, first.y0, first.x1, first.y1) == (10, 10, 95, 30)
    assert [line.y0 for line in lines] == sorted(line.y0 for line in lines)


def test_recognize_without_executable_errors() -> None:
    engine = TesseractEngine(executable=None)
    assert engine.available() is False
    with pytest.raises(OcrEngineError, match="没有 tesseract"):
        engine.recognize(b"png")


class _Proc:
    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_recognize_pipes_image_and_parses_tsv(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> _Proc:
        seen["cmd"] = cmd
        seen["input"] = kwargs["input"]
        return _Proc(stdout=TSV.encode())

    monkeypatch.setattr(subprocess, "run", fake_run)
    engine = TesseractEngine(executable="/usr/bin/tesseract")
    page = engine.recognize(b"image-bytes")
    assert seen["input"] == b"image-bytes"
    assert page.engine == "tesseract" and len(page.lines) == 3
    cmd = seen["cmd"]
    assert isinstance(cmd, list) and "chi_sim+eng" in cmd and "tsv" in cmd


def test_recognize_failures_become_engine_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _Proc(returncode=1, stderr=b"bad image\n")
    )
    engine = TesseractEngine(executable="tesseract")
    with pytest.raises(OcrEngineError, match="bad image"):
        engine.recognize(b"x")

    def timeout(*a: object, **k: object) -> _Proc:
        raise subprocess.TimeoutExpired(cmd="tesseract", timeout=1)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(OcrEngineError, match="超时"):
        engine.recognize(b"x")

    def missing(*a: object, **k: object) -> _Proc:
        raise OSError("no such file")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(OcrEngineError, match="无法运行"):
        engine.recognize(b"x")


class FakeEngine:
    """预置识别结果的假引擎：记录输入，返回固定行。"""

    name = "fake"

    def __init__(self, lines: list[OcrLine] | None = None) -> None:
        self.lines = lines or [OcrLine("识别出的正文。", 0.92, 0, 0, 200, 20)]
        self.seen: list[bytes] = []

    def recognize(self, image: bytes) -> OcrPage:
        self.seen.append(image)
        return OcrPage(tuple(self.lines), self.name)


class StubFetch:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.requests: list[str] = []

    async def get(self, url: str, *, referer: str | None = None):  # noqa: ANN202
        self.requests.append(url)
        return type("R", (), {"content": self.payloads[url]})()


def runner(tmp_path: Path, engine: object, **kwargs: object) -> OcrRunner:
    instance = OcrRunner(model_dir=tmp_path, **kwargs)  # type: ignore[arg-type]
    instance._engine = engine  # type: ignore[arg-type]
    return instance


def test_recognize_page_without_images_skips_engine(tmp_path: Path) -> None:
    fake = FakeEngine()
    doc = parse("<html><body><div id='content'><p>纯文本。</p></div></body></html>")
    result = asyncio.run(
        runner(tmp_path, fake).recognize_page(doc, "http://t/1.html", StubFetch({}))
    )
    assert result.paragraphs == () and result.images == 0
    assert fake.seen == []


def test_recognize_page_fetches_and_merges(tmp_path: Path) -> None:
    fake = FakeEngine(
        [
            OcrLine("第一行没有完", 0.92, 0, 0, 400, 20),
            OcrLine("接着说完。", 0.88, 0, 25, 400, 45),
        ]
    )
    fetch = StubFetch({"http://t/img/1.jpg": b"jpeg-bytes"})
    doc = parse(
        "<html><body><div id='content'><img src='img/1.jpg'></div></body></html>",
        base_url="http://t/1.html",
    )
    result = asyncio.run(runner(tmp_path, fake).recognize_page(doc, "http://t/1.html", fetch))
    assert fetch.requests == ["http://t/img/1.jpg"]
    assert fake.seen == [b"jpeg-bytes"]
    assert result.paragraphs == ("第一行没有完接着说完。",)
    assert result.engine == "fake" and result.images == 1
    assert len(result.lines) == 2


def test_recognize_page_skips_oversized_images(tmp_path: Path) -> None:
    fake = FakeEngine()
    fetch = StubFetch({"http://t/big.jpg": b"x" * 100})
    doc = parse(
        "<html><body><div id='content'><img src='big.jpg'></div></body></html>",
        base_url="http://t/1.html",
    )
    result = asyncio.run(
        runner(tmp_path, fake, max_bytes=10).recognize_page(doc, "http://t/1.html", fetch)
    )
    assert result.paragraphs == () and result.images == 0
    assert "超过大小上限" in result.warnings[0]
    assert fake.seen == []


def test_engine_preference_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capture, "find_tesseract", lambda: None)
    with pytest.raises(UnsupportedError, match="tesseract 引擎未安装") as caught:
        OcrRunner(engine="tesseract", model_dir=tmp_path)._create_engine()
    assert caught.value.exit_code == 6

    monkeypatch.setattr(capture, "onnx_available", lambda: False)
    with pytest.raises(UnsupportedError, match="没有可用的 OCR 引擎"):
        OcrRunner(engine="auto", model_dir=tmp_path)._create_engine()
    with pytest.raises(UnsupportedError, match="缺少 onnxruntime"):
        OcrRunner(engine="onnx", model_dir=tmp_path)._create_engine()


def test_engine_auto_prefers_tesseract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capture, "find_tesseract", lambda: "/usr/bin/tesseract")
    engine = OcrRunner(engine="auto", model_dir=tmp_path)._create_engine()
    assert engine.name == "tesseract"


def test_create_onnx_ensures_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capture, "find_tesseract", lambda: None)
    monkeypatch.setattr(capture, "onnx_available", lambda: True)
    called: dict[str, object] = {}

    class FakeOnnx(FakeEngine):
        name = "onnx"

        def __init__(self, model_dir: Path) -> None:
            super().__init__()
            called["model_dir"] = model_dir

    def fake_ensure(model_dir: Path, *, offline: bool = False, **kwargs: object) -> None:
        called["offline"] = offline

    monkeypatch.setattr(capture, "OnnxEngine", FakeOnnx)
    monkeypatch.setattr(capture, "ensure_models", fake_ensure)
    engine = OcrRunner(engine="auto", model_dir=tmp_path, offline=True)._create_engine()
    assert engine.name == "onnx"
    assert called == {"model_dir": tmp_path, "offline": True}
