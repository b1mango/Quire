"""阅读页把正文放在内嵌框架里时,改读框架页。

* 苹果 CMS(MacCMS)模板:阅读页只有 ``var player_aaaa={…}``,播放器脚本
  ``/static/player/<from>.js`` 用 ``'前缀' + MacPlayer.PlayUrl + '后缀'`` 拼出 iframe 地址,
  漫画站的图片都在这个 iframe 页里(kalamanhua 等)。这里照播放器的做法解码
  (encrypt 1 = unescape,2 = base64 后 unescape,其余原样)并拼出框架地址。
* 漫画页本身没有图、只有一个同站或图床 iframe 时,也改读 iframe。

框架页取回后作为「本章页面」交给通用图片识别;任何一步失败都退回原页面,不终止任务。
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from typing import Protocol
from urllib.parse import unquote, urljoin, urlsplit

from ..errors import FetchError
from ..fetch.simple import Response
from ..parse.minidom import parse as parse_html

_LOG = logging.getLogger(__name__)

_PLAYER = re.compile(r"var\s+player_[a-z]+\s*=\s*(\{.*?\})\s*;?\s*</script>", re.S)
_CMS_PATH = re.compile(r"var\s+maccms\s*=\s*\{[^}]*\"path\"\s*:\s*\"([^\"]*)\"")
_FRAME_TEMPLATE = re.compile(
    r"""src\s*=\s*\\?["']([^"'+]*)['"]\s*\+\s*MacPlayer\.PlayUrl\s*\+\s*['"]([^"']*)\\?["']"""
)
_PLACEHOLDER = re.compile(r"^data:|^about:|^javascript:", re.I)


class _Client(Protocol):
    async def get(
        self, url: str, *, referer: str | None = None, headers: dict[str, str] | None = None
    ) -> Response: ...


def _player(html: str) -> dict[str, object] | None:
    found = _PLAYER.search(html)
    if found is None:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(found[1])
    except ValueError:
        return None
    return data if isinstance(data, dict) and isinstance(data.get("url"), str) else None


def player_url(player: dict[str, object]) -> str:
    """按 MacPlayer.Init 的规则解出 PlayUrl。"""
    url, encrypt = str(player.get("url") or ""), str(player.get("encrypt") or "0")
    if encrypt == "1":
        return unquote(url)
    if encrypt == "2":
        try:
            return unquote(base64.b64decode(url).decode("utf-8", "replace"))
        except (binascii.Error, ValueError):
            return url
    return url


def frame_from_script(script: str, play_url: str) -> str | None:
    found = _FRAME_TEMPLATE.search(script)
    return f"{found[1]}{play_url}{found[2]}" if found else None


def _has_images(html: str) -> bool:
    for node in parse_html(html).iter_elements():
        if node.tag != "img":
            continue
        for attr in ("data-src", "data-original", "src"):
            value = node.get(attr) or ""
            if value and not _PLACEHOLDER.match(value):
                return True
    return False


def _lone_iframe(page: Response) -> str | None:
    frames = [
        urljoin(page.url, node.get("src") or "")
        for node in parse_html(page.text).iter_elements()
        if node.tag == "iframe" and (node.get("src") or "").strip()
    ]
    frames = [f for f in frames if urlsplit(f).scheme in {"http", "https"}]
    return frames[0] if len(frames) == 1 else None


async def follow(client: _Client, page: Response, *, manga: bool) -> Response:
    """若正文在内嵌框架里,返回框架页;否则原样返回。"""
    target: str | None = None
    player = _player(page.text)
    if player is not None:
        play_url = player_url(player)
        path = _CMS_PATH.search(page.text)
        script_url = urljoin(
            page.url, f"{path[1] if path else ''}/static/player/{player.get('from', '')}.js"
        )
        try:
            script = await client.get(script_url, referer=page.url)
        except FetchError as exc:
            _LOG.warning("阅读页播放器脚本读取失败,按原页面识别:%s", exc)
            return page
        target = frame_from_script(script.text, play_url)
    elif manga and not _has_images(page.text):
        target = _lone_iframe(page)
    if not target:
        return page
    try:
        frame = await client.get(urljoin(page.url, target), referer=page.url)
    except FetchError as exc:
        _LOG.warning("阅读框架页读取失败,按原页面识别:%s", exc)
        return page
    return frame if _has_images(frame.text) else page
