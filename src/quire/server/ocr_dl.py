"""设置页「下载 OCR 模块」：NDJSON 流式模型下载（§6.7 模型下载契约）。"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from ..cli_console import module_available
from ..errors import ConfigError, QuireError
from ..ocr.models import check_models, ensure_models
from ..store.models import JsonValue

if TYPE_CHECKING:
    from .app import QuireServer


def download_ocr_models(
    ctx: QuireServer, send_frame: Callable[[dict[str, JsonValue]], None]
) -> None:
    """逐文件发进度帧，结果或错误帧收尾；流开始前的失败回普通错误码。"""
    if not module_available("onnxruntime"):
        raise ConfigError(
            "这台机器没有内置 OCR 引擎",
            hint="应用包不含 onnxruntime；安装系统 tesseract 后也能识别图片正文。",
        )
    model_dir = ctx.data_root / "models"
    status = check_models(model_dir)
    missing = (*status.missing, *status.corrupt)
    send_frame({"total": len(missing)})
    if not missing:
        send_frame({"result": {"ready": True, "downloaded": []}})
        return
    downloaded: list[JsonValue] = []

    def on_file(name: str) -> None:
        downloaded.append(name)
        send_frame({"done": len(downloaded), "total": len(missing), "file": name})

    try:
        ensure_models(model_dir, on_file=on_file)
    except QuireError as exc:
        send_frame({"error": exc.message, "hint": exc.hint})
        return
    send_frame({"result": {"ready": True, "downloaded": downloaded}})
