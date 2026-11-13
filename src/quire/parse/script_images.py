"""脚本里内嵌的整章图片清单（渲染前先扫一遍，命中就不用起浏览器）。

参照 Mihon/HakuNeko 各模板的做法，常见藏法有三种：

* SinMH 模板：``var chapterImages = [...]; var chapterPath = "..."``，图片为绝对地址，
  或相对地址配 ``pageImage``/``resHost`` 给出的图床；
* 具名数组：``newImgs``（DM5）、``img_data``、``imgList``、``picdata``、MangaThemesia
  ``ts_reader.run({"sources":[{"images":[...]}]})`` 等；
* ``data:text/javascript;base64,`` 内联脚本与 ``atob("…")``：先解码再按上面两条扫。

不认识名字的数组只在「≥5 张、同一目录」时采用，避免把推荐作品封面当成正文。
"""

from __future__ import annotations

import base64
import binascii
import json
import posixpath
import re
from urllib.parse import urljoin, urlsplit

_IMAGE = re.compile(r"\.(?:jpe?g|png|webp|gif|avif|bmp)(?:$|[?#])", re.I)
_NAMED = re.compile(
    r"(?:chapterImages|newImgs|img_?data|imgList|img_list|picdata|pic_?list|images|imgs|pages)"
    r"""["']?\s*[:=]\s*(\[[^\[\]]{10,}\])""",
    re.I,
)
_ARRAY = re.compile(r"""\[\s*"(?:https?:)?(?:\\?/){2}[^"]+"(?:\s*,\s*"[^"]*")+\s*\]""")
_CHAPTER_PATH = re.compile(r"""chapterPath\s*=\s*["']([^"']*)["']""")
_RES_HOST = re.compile(
    r"""(?:pageImage\s*=\s*["'](https?://[^/"']+)|resHost[^\]]{0,80}?["'](https?://[^"']+)["'])"""
)
_BASE64_SCRIPT = re.compile(r"data:text/javascript;base64,([A-Za-z0-9+/=]{16,})")
_ATOB = re.compile(r"""atob\(\s*["']([A-Za-z0-9+/=]{16,})["']\s*\)""")


def _decoded_scripts(html: str) -> str:
    extra: list[str] = []
    for pattern in (_BASE64_SCRIPT, _ATOB):
        for match in pattern.finditer(html):
            try:
                extra.append(base64.b64decode(match[1]).decode("utf-8", "replace"))
            except (binascii.Error, ValueError):
                continue
    return "\n".join(extra)


def _strings(raw: str) -> list[str]:
    try:
        data = json.loads(raw)
    except ValueError:
        try:
            data = json.loads(raw.replace("'", '"'))
        except ValueError:
            return []
    if not isinstance(data, list):
        return []
    return [item.strip() for item in data if isinstance(item, str) and item.strip()]


def _resolve(items: list[str], html: str, page_url: str) -> list[str]:
    path = _CHAPTER_PATH.search(html)
    host = _RES_HOST.search(html)
    origin = (host[1] or host[2]) if host else ""
    out: list[str] = []
    for item in items:
        if item.startswith("//"):
            item = f"{urlsplit(page_url).scheme or 'https'}:{item}"
        if not item.startswith(("http://", "https://")):
            if not origin:
                return []  # 相对地址又不知道图床：交给渲染
            prefix = "" if item.startswith("/") or not path else "/" + path[1].strip("/") + "/"
            item = urljoin(origin.rstrip("/") + "/", (prefix + item).lstrip("/"))
        out.append(item)
    return out


def _same_folder(urls: list[str]) -> bool:
    folders = {posixpath.dirname(urlsplit(u).path) for u in urls}
    return len(folders) == 1


def extract_array_images(html: str, page_url: str = "") -> list[str]:
    """返回脚本内嵌的整章图片地址；找不到可信清单时返回空。"""
    text = html + "\n" + _decoded_scripts(html)
    best: list[str] = []
    for match in _NAMED.finditer(text):
        items = [i for i in _strings(match[1]) if _IMAGE.search(i)]
        urls = _resolve(items, text, page_url) if len(items) >= 2 else []
        if len(urls) > len(best):
            best = urls
    if best:
        return best[:2000]
    for match in _ARRAY.finditer(text):
        urls = _resolve([i for i in _strings(match[0]) if _IMAGE.search(i)], text, page_url)
        if len(urls) >= 5 and _same_folder(urls) and len(urls) > len(best):
            best = urls
    return best[:2000]
