"""追更：书库书籍的更新检查与增量续抓（项目设计.md §6.12 延伸）。

书记账在 ``library.db`` 的 follows 表（来源 URL 在 books 表）：已抓连续章节数 +
末章标题指纹。「检查更新」重新 probe 来源目录，边界标题对不上时只报「目录变动」，
不按序号续抓——站点改号/插章时续抓会拼错章节。

续抓任务只抓新章节（``chapter_first = 已抓数 + 1``），导出时把旧任务账本里
已落定的章节缓存拼在前面（``cached_prefix``），产物仍是整书单文件；
导出阶段不联网。旧章节的提取选项以当时任务为准，不在此重新校验。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ..assemble.models import NovelChapter
from ..core_chapter_cache import decode_chapter
from ..errors import ConfigError, LedgerError, QuireError
from ..models import NovelResult
from ..store import library
from ..store.cache import read_cached
from ..store.ledger import Ledger
from ..store.models import JsonValue
from . import settings as settings_mod
from .job_state import JobSpec
from .probe import probe_url

if TYPE_CHECKING:
    from .app import QuireServer

NOVEL_FORMATS = ("epub", "txt", "pdf")


def register_follow(data_root: Path, book_id: str, spec: JobSpec, result: NovelResult) -> None:
    """书籍入库时登记追更锚点；区间残卷（非从第一章起）与单章不登记。"""
    if spec.kind != "novel" or spec.capture_mode == "single":
        return
    if spec.chapter_first != 1 and spec.follow_prefix != spec.chapter_first - 1:
        return
    written = {chapter.index for chapter in result.chapters if chapter.missing_reason is None}
    contiguous = 0
    while contiguous + 1 in written:
        contiguous += 1
    if not contiguous:
        return
    titles = {chapter.index: chapter.title for chapter in result.chapters}
    library.upsert_follow(
        data_root,
        book_id,
        capture_mode=spec.capture_mode,
        chapters=contiguous,
        last_title=titles[contiguous],
    )


def _followable(ctx: QuireServer, book_id: str) -> tuple[library.Book, library.Follow]:
    book = library.get_book(ctx.data_root, book_id)
    follow = library.get_follow(ctx.data_root, book_id)
    if follow is None:
        raise ConfigError("这本书不支持追更", hint="只有目录模式采集的整本小说可以追更。")
    return book, follow


def _compare_remote(
    ctx: QuireServer,
    book: library.Book,
    follow: library.Follow,
    on_stage: Callable[[str], None] | None = None,
) -> dict[str, JsonValue]:
    result = asyncio.run(
        probe_url(
            book.source_url,
            data_root=ctx.data_root,
            kind="novel",
            capture_mode=follow.capture_mode,
            obey_robots=settings_mod.load(ctx.settings_path, ctx.data_root).obey_robots,
            on_stage=on_stage,
        )
    )
    titles = result.chapters
    match = result.count >= follow.chapters and titles[follow.chapters - 1] == follow.last_title
    library.record_check(ctx.data_root, book.id, remote_count=result.count, match=match)
    return {
        "book_id": book.id,
        "title": book.title,
        "chapters": follow.chapters,
        "remote_count": result.count,
        "update": result.count - follow.chapters if match else 0,
        "changed": not match,
    }


def check_update(ctx: QuireServer, book_id: str) -> dict[str, JsonValue]:
    """重新 probe 来源目录，对比章节数与末章标题指纹，结果记入 follows 表。"""
    book, follow = _followable(ctx, book_id)
    return _compare_remote(ctx, book, follow)


def check_update_stream(
    ctx: QuireServer, book_id: str, send_frame: Callable[[dict[str, JsonValue]], None]
) -> None:
    """NDJSON 流式单本检查：probe 阶段帧先行，结果或错误帧收尾（§3.3 同识别语义）。"""
    book, follow = _followable(ctx, book_id)
    streamed = False

    def frame(obj: dict[str, JsonValue]) -> None:
        nonlocal streamed
        streamed = True
        send_frame(obj)

    try:
        payload = _compare_remote(ctx, book, follow, on_stage=lambda name: frame({"stage": name}))
    except QuireError as exc:
        if not streamed:
            raise
        frame({"error": exc.message, "hint": exc.hint})
        return
    frame({"result": payload})


def check_all_stream(ctx: QuireServer, send_frame: Callable[[dict[str, JsonValue]], None]) -> None:
    """书库页「全部检查」：首帧给总数，逐本发进度帧，单本失败不拖垮其余。"""
    books = [
        book
        for book in library.list_books(ctx.data_root)
        if library.get_follow(ctx.data_root, book.id) is not None
    ]
    send_frame({"total": len(books)})
    results: list[JsonValue] = []
    for index, book in enumerate(books, 1):
        try:
            owned, follow = _followable(ctx, book.id)
            payload: dict[str, JsonValue] = _compare_remote(ctx, owned, follow)
        except QuireError as exc:
            payload = {"book_id": book.id, "title": book.title, "error": exc.message}
        send_frame(
            {
                "done": index,
                "total": len(books),
                "title": book.title,
                "update": payload.get("update", 0),
                "error": payload.get("error"),
            }
        )
        results.append(payload)
    send_frame({"result": {"results": results}})


def follow_submit(ctx: QuireServer, book_id: str) -> dict[str, JsonValue]:
    """一键追更：确认有新章节后，以「已抓数 + 1」起抓，旧章节从账本缓存拼回。"""
    book = library.get_book(ctx.data_root, book_id)
    follow = library.get_follow(ctx.data_root, book_id)
    if follow is None:
        raise ConfigError("这本书不支持追更", hint="只有目录模式采集的整本小说可以追更。")
    check = check_update(ctx, book_id)
    if check["changed"]:
        raise ConfigError("目录在已抓末章处对不上，不能按序号续抓", hint="请整本重新抓取。")
    if not check["update"]:
        raise ConfigError("没有新章节")
    formats = tuple(f for f in (a.format for a in book.files) if f in NOVEL_FORMATS)
    if not formats:
        formats = ("txt",)
    if "pdf" in formats:
        from ..cli_console import find_chrome

        if find_chrome() is None:
            formats = tuple(f for f in formats if f != "pdf") or ("txt",)
    spec = JobSpec(
        kind="novel",
        url=book.source_url,
        title=book.title,
        formats=formats,
        compress=book.compress or "balanced",
        capture_mode=follow.capture_mode,
        chapter_first=follow.chapters + 1,
        chapter_last=0,
        follow_prefix=follow.chapters,
    )
    job = ctx.manager.submit(spec, settings_mod.load(ctx.settings_path, ctx.data_root))
    return dict(job.snapshot())


def resolve_prefix(spec: JobSpec, workdir: Path) -> tuple[NovelChapter, ...]:
    """任务启动时解析待拼接的旧章节；缓存不完整时明确报错，不出残缺的书。"""
    prefix = cached_prefix(workdir, spec.url, spec.follow_prefix)
    if len(prefix) != spec.follow_prefix:
        raise ConfigError("本地缓存不完整，无法只续抓新章节", hint="请用完整范围重新抓取这本书。")
    return prefix


def cached_prefix(workdir: Path, url: str, count: int) -> tuple[NovelChapter, ...]:
    """同来源地址各任务里已落定章节缓存的连续前缀（章号 1..count）。

    逐章校验缓存哈希；任一章缺失或损坏即截断，由调用方决定是否拒绝。
    """
    if count < 1:
        return ()
    found: dict[int, NovelChapter] = {}
    with Ledger(workdir) as ledger:
        rows = ledger.done_chapters(url)
        for task_id, record in rows:
            chapter = record.spec.chapter
            if chapter > count:
                continue
            if not (record.local_path and record.sha256 and record.size):
                continue
            try:
                cached = decode_chapter(
                    read_cached(
                        ledger.root,
                        task_id,
                        record.local_path,
                        sha256=record.sha256,
                        size=record.size,
                    )
                )
            except LedgerError:
                continue
            # 同章号以较新的任务为准（done_chapters 按创建时间升序，后写覆盖）
            found[chapter] = NovelChapter(
                chapter,
                cached.title,
                cached.paragraphs,
                source_url=record.spec.url,
                pages=cached.pages,
                truncated=cached.truncated,
                source=cached.source,
                review=cached.review,
            )
    prefix: list[NovelChapter] = []
    for chapter in range(1, count + 1):
        if chapter not in found:
            break
        prefix.append(found[chapter])
    return tuple(prefix)
