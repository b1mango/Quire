"""M8: rule -> series -> delivered volumes -> offline rebuild, plus failure boundaries."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from pypdf import PdfReader

from quire.cli import main
from quire.core_reassemble import run_reassemble
from quire.core_series import run_series
from quire.errors import ConfigError, LedgerError
from quire.fetch.session import AsyncFetcher
from quire.image.options import CompressionOptions
from quire.models import MangaOptions, NovelOptions
from quire.parse.minidom import parse
from quire.parse.series import plan_volumes, split_spec
from quire.sites.diagnostics import diagnose
from quire.sites.rules import load_rule, new_rule, resolve_rule, rule_path
from quire.store.ledger import Ledger
from tests.mock_site.server import page_image
from tests.test_async_fetch import Stream

URL = "https://series.test/book"
CATALOGUE = """<title>山海集</title>
<section class="volume"><h2>第一卷</h2><div class="chapter-list">
<a href="/chapter/1">第1话</a><a href="/chapter/2">第2话</a></div></section>
<section class="volume"><h2>第二卷</h2><div class="chapter-list">
<a href="/chapter/3">第3话</a><a href="/chapter/4">第4话</a></div></section>"""


def rule(root):
    path = new_rule(root, "series.test")
    path.write_text(path.read_text().replace('# volumes = ".volume"', 'volumes = ".volume"'))
    return load_rule(root, "series.test")


def response(status, *, request, text=None, content=b"", headers=None):
    return httpx.Response(
        status,
        stream=Stream([text.encode() if text is not None else content]),
        request=request,
        headers=headers,
    )


def client(calls=None, missing=False):
    def respond(request):
        if calls is not None:
            calls.append(request.url.path)
        path = request.url.path
        if path == "/robots.txt":
            return response(200, text="User-agent: *\nAllow: /", request=request)
        if path == "/book":
            return response(200, text=CATALOGUE, request=request)
        if path.startswith("/chapter/"):
            n = int(path.rsplit("/", 1)[-1])
            return response(
                200,
                text=f'<title>第{n}话</title><main class="reader"><img src="/pages/{n}.jpg"></main>',
                request=request,
            )
        if path.startswith("/pages/"):
            n = int(path.rsplit("/", 1)[-1].split(".")[0])
            return response(
                404 if missing and n == 2 else 200,
                content=page_image(n),
                headers={"Content-Type": "image/jpeg"},
                request=request,
            )
        return response(404, request=request)

    return AsyncFetcher(rate=10000, retries=0, transport=httpx.MockTransport(respond))


def test_series_rules_offline_reassemble_and_resume(tmp_path):
    site = rule(tmp_path)
    calls, delivered = [], []
    result = asyncio.run(
        run_series(
            URL,
            tmp_path / "books",
            split_by="volume",
            rule=site,
            options=MangaOptions(rate=10000, keep_images=True),
            fetcher=client(calls),
            on_volume=delivered.append,
            formats=("pdf", "cbz"),
        )
    )
    assert len(result.volumes) == 2 and not result.partial
    assert delivered == list(result.volumes)
    for index, volume in enumerate(result.volumes, 1):
        reader = PdfReader(volume.output)
        assert len(reader.pages) == 2
        assert "第" in str(reader.outline) and f"第{index}卷.pdf" == volume.output.name
    before = list(calls)
    first = result.volumes[0]
    root = tmp_path / "books/.quire-core"
    rebuilt = asyncio.run(
        run_reassemble(
            first.task_id,
            tmp_path / "rebuilt.pdf",
            workdir=root,
            formats=("pdf", "zip"),
            compression=CompressionOptions("small"),
        )
    )
    assert calls == before and rebuilt.resources_reused == 2
    assert len(PdfReader(rebuilt.output).pages) == 2 and rebuilt.images_dir.is_dir()
    again = asyncio.run(
        run_reassemble(
            first.task_id,
            tmp_path / "rebuilt.pdf",
            workdir=root,
            formats=("pdf", "zip"),
            compression=CompressionOptions("small"),
        )
    )
    assert again.artifacts_reused
    resumed_calls = []
    resumed = asyncio.run(
        run_series(
            URL,
            tmp_path / "books",
            split_by="volume",
            rule=site,
            options=MangaOptions(rate=10000, keep_images=True),
            fetcher=client(resumed_calls),
            formats=("pdf", "cbz"),
        )
    )
    assert all(v.artifacts_reused for v in resumed.volumes)
    assert not any(p.startswith("/pages/") for p in resumed_calls)
    next(first.images_dir.glob("*.jpg")).write_bytes(b"changed")
    with pytest.raises(ConfigError, match="原图"):
        asyncio.run(run_reassemble(first.task_id, tmp_path / "unsafe.pdf", workdir=root))
    assert not (tmp_path / "unsafe.pdf").exists()


def test_series_partial_selection_and_default_cleanup(tmp_path):
    site = rule(tmp_path)
    result = asyncio.run(
        run_series(
            URL, tmp_path / "all", split_by="chapters 2", rule=site, fetcher=client(missing=True)
        )
    )
    assert result.partial and len(result.volumes) == 2 and result.warnings
    assert len(PdfReader(result.volumes[0].output).pages) == 2
    second = result.volumes[1]
    with pytest.raises(ConfigError, match="原图"):
        asyncio.run(
            run_reassemble(
                second.task_id, tmp_path / "missing.pdf", workdir=tmp_path / "all/.quire-core"
            )
        )
    selected = asyncio.run(
        run_series(
            URL, tmp_path / "chosen", split_by="volume", rule=site, selected=(2,), fetcher=client()
        )
    )
    assert len(selected.volumes) == 1 and selected.volumes[0].output.name == "第2卷.pdf"
    with pytest.raises(ConfigError, match="卷号"):
        asyncio.run(
            run_series(
                URL, tmp_path / "bad", split_by="volume", selected=(3,), rule=site, fetcher=client()
            )
        )
    with pytest.raises(ConfigError, match="小说"):
        asyncio.run(run_series(URL, tmp_path / "novel", rule=replace(site, kind="novel")))


def test_actual_size_split_and_unsplittable_chapter(tmp_path):
    site = rule(tmp_path)
    one = asyncio.run(
        run_series(
            URL,
            tmp_path / "sample",
            split_by="chapters 1",
            selected=(1,),
            rule=site,
            fetcher=client(),
            compression=CompressionOptions("archive", None),
        )
    )
    limit = one.volumes[0].bytes_out + 500
    result = asyncio.run(
        run_series(
            URL,
            tmp_path / "sized",
            split_by=f"size {limit}",
            rule=site,
            fetcher=client(),
            compression=CompressionOptions("archive", None),
        )
    )
    assert len(result.volumes) == 4
    assert all(v.bytes_out <= limit and len(PdfReader(v.output).pages) == 1 for v in result.volumes)
    with pytest.raises(ConfigError, match="单章"):
        asyncio.run(
            run_series(URL, tmp_path / "tiny", split_by="size 10", rule=site, fetcher=client())
        )
    assert list((tmp_path / "tiny/.quire-core/cache").rglob("*.jpg"))
    assert not list((tmp_path / "tiny").glob("*.pdf"))


def test_size_limit_includes_final_volume_title(tmp_path):
    site = rule(tmp_path)
    first = asyncio.run(
        run_series(
            URL,
            tmp_path / "reference",
            split_by="size 1MB",
            rule=site,
            fetcher=client(),
            compression=CompressionOptions("archive", None),
        )
    )
    limit = first.volumes[0].bytes_out - 1
    result = asyncio.run(
        run_series(
            URL,
            tmp_path / "bounded",
            split_by=f"size {limit}",
            rule=site,
            fetcher=client(),
            compression=CompressionOptions("archive", None),
        )
    )
    assert len(result.volumes) > 1
    assert all(v.bytes_out <= limit for v in result.volumes)


@pytest.mark.parametrize("value", ["chapters 0", "size nope", "invalid", "volume 2"])
def test_bad_split(value):
    with pytest.raises(ConfigError):
        split_spec(value)


def test_volume_detection_fallback_order_range():
    doc = parse(CATALOGUE)
    volumes, warnings = plan_volumes(doc, URL, split_by="volume", volume_selector=".volume")
    assert len(volumes) == 2 and not warnings
    groups, warnings = plan_volumes(doc, URL, split_by="volume", fallback_chapters=3)
    assert [len(v.chapters) for v in groups] == [3, 1] and warnings
    groups, _ = plan_volumes(doc, URL, split_by="chapters 1", first=2, last=3)
    assert [v.chapters[0].title for v in groups] == ["第2话", "第3话"]
    named = parse(
        CATALOGUE.replace("第1话", "第1卷 第1话")
        .replace("第2话", "第1卷 第2话")
        .replace("第3话", "Vol.2 第3话")
        .replace("第4话", "Vol.2 第4话")
    )
    assert len(plan_volumes(named, URL, split_by="volume")[0]) == 2
    for first, last in ((0, 0), (2, 1)):
        with pytest.raises(ConfigError):
            plan_volumes(doc, URL, first=first, last=last)
    with pytest.raises(ConfigError):
        plan_volumes(doc, URL, fallback_chapters=0)
    # 范围超出目录是参数错误（fail-fast），不是解析失败
    with pytest.raises(ConfigError, match="超出当前目录"):
        plan_volumes(doc, URL, first=100)


def test_site_configuration_and_diagnostics(tmp_path):
    site = rule(tmp_path)
    assert resolve_rule(tmp_path, URL) == site
    assert resolve_rule(tmp_path, "https://other.test") is None
    assert site.manga(MangaOptions()).selector == ".reader img"
    assert site.manga(MangaOptions(selector="#explicit")).selector == "#explicit"
    assert site.novel(NovelOptions()).chapter_selector == ".chapter-list a"
    with pytest.raises(ConfigError):
        resolve_rule(tmp_path, "https://other.test", "series.test")
    with pytest.raises(ConfigError):
        new_rule(tmp_path, "series.test")
    for name in ("../x", "UPPER", "x/y", "a..b"):
        with pytest.raises(ConfigError):
            rule_path(tmp_path, name)
    details = diagnose(parse(CATALOGUE), URL, site)
    assert details["chapters"] == 4 and details["granularity"] == "series"
    broken = replace(site, chapter_links="#missing")
    details = diagnose(parse(CATALOGUE), URL, broken)
    assert details["warnings"] and details["suggestions"][0]["matches"] == 4
    rule_path(tmp_path, "copy").write_text(rule_path(tmp_path, "series.test").read_text())
    with pytest.raises(ConfigError, match="多个"):
        resolve_rule(tmp_path, URL)


@pytest.mark.parametrize(
    "replacement",
    [
        "version = 2",
        "version = true",
        "version = 1\nunknown = 1",
        "version = 1\ndomains = []",
        'version = 1\ndomains = ["series.test"]\nkind = "bad"',
        'version = 1\ndomains = ["series.test"]\nlast_verified = "yesterday"',
        'version = 1\ndomains = ["series.test"]\n[chapter]\nimage_selector = "a:nth-child(2)"',
        'version = 1\ndomains = ["series.test"]\n[chapter]\nimage_selector = 1',
        'version = 1\ndomains = ["series.test"]\n[chapter]\nimage_attrs = ["bad key"]',
        "broken = [",
    ],
)
def test_invalid_rule(tmp_path, replacement):
    path = new_rule(tmp_path, "series.test")
    path.write_text(replacement)
    with pytest.raises(ConfigError):
        load_rule(tmp_path, "series.test")


def test_profile_and_sites_cli(tmp_path, capsys):
    root = str(tmp_path)
    assert main(["sites", "--data-dir", root, "new", "series.test"]) == 0
    assert main(["sites", "--data-dir", root, "list"]) == 0
    assert "series.test" in capsys.readouterr().out
    assert (
        main(
            [
                "sites",
                "--data-dir",
                root,
                "set",
                "series.test",
                "chapter.image_selector",
                "#reader img",
            ]
        )
        == 0
    )
    assert load_rule(tmp_path, "series.test").image_selector == "#reader img"
    profile = tmp_path / "profile.json"
    assert main(["profile", "--data-dir", root, "export", str(profile)]) == 0
    data = json.loads(profile.read_text())
    assert "output_dir" not in data["settings"]
    data["settings"]["theme"] = "darkroom:dark"
    profile.write_text(json.dumps(data))
    assert main(["profile", "--data-dir", root, "import", str(profile)]) == 0
    assert json.loads((tmp_path / "settings.json").read_text())["theme"] == "darkroom:dark"
    data["settings"]["output_dir"] = "/tmp/forbidden"
    profile.write_text(json.dumps(data))
    assert main(["profile", "--data-dir", root, "import", str(profile)]) == 1


def test_offline_unknown_or_wrong_task(tmp_path):
    with pytest.raises(ConfigError):
        asyncio.run(run_reassemble("../no", tmp_path / "b.pdf", workdir=tmp_path))
    with pytest.raises(ConfigError):
        asyncio.run(run_reassemble("0" * 64, tmp_path / "b.pdf", workdir=tmp_path))
    with Ledger(tmp_path) as ledger:
        with pytest.raises(LedgerError):
            ledger.describe("0" * 64)


def test_size_split_changed_range_does_not_reuse_old_volume(tmp_path):
    site = rule(tmp_path)
    opts = MangaOptions(keep_images=True, overwrite=True)
    reference = asyncio.run(
        run_series(
            URL,
            tmp_path / "ref",
            split_by="chapters 1",
            rule=site,
            fetcher=client(),
            compression=CompressionOptions("archive", None),
        )
    )
    small = max(v.bytes_out for v in reference.volumes) + 500
    narrow = asyncio.run(
        run_series(
            URL,
            tmp_path / "same",
            split_by=f"size {small}",
            rule=site,
            options=opts,
            fetcher=client(),
            compression=CompressionOptions("archive", None),
        )
    )
    assert len(narrow.volumes) == 4
    wide = asyncio.run(
        run_series(
            URL,
            tmp_path / "same",
            split_by="size 1MB",
            rule=site,
            options=opts,
            fetcher=client(),
            compression=CompressionOptions("archive", None),
        )
    )
    assert len(wide.volumes) == 1 and not wide.volumes[0].artifacts_reused
    assert len(PdfReader(wide.volumes[0].output).pages) == 4


def test_size_split_delivers_prefix_before_later_oversize(tmp_path):
    from tests.conftest import make_image_bytes

    large = make_image_bytes("JPEG", size=(800, 1100), quality=95)

    def handler(request):
        path = request.url.path
        if path == "/robots.txt":
            return response(200, request=request, text="User-agent: *\nAllow: /")
        if path == "/book":
            return response(200, request=request, text=CATALOGUE)
        n = int(path.rsplit("/", 1)[-1].split(".")[0])
        if path.startswith("/chapter/"):
            return response(
                200, request=request, text=f'<main class="reader"><img src="/pages/{n}.jpg"></main>'
            )
        return response(200, request=request, content=large if n == 4 else page_image(n))

    delivered = []
    with pytest.raises(ConfigError, match="已交付"):
        asyncio.run(
            run_series(
                URL,
                tmp_path / "partial",
                split_by="size 30000",
                rule=rule(tmp_path),
                compression=CompressionOptions("archive", None),
                fetcher=AsyncFetcher(rate=10000, transport=httpx.MockTransport(handler)),
                on_volume=delivered.append,
            )
        )
    assert delivered and sum(len(PdfReader(v.output).pages) for v in delivered) == 3
    assert all(v.bytes_out <= 30000 for v in delivered)
