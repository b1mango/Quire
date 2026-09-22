"""REST 端点逻辑：与 HTTP 管线分离，便于直接单测（项目设计.md §18）。

所有输入在这里做后端复核——前端的任何校验都可被绕过（安全清单 §6.8）。
"""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import __version__
from ..assemble.models import clean_metadata_text
from ..cli_console import data_home, find_chrome, module_available
from ..errors import ConfigError, QuireError
from ..sites.kind_guard import check_placement
from ..store import groups, library
from ..store.models import JsonValue
from . import settings as settings_mod
from .job_state import JobSpec
from .probe import probe_url, validate_task_url

if TYPE_CHECKING:
    from .app import QuireServer


def capabilities(ctx: QuireServer) -> dict[str, JsonValue]:
    """doctor 信息：渲染后端、OCR 引擎与数据目录（项目设计.md §6.9）。"""
    chrome = find_chrome()
    ocr: dict[str, JsonValue] = {"tesseract": shutil.which("tesseract") is not None}
    if module_available("onnxruntime"):
        from ..ocr.models import check_models

        status = check_models(data_home() / "models")
        ocr["onnxruntime"] = True
        ocr["models_ready"] = status.ready
        ocr["model_dir"] = str(status.model_dir)
    else:
        ocr["onnxruntime"] = False
        ocr["models_ready"] = False
    return {
        "version": __version__,
        "chrome": chrome,
        "ocr": ocr,
        "data_dir": str(ctx.data_root),
        "output_dir": str(settings_mod.load(ctx.settings_path, ctx.data_root).output_path),
    }


def get_settings(ctx: QuireServer) -> dict[str, JsonValue]:
    from dataclasses import asdict

    return dict(asdict(settings_mod.load(ctx.settings_path, ctx.data_root)))


def put_settings(ctx: QuireServer, payload: Any) -> dict[str, JsonValue]:
    if not isinstance(payload, dict):
        raise ConfigError("设置格式不正确")
    # 读改写整体持锁：并发 PUT 各自合并后整体覆盖会丢更新
    with ctx.settings_lock:
        merged: dict[str, Any] = {**get_settings(ctx), **payload}
        parsed = settings_mod.parse(merged)
        settings_mod.save(ctx.settings_path, parsed)
        return get_settings(ctx)


def probe(
    ctx: QuireServer, payload: Any, on_stage: Callable[[str], None] | None = None
) -> dict[str, JsonValue]:
    if not isinstance(payload, dict) or not isinstance(payload.get("url"), str):
        raise ConfigError("缺少链接")
    settings = settings_mod.load(ctx.settings_path, ctx.data_root)
    result = asyncio.run(
        probe_url(
            payload["url"],
            data_root=ctx.data_root,
            kind=payload.get("kind"),
            split_by=str(payload.get("split_by", "volume")),
            capture_mode=payload.get("capture_mode", "auto"),
            obey_robots=settings.obey_robots,
            on_stage=on_stage,
        )
    )
    return {
        "kind": result.kind,
        "title": result.title,
        "count": result.count,
        "url": result.url,
        "chrome": find_chrome() is not None,
        "series": result.series,
        "render": result.render,
        "chapters": [{"index": i, "title": title} for i, title in enumerate(result.chapters, 1)],
        "estimate_bytes": result.estimate_bytes,
        "estimates": dict(result.estimates),
        "volumes": [
            {
                "index": v.index,
                "title": v.title,
                "chapters": len(v.chapters),
                "pages": None,
                "bytes": None,
            }
            for v in result.volumes
        ],
    }


