"""字体反混淆:指纹库比对、页面改写与失败路径。

夹具 obfuscated-sans.woff 由 scripts/build_font_db.py 生成——把 40 个
常用汉字的思源黑体字形重映射到 PUA 码位,模拟站点的混淆字体;
obfuscated-truth.json 记录 PUA 码位 → 原字的正确答案。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from quire.errors import FetchError, ParseError
from quire.fetch.simple import Response
from quire.novel_fonts import FontCache, deobfuscate_page
from quire.parse import fontmap

FIXTURES = Path(__file__).parent / "fixtures"
FONT = (FIXTURES / "obfuscated-sans.woff").read_bytes()
TRUTH = json.loads((FIXTURES / "obfuscated-truth.json").read_text("utf-8"))

PAGE_URL = "https://obfuscated.test/reader/1"
FONT_URL = "https://obfuscated.test/fonts/x.woff"


def obfuscated_page(text: str) -> str:
    return (
        f"<html><head><style>@font-face{{font-family:ObfX;"
        f"src:url({FONT_URL}) format('woff');}}</style></head>"
        f"<body><div id='content'>{text}</div></body></html>"
    )


def encode(text: str) -> str:
    """按夹具真相表把正常文字转成 PUA 混淆文本。"""
    reverse = {char: chr(int(code, 16)) for code, char in TRUTH.items()}
    return "".join(reverse.get(char, char) for char in text)


def test_fixture_mapping_recovers_every_char() -> None:
    """夹具字体的全部 PUA 字形都被指纹库认出,且答案正确。"""
    mapping = fontmap.build_mapping(FONT)
    expected = {chr(int(code, 16)): char for code, char in TRUTH.items()}
    assert mapping == expected


def test_needs_deobfuscation_threshold() -> None:
    assert not fontmap.needs_deobfuscation("正常正文没有混淆")
    assert not fontmap.needs_deobfuscation("只有零星几个")
    assert fontmap.needs_deobfuscation("" * fontmap.MIN_PUA_CHARS)


def test_find_font_urls_prefers_plain_woff() -> None:
    html = (
        "@font-face{font-family:X;src:url(https://a.test/f.woff2)format('woff2'),"
        "url(https://a.test/f.woff)format('woff'),url(https://a.test/f.otf)format('opentype')}"
    )
    urls = fontmap.find_font_urls(html)
    assert urls[:3] == [
        "https://a.test/f.woff",
        "https://a.test/f.otf",
        "https://a.test/f.woff2",
    ]


def test_build_mapping_rejects_unreadable_font() -> None:
    with pytest.raises(ParseError):
        fontmap.build_mapping(b"not a font")


def test_build_mapping_rejects_font_without_pua() -> None:
    """没有 PUA 字形的字体(正常字体)不构成映射,明确报错。"""
    from fontTools.ttLib import TTFont

    buffer = Path(__file__).parent / "fixtures"
    plain = TTFont(FIXTURES / "obfuscated-sans.woff")
    for table in plain["cmap"].tables:
        if table.isUnicode():
            table.cmap.clear()
            table.cmap.update({ord("A"): plain.getGlyphOrder()[1]})
    import io

    out = io.BytesIO()
    plain.flavor = "woff"
    plain.save(out)
    with pytest.raises(ParseError, match="没有 PUA 字形"):
        fontmap.build_mapping(out.getvalue())
    assert buffer.exists()


class FontClient:
    """返回夹具字体的桩;记录字体请求数,验证同任务只解析一次。"""

    def __init__(self, font: bytes | Exception = FONT) -> None:
        self.font = font
        self.font_requests = 0

    async def get(self, url: str, *, referer: str | None = None, robots: bool = True) -> Response:
        if url == FONT_URL:
            self.font_requests += 1
            if isinstance(self.font, Exception):
                raise self.font
            return Response(url, 200, {"content-type": "font/woff"}, self.font, 1)
        return Response(url, 200, {"content-type": "text/html; charset=utf-8"}, b"", 1)


def test_deobfuscate_page_replaces_pua() -> None:
    text = "我的一是在" * 5
    page_html = obfuscated_page(encode(text))
    page = Response(
        PAGE_URL, 200, {"content-type": "text/html; charset=utf-8"}, page_html.encode(), 1
    )
    client = FontClient()
    cache = FontCache()
    result = asyncio.run(deobfuscate_page(client, page, cache))
    assert text in result.text
    assert not fontmap.pua_chars(result.text)
    # 第二次命中缓存,不再重复请求字体文件
    again = asyncio.run(deobfuscate_page(client, page, cache))
    assert text in again.text
    assert client.font_requests == 1


def test_deobfuscate_page_passthrough_without_pua() -> None:
    page = Response(PAGE_URL, 200, {}, "正常页面没有混淆".encode(), 1)
    client = FontClient()
    result = asyncio.run(deobfuscate_page(client, page, FontCache()))
    assert result is page
    assert client.font_requests == 0


def test_deobfuscate_page_fails_without_font_reference() -> None:
    html = "<html><body><p>" + encode("我的一是在" * 3) + "</p></body></html>"
    page = Response(PAGE_URL, 200, {}, html.encode(), 1)
    with pytest.raises(ParseError, match="没有引用字体文件"):
        asyncio.run(deobfuscate_page(FontClient(), page, FontCache()))


def test_deobfuscate_page_fails_when_font_unfetchable() -> None:
    page_html = obfuscated_page(encode("我的一是在" * 3))
    page = Response(PAGE_URL, 200, {}, page_html.encode(), 1)
    client = FontClient(FetchError("timeout"))
    with pytest.raises(ParseError, match="字体反混淆失败"):
        asyncio.run(deobfuscate_page(client, page, FontCache()))


def test_capture_chapter_deobfuscates_and_caches(tmp_path: Path) -> None:
    """章级链路:混淆页面抓取后,缓存里存的是还原后的正文。"""
    from quire.core_chapter_cache import decode_chapter
    from quire.core_chapters import capture_chapters
    from quire.models import NovelOptions
    from quire.parse.chapters import ChapterLink
    from quire.store.cache import read_cached
    from quire.store.ledger import Ledger
    from quire.store.models import ResourceSpec, task_identity

    prose = "我的一是在，他在那里有。" * 25
    html = obfuscated_page(f"<h1>第一章 测试</h1><div id='content'><p>{encode(prose)}</p></div>")

    class Client(FontClient):
        async def get(
            self, url: str, *, referer: str | None = None, robots: bool = True
        ) -> Response:
            if url == PAGE_URL:
                return Response(
                    url, 200, {"content-type": "text/html; charset=utf-8"}, html.encode(), 1
                )
            return await super().get(url, referer=referer, robots=robots)

    link = ChapterLink("第一章 测试", PAGE_URL)
    specs = [ResourceSpec(1, 1, link.url)]
    task_id, _ = task_identity(PAGE_URL, {"chapters": [[link.title, link.url]]}, specs)
    with Ledger(tmp_path / "work") as ledger:
        ledger.create_task(PAGE_URL, {"chapters": [[link.title, link.url]]}, specs)
        result = asyncio.run(
            capture_chapters(
                Client(),
                ledger,
                task_id,
                (link,),
                options=NovelOptions(rate=1000.0, retries=0, concurrency=1),
            )
        )
        record = result.snapshot.resources[0]
        assert record.status == "done"
        cached = decode_chapter(
            read_cached(
                ledger.root,
                task_id,
                record.local_path or "",
                sha256=record.sha256 or "",
                size=record.size or 0,
            )
        )
        assert cached.paragraphs == (prose,)


def test_capture_chapter_fails_honestly_when_font_broken(tmp_path: Path) -> None:
    """字体取不到时整章失败并写明原因,乱码不进缓存。"""
    from quire.core_chapters import capture_chapters
    from quire.models import NovelOptions
    from quire.parse.chapters import ChapterLink
    from quire.store.ledger import Ledger
    from quire.store.models import ResourceSpec, task_identity

    html = obfuscated_page(f"<h1>第一章 测试</h1><p>{encode('我的一是在，他在那里有。' * 25)}</p>")

    class Client(FontClient):
        async def get(
            self, url: str, *, referer: str | None = None, robots: bool = True
        ) -> Response:
            if url == PAGE_URL:
                return Response(url, 200, {}, html.encode(), 1)
            return await super().get(url, referer=referer, robots=robots)

    link = ChapterLink("第一章 测试", PAGE_URL)
    specs = [ResourceSpec(1, 1, link.url)]
    task_id, _ = task_identity(PAGE_URL, {"chapters": [[link.title, link.url]]}, specs)
    with Ledger(tmp_path / "work") as ledger:
        ledger.create_task(PAGE_URL, {"chapters": [[link.title, link.url]]}, specs)
        result = asyncio.run(
            capture_chapters(
                Client(FetchError("timeout")),
                ledger,
                task_id,
                (link,),
                options=NovelOptions(rate=1000.0, retries=0, concurrency=1),
            )
        )
        assert result.snapshot.resources[0].status == "failed"
        assert result.snapshot.resources[0].error_code == "invalid_text"
        assert any("字体反混淆失败" in w for w in result.warnings)
