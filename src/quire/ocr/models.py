"""OCR 模型按需下载与强制校验（项目设计.md §6.7 模型下载契约）。

契约逐条对应：

* **HTTPS 固定 URL**——地址与 SHA-256 一起钉死在 ``MODEL_SET``，
  来源是 RapidAI 发布的 PP-OCRv4 mobile ONNX 模型（含其公布的哈希）；
* **强制 SHA-256 校验**——下载后、以及每次加载前都校验；
* **失败即中止不留半模型**——先写临时文件，校验通过才原子替换，
  任何一个文件失败都把本次下载的临时文件全部清掉；
* **写入 ``models/CHECKSUMS``**——安装成功后落盘 ``sha256  文件名`` 清单；
* **``--offline`` 缺模型直接报错（退出码 6）**——打印手动放置路径。

镜像按顺序尝试；全部失败给出可操作的下一步（手动放置路径）。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..errors import UnsupportedError

#: 识别模型还差一个字典文件：CTC 输出索引到字符的映射。
MODEL_FILES: tuple[tuple[str, str, str], ...] = (
    (
        "ch_PP-OCRv4_det_mobile.onnx",
        "d2a7720d45a54257208b1e13e36a8479894cb74155a5efe29462512d42f49da9",
        "onnx/PP-OCRv4/det/ch_PP-OCRv4_det_mobile.onnx",
    ),
    (
        "ch_PP-OCRv4_rec_mobile.onnx",
        "48fc40f24f6d2a207a2b1091d3437eb3cc3eb6b676dc3ef9c37384005483683b",
        "onnx/PP-OCRv4/rec/ch_PP-OCRv4_rec_mobile.onnx",
    ),
    (
        "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
        "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c",
        "onnx/PP-OCRv4/cls/ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    ),
    (
        "ppocr_keys_v1.txt",
        "28b2362ad4ab2dc38769aa72feb535e3a9ddb3fd2a7585a05920e6393b1dc7f7",
        "paddle/PP-OCRv4/rec/ch_PP-OCRv4_rec_mobile/ppocr_keys_v1.txt",
    ),
)

#: 镜像按顺序尝试；URL 模板 ``{path}`` 替换为上面的相对路径。
MIRRORS: tuple[str, ...] = (
    "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/{path}",
)

#: 单个模型文件的大小上限（rec 模型约 10.9 MB，留足余量）。
MAX_MODEL_BYTES = 64 * 1024 * 1024

#: 下载读取函数：给定 URL 返回字节。生产用 httpx，测试注入假实现。
Downloader = Callable[[str], bytes]


@dataclass(frozen=True, slots=True)
class ModelStatus:
    """模型目录的体检结果。"""

    model_dir: Path
    ready: bool
    missing: tuple[str, ...]
    corrupt: tuple[str, ...]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_models(model_dir: Path) -> ModelStatus:
    """检查模型目录：每个文件是否存在、SHA-256 是否匹配。"""
    missing: list[str] = []
    corrupt: list[str] = []
    for name, expected, _ in MODEL_FILES:
        path = model_dir / name
        if not path.is_file():
            missing.append(name)
        elif sha256_file(path) != expected:
            corrupt.append(name)
    return ModelStatus(model_dir, not missing and not corrupt, tuple(missing), tuple(corrupt))


def manual_hint(model_dir: Path, names: Sequence[str]) -> str:
    """给用户的兜底指引：手动下载这些 URL 放到模型目录。"""
    wanted = {item for item in MODEL_FILES if item[0] in names}
    urls = [mirror.format(path=path) for _, _, path in wanted for mirror in MIRRORS[:1]]
    listing = "\n".join(f"  {url}" for url in urls)
    return f"手动下载以下文件放到 {model_dir}（文件名保持不变）：\n{listing}"


def _http_download(url: str) -> bytes:
    import httpx

    with httpx.stream("GET", url, follow_redirects=True, timeout=120) as response:
        response.raise_for_status()
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes(1 << 16):
            size += len(chunk)
            if size > MAX_MODEL_BYTES:
                raise UnsupportedError(f"模型文件超过大小上限：{url}")
            chunks.append(chunk)
    return b"".join(chunks)


def _install(model_dir: Path, name: str, expected: str, data: bytes) -> None:
    """先写临时文件、校验哈希，匹配才原子替换；不匹配什么都不留。"""
    if hashlib.sha256(data).hexdigest() != expected:
        raise UnsupportedError(f"模型校验失败（SHA-256 不匹配）：{name}")
    fd, tmp = tempfile.mkstemp(dir=model_dir, prefix=f".{name}.", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, model_dir / name)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_checksums(model_dir: Path) -> Path:
    """安装成功后写入 ``CHECKSUMS`` 清单（``sha256  文件名``，逐行）。"""
    content = "".join(f"{expected}  {name}\n" for name, expected, _ in MODEL_FILES)
    path = model_dir / "CHECKSUMS"
    path.write_text(content, encoding="utf-8")
    return path


def ensure_models(
    model_dir: Path,
    *,
    offline: bool = False,
    downloader: Downloader | None = None,
) -> ModelStatus:
    """保证模型目录可用；缺文件或损坏时按需下载并强制校验。

    ``offline`` 下缺失直接报错（退出码 6）并打印手动放置路径；
    下载任一步失败即中止，目录里不留半成品。
    """
    status = check_models(model_dir)
    if status.ready:
        return status
    wanted = (*status.missing, *status.corrupt)
    if offline:
        raise UnsupportedError(
            "离线模式缺少 OCR 模型：" + "、".join(wanted),
            hint=manual_hint(model_dir, wanted),
        )
    fetch = downloader or _http_download
    model_dir.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    for name, expected, path in (item for item in MODEL_FILES if item[0] in wanted):
        data: bytes | None = None
        for mirror in MIRRORS:
            url = mirror.format(path=path)
            try:
                data = fetch(url)
                break
            except UnsupportedError:
                raise
            except Exception as exc:  # httpx 各网络异常统一转为领域错误
                errors.append(f"{url}（{type(exc).__name__}）")
        if data is None:
            detail = "；".join(errors[-len(MIRRORS) :]) if errors else "无可用镜像"
            raise UnsupportedError(
                f"OCR 模型下载失败：{name}（{detail}）",
                hint=manual_hint(model_dir, wanted),
            )
        _install(model_dir, name, expected, data)
    write_checksums(model_dir)
    return check_models(model_dir)