def probe_stream(
    ctx: QuireServer, payload: Any, send_frame: Callable[[dict[str, JsonValue]], None]
) -> None:
    """NDJSON 流式识别：阶段帧先行，结果或错误帧收尾（项目设计.md §3.3）。

    识别中途的失败（如目录解析不到章节）以错误帧收尾，此时响应行已是 200；
    流尚未开始时的失败（链接不合法等）照常抛出，由 HTTP 层回对应状态码。
    """
    streamed = False

    def frame(obj: dict[str, JsonValue]) -> None:
        nonlocal streamed
        streamed = True
        send_frame(obj)

    try:
        result = probe(ctx, payload, on_stage=lambda name: frame({"stage": name}))
    except QuireError as exc:
        if not streamed:
            raise
        frame({"error": exc.message, "hint": exc.hint})
        return
    frame({"result": result})


def _volumes(value: Any) -> tuple[int, ...]:
    if not isinstance(value, list) or any(type(v) is not int for v in value):
        raise ConfigError("卷号须为整数列表")
    return tuple(value)


def submit_job(ctx: QuireServer, payload: Any) -> dict[str, JsonValue]:
    if not isinstance(payload, dict):
        raise ConfigError("任务格式不正确")
    kind = payload.get("kind")
    title = payload.get("title") or "book"
    formats = payload.get("formats")
    if not isinstance(kind, str) or not isinstance(title, str) or not isinstance(formats, list):
        raise ConfigError("任务缺少类型、书名或格式")
    target = payload.get("target_mb")
    ranges = payload.get("chapter_ranges") or ""
    if not isinstance(ranges, str):
        raise ConfigError("章节范围表达式须为字符串")
    url = validate_task_url(str(payload.get("url") or ""))
    check_placement(url, kind)
    if kind == "novel" and "pdf" in formats and find_chrome() is None:
        raise ConfigError("转 PDF 需要 Chrome，这台机器上没找到", hint="安装 Chrome 后再试。")
    spec = JobSpec(
        kind=kind,
        url=url,
        title=clean_metadata_text(title) or "book",
        formats=tuple(str(item) for item in formats),
        compress=str(payload.get("compress") or "balanced"),
        target_bytes=None
        if payload.get("compress") == "lossless"
        else (int(target) * 1_000_000 if isinstance(target, int | float) else 50_000_000),
        ocr=str(payload.get("ocr") or "auto"),
        series=payload.get("series", False),
        split_by=str(payload.get("split_by", "none")),
        volumes=_volumes(payload.get("volumes", [])),
        capture_mode=payload.get("capture_mode", "auto"),
        render=payload.get("render", False),
        chapter_first=payload.get("chapter_first", 1),
        chapter_last=payload.get("chapter_last", 0),
        chapter_ranges=ranges,
        follow_prefix=payload.get("follow_prefix", 0),
    )
    job = ctx.manager.submit(spec, settings_mod.load(ctx.settings_path, ctx.data_root))
    return dict(job.snapshot())


def list_jobs(ctx: QuireServer) -> dict[str, JsonValue]:
    return {"jobs": [job.snapshot() for job in ctx.manager.jobs()]}


def _book_json(ctx: QuireServer, book: library.Book) -> dict[str, JsonValue]:
    follow = library.get_follow(ctx.data_root, book.id)
    return {
        "id": book.id,
        "title": book.title,
        "author": book.author,
        "kind": book.kind,
        "source_url": book.source_url,
        "formats": [a.format for a in book.files],
        "bytes": sum(a.bytes for a in book.files),
        "compress": book.compress,
        "created_at": book.created_at,
        "cover": f"/api/books/{book.id}/cover" if book.cover else None,
        "follow": None
        if follow is None
        else {
            "chapters": follow.chapters,
            "update": follow.update,
            "changed": bool(follow.checked_at) and not follow.remote_match,
        },
    }


def list_books(ctx: QuireServer, search: str) -> dict[str, JsonValue]:
    books = library.list_books(ctx.data_root, search[:200])
    belongs = groups.membership(ctx.data_root)
    payload: list[JsonValue] = []
    for book in books:
        item = _book_json(ctx, book)
        item["group"] = belongs.get(book.id)
        payload.append(item)
    return {
        "books": payload,
        "groups": [
            {"id": group.id, "name": group.name, "members": group.members}
            for group in groups.list_groups(ctx.data_root)
        ],
    }


