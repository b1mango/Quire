"""Serial volume delivery using the existing verified manga capture/export pipeline."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from .capture_plan import MangaPlan
from .core_manga import run_core_manga
from .errors import ConfigError, FetchError, PausedError
from .fetch.browser import RenderOptions, render_page
from .fetch.session import AsyncFetcher
from .image.options import CompressionOptions
from .models import MangaOptions, MangaResult, ProgressSink
from .parse.images import Candidate
from .parse.minidom import parse
from .parse.series import Volume, plan_volumes, split_spec
from .sites.rules import SiteRule
from .store.models import ResourceSpec


@dataclass(frozen=True, slots=True)
class SeriesResult:
    title: str
    volumes: tuple[MangaResult, ...]
    warnings: tuple[str, ...] = ()

    @property
    def partial(self) -> bool:
        return any(volume.partial for volume in self.volumes)


async def _pages(
    client: AsyncFetcher, volume: Volume, opts: MangaOptions, render: RenderOptions | None
) -> MangaPlan:
    candidates: list[Candidate] = []
    specs: list[ResourceSpec] = []
    result = MangaResult(Path(), title=volume.title)
    for chapter, link in enumerate(volume.chapters, 1):
        from .core_discovery import discover_manga

        found, part = await discover_manga(client, link.url, opts, render)
        for index, candidate in enumerate(found, 1):
            candidates.append(candidate)
            specs.append(ResourceSpec(chapter, index, candidate.url, candidate.referer))
        result = replace(
            result,
            warnings=result.warnings + part.warnings,
            rejections=result.rejections + part.rejections,
        )
    return MangaPlan(
        tuple(candidates), tuple(specs), result, tuple(link.title for link in volume.chapters)
    )


async def run_series(
    url: str,
    out: Path | str,
    *,
    split_by: str = "none",
    first: int = 1,
    last: int = 0,
    selected: tuple[int, ...] = (),
    fallback_chapters: int = 20,
    rule: SiteRule | None = None,
    options: MangaOptions | None = None,
    compression: CompressionOptions | None = None,
    formats: tuple[str, ...] = ("pdf",),
    workdir: Path | str | None = None,
    fetcher: AsyncFetcher | None = None,
    render: RenderOptions | None = None,
    progress: ProgressSink | None = None,
    on_volume: Callable[[MangaResult], None] | None = None,
    on_task: Callable[[str], None] | None = None,
    stop: Callable[[], bool] | None = None,
) -> SeriesResult:
    mode, _ = split_spec(split_by)
    opts = options or MangaOptions()
    if rule:
        if rule.kind != "manga":
            raise ConfigError("series 当前用于漫画系列；小说使用 novel")
        opts = rule.manga(opts)
    output = Path(out).absolute()
    output.mkdir(parents=True, exist_ok=True)
    root = Path(workdir) if workdir else output / ".quire-core"
    client = fetcher or AsyncFetcher(
        timeout=opts.timeout,
        retries=opts.retries,
        concurrency=opts.concurrency,
        rate=opts.rate,
        max_bytes=opts.max_bytes,
    )
    results: list[MangaResult] = []
    async with client:
        page = await client.get(url)
        if render:
            page, _ = await render_page(page, client, render, content="text")
        doc = parse(page.text, base_url=page.url)
        volumes, warnings = plan_volumes(
            doc,
            page.url,
            split_by=split_by,
            chapter_selector=rule.chapter_links if rule else None,
            volume_selector=rule.volume_selector if rule else None,
            order=rule.chapter_order if rule else "auto",
            first=first,
            last=last,
            fallback_chapters=fallback_chapters,
        )
        if selected and (
            len(set(selected)) != len(selected)
            or any(type(i) is not int or not 1 <= i <= len(volumes) for i in selected)
        ):
            raise ConfigError("所选卷号不在目录范围内")
        width = len(str(len(volumes)))
        for volume in volumes:
            if selected and volume.index not in selected:
                continue
            if stop is not None and stop():
                raise PausedError("任务已暂停，已下载的部分保留在缓存里")
            plan = await _pages(client, volume, replace(opts, first=1, last=0), render)
            plan = replace(
                plan,
                result=replace(
                    plan.result,
                    title=f"{doc.title or '系列'} · {volume.title}",
                    warnings=plan.result.warnings + warnings,
                ),
            )
            if mode == "size":
                from .series_size import export_by_size

                sized = await export_by_size(
                    url,
                    output,
                    root,
                    plan,
                    opts,
                    compression or CompressionOptions(),
                    formats,
                    split_spec(split_by)[1],
                    client,
                    progress,
                    on_volume,
                    on_task,
                    stop=stop,
                )
                results.extend(sized)
                break
            result = await run_core_manga(
                url,
                output / f"第{volume.index:0{width}d}卷.{formats[0]}",
                options=opts,
                compression=compression,
                formats=formats,
                workdir=root,
                resume=True,
                fetcher=client,
                shared_fetcher=True,
                plan=plan,
                progress=progress,
                on_task=on_task,
                stop=stop,
            )
            results.append(result)
            if on_volume:
                on_volume(result)
            if result.partial:
                # A delivered partial volume is visible; later volumes still run.
                warnings += (f"第 {volume.index} 卷有缺页，请查看该卷报告",)
    if not results:
        raise FetchError("没有生成任何卷")
    return SeriesResult(doc.title or "系列", tuple(results), warnings)
