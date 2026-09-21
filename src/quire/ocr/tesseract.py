"""系统 tesseract 引擎：探测到就用，增量 0（项目设计.md §6.7）。

通过子进程调用，不引入任何 Python 绑定：图片从 stdin 喂入，
TSV 从 stdout 取回，按 ``块-段-行`` 聚合成行并取词置信度均值。
识别语言固定 ``chi_sim+eng``——本项目 OCR 的目标就是中文扫描正文。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from contextlib import closing
from io import BytesIO

from ..errors import FetchError, QuireError
from ..image.codec import decoded_image
from .base import OcrLine, OcrPage

#: 单页识别上限（秒）：扫描页正常 1-3 秒，超时视为引擎故障。
RECOGNIZE_TIMEOUT = 120

#: 识别语言与排版模式（6 = 假设单块统一文本）。
LANGUAGES = "chi_sim+eng"
PAGE_MODE = "6"

_ASCII_WORD = re.compile(r"^[A-Za-z0-9]+$")


class OcrEngineError(QuireError):
    """引擎存在但识别本身失败（超时、崩溃、输出不可解析）。"""

    exit_code = 3


def find_tesseract() -> str | None:
    return shutil.which("tesseract")


class TesseractEngine:
    """``OcrEngine`` 协议的 tesseract 实现。"""

    name = "tesseract"

    def __init__(self, executable: str | None = None, *, timeout: float = RECOGNIZE_TIMEOUT):
        self.executable = executable or find_tesseract()
        self.timeout = timeout

    def available(self) -> bool:
        return self.executable is not None

    def recognize(self, image: bytes) -> OcrPage:
        if self.executable is None:
            raise OcrEngineError("系统没有 tesseract", hint="安装 tesseract 或改用 onnx 引擎")
        try:
            # Pass only the validated first frame; no resizing/EXIF rotation so TSV
            # coordinates retain the input pixel geometry.
            with decoded_image(image) as (frame, _), BytesIO() as buffer:
                mode = "RGBA" if "A" in frame.getbands() or "transparency" in frame.info else "RGB"
                with closing(frame.convert(mode)) as png:
                    png.info.clear()
                    png.save(buffer, format="PNG")
                image = buffer.getvalue()
        except FetchError as exc:
            raise OcrEngineError(f"OCR 输入不是可解码或尺寸合规的图片：{exc}") from exc
        try:
            proc = subprocess.run(
                [
                    self.executable,
                    "stdin",
                    "stdout",
                    "-l",
                    LANGUAGES,
                    "--psm",
                    PAGE_MODE,
                    "tsv",
                ],
                input=image,
                capture_output=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise OcrEngineError(f"tesseract 识别超时（{self.timeout:.0f} 秒）") from exc
        except OSError as exc:
            raise OcrEngineError(f"无法运行 tesseract：{exc}") from exc
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            raise OcrEngineError(f"tesseract 识别失败：{detail[-1] if detail else '未知错误'}")
        return OcrPage(tuple(_parse_tsv(proc.stdout.decode("utf-8", "replace"))), self.name)


def _parse_tsv(tsv: str) -> list[OcrLine]:
    """把 tesseract TSV 聚合成行：同一块/段/行的词按横坐标排序拼接。"""
    groups: dict[tuple[str, str, str], list[tuple[int, int, int, int, float, str]]] = {}
    for row in tsv.splitlines()[1:]:
        fields = row.split("\t")
        if len(fields) != 12 or fields[0] != "5":
            continue
        text = fields[11].strip()
        if not text:
            continue
        try:
            left, top = int(fields[6]), int(fields[7])
            right, bottom = left + int(fields[8]), top + int(fields[9])
            conf = float(fields[10]) / 100.0
        except ValueError:
            continue
        key = (fields[2], fields[3], fields[4])
        groups.setdefault(key, []).append((left, top, right, bottom, conf, text))
    lines: list[OcrLine] = []
    for key in sorted(groups):
        words = sorted(groups[key])
        text = _join_words([word[5] for word in words])
        if not text:
            continue
        confidence = sum(word[4] for word in words) / len(words)
        lines.append(
            OcrLine(
                text,
                round(confidence, 4),
                min(word[0] for word in words),
                min(word[1] for word in words),
                max(word[2] for word in words),
                max(word[3] for word in words),
            )
        )
    lines.sort(key=lambda line: (line.y0, line.x0))
    return lines


def _join_words(words: list[str]) -> str:
    """中文词之间不加空格；两侧都是 ASCII 单词时保留一个空格。"""
    out = ""
    for word in words:
        if out and _ASCII_WORD.match(out[-1:]) and _ASCII_WORD.match(word):
            out += " "
        out += word
    return out.strip()
