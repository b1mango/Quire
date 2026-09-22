"""quanben.io(全本小说网)目录适配。

列表页 ``/n/<slug>/list.html`` 只内嵌首尾各一段章节,完整目录由页面脚本
经 JSONP 接口 ``/index.php?c=book&a=list.jsonp`` 加载。接口校验三个参数:

* ``callback``:页面内嵌 ``var callback='…'``(每页随机);
* ``book_id``:``load_more('<id>')`` 的参数;
* ``b``:对 callback 逐字符混淆——查页面内嵌的置换字母表偏移 3 位,
  首尾各夹一个随机字符(随机部分服务端不校验)。

这里按页面内嵌算法重建 ``b``、带目录页 Referer 请求接口,把返回的章节
与页面内嵌的两段合并、按 URL 章号排序后重建一份完整列表并入页面,再
交给通用目录识别;接口失败时退回页面内嵌的部分目录,不因此终止任务。
"""

from __future__ import annotations

import json
import logging
import random
import re
from html import escape
from typing import Protocol
from urllib.parse import quote, urlsplit, urlunsplit

from ..errors import FetchError, ParseError
from ..fetch.simple import Response
from ..parse.minidom import parse as parse_html

_LOG = logging.getLogger(__name__)

_HOSTS = frozenset({"quanben.io", "www.quanben.io"})
_LIST_PATH = re.compile(r"^/n/[^/]+/list\.html$")
_CHAPTER_PATH = re.compile(r"^/n/[^/]+/(\d+)\.html$")
_CALLBACK = re.compile(r"var\s+callback\s*=\s*'([A-Za-z0-9]{1,32})'")
_BOOK_ID = re.compile(r"load_more\('(\d{1,12})'\)")
#: 页面内嵌 base64() 的置换字母表(是标准 base64 字母表的乱序,非编码)。
_STATIC_CHARS = "PXhw7UT1B0a9kQDKZsjIASmOezxYG4CHo5Jyfg2b8FLpEvRr3WtVnlqMidu6cN"


class _CatalogueClient(Protocol):
    async def get(self, url: str, *, referer: str | None = None) -> Response: ...


def is_list_page(url: str) -> bool:
    parts = urlsplit(url)
    return (parts.hostname or "").lower() in _HOSTS and _LIST_PATH.match(parts.path) is not None


def _credentials(html: str) -> tuple[str, str] | None:
    """页面内嵌的 (callback, book_id);没有则说明结构已变,回退通用识别。"""
    callback, book = _CALLBACK.search(html), _BOOK_ID.search(html)
    return (callback[1], book[1]) if callback and book else None


def obfuscate(callback: str, rng: random.Random) -> str:
    """复刻页面 base64():每字符 → 随机字符 + 查表偏移 3 位 + 随机字符。"""
    size = len(_STATIC_CHARS)
    out: list[str] = []
    for char in callback:
        index = _STATIC_CHARS.find(char)
        code = _STATIC_CHARS[(index + 3) % size] if index >= 0 else char
        out.append(_STATIC_CHARS[rng.randrange(size)] + code + _STATIC_CHARS[rng.randrange(size)])
    return "".join(out)


def jsonp_url(page_url: str, callback: str, book_id: str, token: str) -> str:
    parts = urlsplit(page_url)
    origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    return (
        f"{origin}/index.php?c=book&a=list.jsonp&callback={quote(callback)}"
        f"&book_id={quote(book_id)}&b={quote(token)}"
    )


def jsonp_content(body: str, callback: str) -> str:
    """剥掉 ``d<callback>(…)`` 包裹,取出章节目录 HTML;格式不符明确报错。"""
    prefix = f"d{callback}("
    text = body.strip()
    if not text.startswith(prefix):
        raise ParseError("quanben 目录接口返回格式不符", hint="站点可能调整了接口结构。")
    text = text[len(prefix) :].rstrip().removesuffix(";")
    if not text.endswith(")"):
        raise ParseError("quanben 目录接口返回格式不符", hint="站点可能调整了接口结构。")
    try:
        payload = json.loads(text[:-1])
    except ValueError:
        raise ParseError("quanben 目录接口返回的不是合法 JSON") from None
    content = payload.get("content") if isinstance(payload, dict) else None
    if not isinstance(content, str) or "<a " not in content:
        raise ParseError("quanben 目录接口没有返回章节列表")
    return content


async def expand_listing(
    client: _CatalogueClient, page: Response, *, rng: random.Random | None = None
) -> Response:
    """quanben 列表页并入 JSONP 全量目录;非目标页或接口失败时原样返回。

    页面内嵌首尾两段、接口给中间段;三者按 URL 章号合并去重后重建一份
    完整列表附在页尾。目录识别按 URL 去重时以后出现者为准,阅读顺序即
    章号顺序——章节标题重号(实测存在)不影响。
    """
    if not is_list_page(page.url):
        return page
    credentials = _credentials(page.text)
    if credentials is None:
        return page
    callback, book_id = credentials
    url = jsonp_url(page.url, callback, book_id, obfuscate(callback, rng or random.Random()))
    try:
        response = await client.get(url, referer=page.url)
        content = jsonp_content(response.text, callback)
    except (FetchError, ParseError) as exc:
        _LOG.warning("quanben 全量目录接口失败,退回页面内嵌的部分目录:%s", exc)
        return page
    anchors = _chapter_anchors(content)
    anchors.update(_chapter_anchors(page.text))
    if not anchors:
        _LOG.warning("quanben 目录接口返回的链接形态已变,退回页面内嵌的部分目录")
        return page
    slug = urlsplit(page.url).path.removesuffix("list.html")
    items = "".join(
        f'<li><a href="{escape(slug)}{number}.html"><span>{escape(title)}</span></a></li>'
        for number, title in sorted(anchors.items())
    )
    block = f'<ul class="list3">{items}</ul>'
    html = page.text
    anchor = html.rfind("</body>")
    merged = html[:anchor] + block + html[anchor:] if anchor >= 0 else html + block
    headers = {**page.headers, "content-type": "text/html; charset=utf-8"}
    return Response(page.url, page.status, headers, merged.encode("utf-8"), page.elapsed_ms)


def _chapter_anchors(html: str) -> dict[int, str]:
    """页面/接口 HTML 里的章节链接:URL 章号 → 标题(同章号后者覆盖)。"""
    found: dict[int, str] = {}
    for node in parse_html(html).iter_elements():
        if node.tag != "a":
            continue
        match = _CHAPTER_PATH.match(urlsplit(node.get("href") or "").path)
        if match:
            found[int(match[1])] = node.text.strip()
    return found
