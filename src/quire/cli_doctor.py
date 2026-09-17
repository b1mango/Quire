"""Capability diagnostics available in both micro and core distributions."""

from __future__ import annotations

import argparse
import platform
import shutil
import sys

from . import __version__
from .cli_console import data_home, find_chrome, info, warn
from .cli_console import module_available as _module_available


def cmd_doctor(args: argparse.Namespace) -> int:
    """能力自检：为什么能用、为什么不能用，一眼可见。"""
    info(f"quire {__version__}")
    info(f"Python  {platform.python_version()} ({sys.executable})")
    info(f"系统    {platform.system()} {platform.release()} / {platform.machine()}")
    info("")

    info("可选依赖")
    optional = [("PIL", "core 图片解码与压缩"), ("httpx", "core 连接池"), ("pypdf", "PDF 测试校验")]
    for module, purpose in optional:
        mark = "✓" if _module_available(module) else "·"
        info(f"  {mark} {module:<14} {purpose}")

    info("")
    info("渲染后端")
    chrome = find_chrome()
    if chrome:
        info(f"  ✓ {chrome}")
    else:
        warn("没找到 Chrome / Edge / Brave / Chromium")
        warn("  JS 渲染与「小说转 PDF」都需要它（项目设计.md 决策 N）")
    info("")
    info("OCR 引擎")
    if shutil.which("tesseract"):
        info("  ✓ 系统 tesseract（优先引擎，增量 0）")
    else:
        info("  · 无系统 tesseract")
    if _module_available("onnxruntime"):
        info("  ✓ onnxruntime（内置引擎，quire-local[ocr]）")
        from .ocr.models import check_models

        status = check_models(data_home() / "models")
        if status.ready:
            info(f"  ✓ PP-OCRv4 模型已就绪：{status.model_dir}")
        else:
            missing = "、".join((*status.missing, *status.corrupt))
            info(f"  · 模型未就绪（{missing}），首次 OCR 时按需下载到 {status.model_dir}")
    else:
        info("  · 无 onnxruntime（内置引擎需 quire-local[ocr]）")

    info("")
    info("能力档位")
    info("  micro：静态网页 / 本地 JPEG、PNG → 无损 PDF，零第三方运行时依赖")
    if all(
        _module_available(name)
        for name in ("httpx", "PIL", "pypdf", "websockets", "quire.core_manga")
    ):
        info("  core：漫画下载恢复、转码切页、PDF/CBZ/ZIP；--render使用系统Chrome加载动态页面")
    if all(_module_available(name) for name in ("httpx", "websockets", "quire.core_novel")):
        info("  core：小说目录、正文抽取、EPUB/TXT/PDF，章级断点恢复（quire novel）")
        info("        图片正文本地 OCR：tesseract 或内置 PP-OCRv4（--ocr，模型按需下载）")
    if _module_available("quire.server") and all(
        _module_available(name)
        for name in ("httpx", "PIL", "pypdf", "websockets", "quire.core_manga")
    ):
        info("  Web 界面：quire ui（回环地址 + 随机令牌）")

    info("")
    info("数据目录")
    info(f"  后续默认数据根：{data_home()}")
    info("  当前 micro 的输出和临时缓存位于所选输出路径旁")

    return 0
