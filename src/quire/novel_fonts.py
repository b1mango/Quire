"""章节页的字体反混淆编排:下载混淆字体、按 hash 缓存映射、改写页面。

parse/fontmap 是纯比对(不联网);这里负责按页面 @font-face 引用下载字体、
按字体内容 SHA-256 缓存映射(同一本书所有章节共用一份字体,整书只解析
一次),然后把整页文本里的 PUA 字符替换回汉字。

忠实性边界:页面有混淆迹象但字体取不到、字形认不全或替换后仍有 PUA 残留
时,抛 ParseError 让这一章如实失败,绝不把乱码当正文交付。
"""

from __future__ import annotations

import hashlib
from typing import Protocol

from .errors import FetchError, ParseError
from .fetch.simple import Response
from .parse import fontmap


class _FontFetcher(Protocol):
    async def get(
        self, url: str, *, referer: str | None = None, robots: bool = True
    ) -> Response: ...


class FontCache:
    """混淆字体映射缓存:按 URL 记住已解决的映射,按内容哈希去重解析。

    同一本书所有章节共用同一字体地址,整书只下载一次、解析一次;
    站点中途换字体(同 URL 不同内容)时由覆盖检查触发重取。
    """

    def __init__(self) -> None:
        self._by_url: dict[str, dict[str, str]] = {}
        self._by_hash: dict[str, dict[str, str]] = {}

    def cached(self, url: str, needed: frozenset[str]) -> dict[str, str] | None:
        mapping = self._by_url.get(url)
        return mapping if mapping is not None and not (needed - mapping.keys()) else None

    def store(self, url: str, font: bytes) -> dict[str, str]:
        key = hashlib.sha256(font).hexdigest()
        if key not in self._by_hash:
            self._by_hash[key] = fontmap.build_mapping(font)
        self._by_url[url] = self._by_hash[key]
        return self._by_hash[key]


async def deobfuscate_page(client: _FontFetcher, page: Response, cache: FontCache) -> Response:
    """页面含成规模 PUA 字符时尝试字体反混淆;否则原样返回。"""
    if not fontmap.needs_deobfuscation(page.text):
        return page
    urls = fontmap.find_font_urls(page.text)
    if not urls:
        raise ParseError(
            "正文被自定义字体混淆,但页面没有引用字体文件",
            hint="站点可能改为接口下发字形;请反馈该站点。",
        )
    needed = fontmap.pua_chars(page.text)
    errors: list[str] = []
    for url in urls:
        mapping = cache.cached(url, needed)
        if mapping is None:
            try:
                font = (await client.get(url, referer=page.url, robots=False)).content
                mapping = cache.store(url, font)
            except (FetchError, ParseError) as exc:
                errors.append(str(exc))
                continue
        missing = needed - mapping.keys()
        if not missing:
            text = fontmap.translate(page.text, mapping)
            headers = {**page.headers, "content-type": "text/html; charset=utf-8"}
            return Response(page.url, page.status, headers, text.encode("utf-8"), page.elapsed_ms)
        errors.append(f"{len(missing)} 个字形未识别")
    detail = ";".join(errors) or "无可用字体"
    raise ParseError("字体反混淆失败,本章无法还原", hint=f"{detail}。不输出乱码内容,请反馈该站点。")