def _group_name(payload: Any) -> str:
    name = payload.get("name") if isinstance(payload, dict) else None
    if not isinstance(name, str):
        raise ConfigError("缺少分组名")
    return name


def create_group(ctx: QuireServer, payload: Any) -> dict[str, JsonValue]:
    group = groups.create_group(ctx.data_root, secrets.token_hex(8), _group_name(payload))
    return {"id": group.id, "name": group.name, "members": 0}


def rename_group(ctx: QuireServer, group_id: str, payload: Any) -> dict[str, JsonValue]:
    groups.rename_group(ctx.data_root, group_id, _group_name(payload))
    return {"renamed": group_id}


def delete_group(ctx: QuireServer, group_id: str) -> dict[str, JsonValue]:
    groups.delete_group(ctx.data_root, group_id)
    return {"deleted": group_id}


def batch_books(ctx: QuireServer, payload: Any) -> dict[str, JsonValue]:
    """批量书籍操作：move（入组/出组）整单校验；delete 逐本执行、汇报每本结果。"""
    if not isinstance(payload, dict) or not isinstance(payload.get("ids"), list):
        raise ConfigError("缺少书籍列表")
    if len(payload["ids"]) > 500:
        raise ConfigError("每次批量操作最多 500 本书，请分批操作")
    ids = [str(item) for item in payload["ids"]]
    if not ids:
        raise ConfigError("没有选中任何书")
    action = payload.get("action")
    if action == "move":
        target = payload.get("group_id")
        moved = groups.assign(ctx.data_root, ids, str(target) if isinstance(target, str) else None)
        return {"moved": moved}
    if action == "delete":
        freed = 0
        failures: list[JsonValue] = []
        for book_id in ids:
            try:
                freed += library.delete_book(ctx.data_root, book_id)
                groups.drop_book(ctx.data_root, book_id)
            except QuireError as exc:
                failures.append({"id": book_id, "error": exc.message})
        return {"deleted": len(ids) - len(failures), "freed": freed, "failures": failures}
    raise ConfigError("不支持的批量操作")


def delete_book(ctx: QuireServer, book_id: str) -> dict[str, JsonValue]:
    freed = library.delete_book(ctx.data_root, book_id)
    groups.drop_book(ctx.data_root, book_id)
    return {"deleted": True, "bytes": freed}


def open_book(ctx: QuireServer, book_id: str) -> dict[str, JsonValue]:
    book = library.get_book(ctx.data_root, book_id)
    target = book.files[0].path  # add_book 保证至少一件成品
    if not target.is_file():
        raise ConfigError("成品文件已不在原位置", hint="可以在书库里删除这条记录。")
    ctx.opener(target)
    return {"opened": str(target)}


def reveal_book(ctx: QuireServer, book_id: str) -> dict[str, JsonValue]:
    """在 Finder 中显示成品：只允许定位当前输出目录内的真实文件。

    路径经 realpath 归一后再判断包含关系，``..`` 与符号链接逃逸一律拒绝。
    """
    book = library.get_book(ctx.data_root, book_id)
    target = book.files[0].path
    if not target.is_file():
        raise ConfigError("成品文件已不在原位置", hint="可以在书库里删除这条记录。")
    output = settings_mod.load(ctx.settings_path, ctx.data_root).output_path
    resolved_output = Path(os.path.realpath(output))
    resolved_target = Path(os.path.realpath(target))
    if resolved_target != resolved_output and resolved_output not in resolved_target.parents:
        raise ConfigError(
            "只能定位输出目录内的成品",
            hint=f"这本书不在当前输出目录 {resolved_output} 下。",
        )
    ctx.revealer(target)
    return {"revealed": str(target)}
