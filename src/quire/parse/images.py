"""漫画图片的收集、过滤与排序。**全部是纯函数**（项目设计.md §17.1 ①）。

"效率与成功率"在这里体现为两件事：
  * **收集要贪**：网页里图片地址有五种藏法（懒加载属性、srcset、
    CSS background、a[href]、noscript），漏一种就少一页；
  * **过滤要准**：logo / 广告 / 占位图混进去，用户翻到才发现，
    整本书的信任度就没了。

所以这里分成 `collect`（贪）→ `prefilter`（URL 层粗筛，不联网）
→ `postfilter`（下载后按真实尺寸/完整性精筛）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from ..image.probe import ImageProbe
from ..parse.minidom import Document, Node
from ..utils.naming import natural_key
from ..utils.urls import is_image_url, is_usable_url, join_url

#: 懒加载属性的尝试顺序。``src`` 放最后是因为懒加载站点的 ``src``
#: 通常是占位 GIF，真正的地址在 ``data-*`` 里。
LAZY_ATTRS: tuple[str, ...] = (
    "data-src",
    "data-original",
    "data-lazy-src",
    "data-lazyload",
    "data-echo",
    "data-url",
    "data-cfsrc",
    "data-actualsrc",
    "data-image",
    "data-img",
    "data-origin",
    "_src",
    "src",
)

#: 命中这些词的 URL 直接丢——它们在漫画里 100% 是噪音。
URL_BLACKLIST = re.compile(
    r"(?:^|[/_.\-])("
    r"logo|avatar|icon|favicon|banner|ad|ads|advert|adv|spacer|blank|"
    r"loading|placeholder|ph\.|grey|gray|pixel|1x1|transparent|"
    r"qrcode|qr_|erweima|weixin|wechat|share|btn|button|arrow|bullet|dot"
    r")(?:[/_.\-]|$|\d)",
    re.IGNORECASE,
)

#: 明确是占位图的 URL 特征——它不导致丢弃，而是让我们改选别的属性。
PLACEHOLDER_HINT = re.compile(
    r"(blank|spacer|placeholder|loading|grey|gray|pixel|1x1|transparent|default)",
    re.IGNORECASE,
)

_CSS_URL = re.compile(r"""url\(\s*['"]?(?P<url>[^'")]+)['"]?\s*\)""", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Candidate:
    """一张待下载的候选图片。``order`` 是 DOM 序，后面排序算法要用。"""

    url: str
    order: int
    source: str = "img"  # img | srcset | css | anchor | style
    referer: str = ""
    alternatives: tuple[str, ...] = ()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Candidate #{self.order} {self.source} {self.url[-40:]}>"


@dataclass(frozen=True, slots=True)
class Rejection:
    """被丢掉的原因。``quire inspect --explain`` 直接打印这些（§6.14）。"""

    url: str
    reason: str
    stage: str  # url | duplicate | size | format | decode


# ============================================================ 收集


def parse_srcset(value: str) -> list[tuple[str, float]]:
    """解析 ``srcset``，返回 ``[(url, 权重)]``，权重取 ``800w`` 的 800 或 ``2x`` 的 2。"""
    out: list[tuple[str, float]] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split()
        url = bits[0]
        weight = 1.0
        if len(bits) > 1:
            desc = bits[1].strip().lower()
            try:
                if desc.endswith("w"):
                    weight = float(desc[:-1])
                elif desc.endswith("x"):
                    weight = float(desc[:-1]) * 1000
            except ValueError:
                weight = 1.0
        out.append((url, weight))
    return out


def _pick_best_srcset(value: str) -> str:
    entries = parse_srcset(value)
    if not entries:
        return ""
    return max(entries, key=lambda e: e[1])[0]


def _img_urls(node: Node, base_url: str, attrs: Sequence[str]) -> list[tuple[str, str]]:
    """从单个 ``<img>`` 上收集候选 ``(url, source)``，按可信度排序。

    先看 ``srcset``（它通常指向最高分辨率），再看懒加载属性，
    最后才轮到 ``src``。
    """
    found: list[tuple[str, str]] = []

    srcset = node.get("data-srcset") or node.get("srcset") or ""
    if not srcset and node.parent is not None and node.parent.tag == "picture":
        sources = node.parent.find_all("source")
        srcset = next(
            (n.get("data-srcset") or n.get("srcset") or "" for n in sources if not n.get("media")),
            "",
        )
    if srcset:
        best = _pick_best_srcset(srcset)
        resolved = join_url(base_url, best)
        if resolved:
            found.append((resolved, "srcset"))

    for attr in attrs:
        raw = node.get(attr)
        if not raw:
            continue
        resolved = join_url(base_url, raw)
        if not resolved:
            continue
        found.append((resolved, "img"))

    return found


def _sort_by_trust(items: list[tuple[str, str]]) -> list[str]:
    """把同一节点上收集到的候选排序：非占位的优先，srcset 优先于 src。"""
    real = [u for u, _ in items if not PLACEHOLDER_HINT.search(u)]
    placeholder = [u for u, _ in items if PLACEHOLDER_HINT.search(u)]
    ordered = real + placeholder
    seen: set[str] = set()
    out: list[str] = []
    for u in ordered:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _declared_thumbnail(node: Node) -> bool:
    """声明展示尺寸双轴都小于 200px 的 ``<img>`` 是缩略图/图标,不是正文页。

    Webtoon 系阅读器的缩略图条(``width="92" height="87"`` + ``data-url``)
    会被懒加载收集当成正文页,下载后才被尺寸精筛拦下变成占位页——
    在收集阶段就排除,与 ``FilterPolicy`` 的 200px 阈值对齐。
    """
    try:
        width = float(node.get("width") or "")
        height = float(node.get("height") or "")
    except ValueError:
        return False
    return width < 200 and height < 200


def collect(
    doc: Document,
    base_url: str,
    *,
    selector: str | None = None,
    attrs: Sequence[str] | None = None,
    include_style_blocks: bool = True,
) -> list[Candidate]:
    """收集所有可能的图片地址。**宁滥勿缺**，精筛交给 ``postfilter``。"""
    attr_order = tuple(attrs) if attrs else LAZY_ATTRS
    positions = {id(n): i for i, n in enumerate(doc.iter_elements())}
    candidates: list[Candidate] = []

    roots = doc.select(selector) if selector else [doc]
    allowed = {id(n) for root in roots for n in root.iter_elements()}
    scoped = [n for n in doc.iter_elements() if id(n) in allowed]
    referer = doc.base_url or base_url
    for node in scoped:
        if node.tag != "img" or _declared_thumbnail(node):
            continue
        order = positions.get(id(node), len(positions))
        urls = _sort_by_trust(_img_urls(node, base_url, attr_order))
        if urls:
            candidates.append(Candidate(urls[0], order, "img", referer, tuple(urls[1:])))

    # 内联 style 的 background-image
    for node in scoped:
        style = node.get("style") or ""
        if "url(" not in style.lower():
            continue
        order = positions.get(id(node), len(positions))
        for match in _CSS_URL.finditer(style):
            url = join_url(base_url, match.group("url"))
            if url:
                candidates.append(Candidate(url=url, order=order, source="css", referer=referer))

    # <a href> 直链图片：收集顺序排在 <img> 之后
    tail_order = len(positions) + 1
    for node in scoped:
        if node.tag != "a":
            continue
        href = node.get("href") or ""
        url = join_url(base_url, href)
        if url and is_image_url(url):
            candidates.append(Candidate(url, positions[id(node)], "anchor", referer))

    # <style> 块里的 url()：最常见的"阅读器用 CSS 铺图"写法。
    # 放在最后，因为整块 CSS 里的图片未必是正文页。
    # 注意 html.parser 把 <style> 内容当文本处理，所以这里读的是文本节点。
    if include_style_blocks and not candidates:
        for node in scoped:
            if node.tag != "style":
                continue
            for match in _CSS_URL.finditer(node.text):
                url = join_url(base_url, match.group("url"))
                if url and is_image_url(url):
                    candidates.append(Candidate(url, tail_order, "style", referer))

    return sorted(candidates, key=lambda candidate: candidate.order)


# ============================================================ 过滤


def prefilter(candidates: Iterable[Candidate]) -> tuple[list[Candidate], list[Rejection]]:
    """URL 层粗筛，不联网。能在这里丢掉的绝不留到下载阶段。"""
    kept: list[Candidate] = []
    rejected: list[Rejection] = []
    seen: set[str] = set()

    for cand in candidates:
        url = cand.url
        if not is_usable_url(url):
            rejected.append(Rejection(url, "不是可用地址（data:/javascript: 等）", "url"))
            continue
        if URL_BLACKLIST.search(url.split("?", 1)[0]):
            rejected.append(Rejection(url, "命中黑名单（logo/广告/图标等）", "url"))
            continue
        if url in seen:
            rejected.append(Rejection(url, "同页重复", "duplicate"))
            continue
        seen.add(url)
        kept.append(cand)

    return kept, rejected


@dataclass(frozen=True, slots=True)
class FilterPolicy:
    """精筛阈值。默认值来自对常见漫画站的观察，可用 CLI 覆盖。"""

    min_width: int = 200
    min_height: int = 200
    min_bytes: int = 0
    max_aspect: float = 8.0
    require_complete: bool = True


def postfilter(
    items: Iterable[tuple[Candidate, ImageProbe, int]],
    policy: FilterPolicy | None = None,
) -> tuple[list[Candidate], list[Rejection]]:
    """下载后精筛。``items`` 是 ``(候选, 探测结果, 字节数)``。

    这里挡住的是最伤用户的那类问题：占位图、横幅、分隔线、
    以及**下了一半的图**（``probe.complete``）。
    """
    pol = policy or FilterPolicy()
    kept: list[Candidate] = []
    rejected: list[Rejection] = []

    for cand, probe, size in items:
        if not probe.format:
            rejected.append(Rejection(cand.url, f"不是图片：{probe.error}", "format"))
            continue
        if size < pol.min_bytes:
            rejected.append(Rejection(cand.url, f"文件过小（{size} 字节）", "size"))
            continue
        if probe.width < pol.min_width or probe.height < pol.min_height:
            rejected.append(
                Rejection(cand.url, f"尺寸过小（{probe.width}x{probe.height}）", "size")
            )
            continue
        if probe.aspect > pol.max_aspect:
            rejected.append(
                Rejection(cand.url, f"宽高比异常（{probe.aspect:.1f}），像是横幅或分隔线", "size")
            )
            continue
        if pol.require_complete and not probe.complete:
            rejected.append(Rejection(cand.url, f"文件不完整：{probe.error}", "decode"))
            continue
        kept.append(cand)

    return kept, rejected


# ============================================================ 排序


def _numeric_sorted(cands: Sequence[Candidate]) -> list[Candidate]:
    return sorted(cands, key=lambda c: natural_key(c.url))


def order_candidates(
    cands: Sequence[Candidate], mode: str = "auto"
) -> tuple[list[Candidate], str | None]:
    """决定最终页序。返回 ``(排序结果, 告警或 None)``。

    ``auto`` 的规则刻意保守：

    * DOM 序与 URL 数字序**完全一致** → 用 DOM 序（无告警）；
    * DOM 序恰好是数字序的**逆序** → 强信号，说明站点倒序输出，改用数字序并告警；
    * 其他不一致 → 保留 DOM 序并告警。

    为什么不像最初设想的那样"不一致就以数字序为准"：
    URL 里的数字未必是页码（可能是时间戳、随机 ID）。
    把 DOM 序让给一个更可疑的信号，代价是整本书错页。
    """
    if mode == "dom":
        return list(cands), None

    by_number = _numeric_sorted(cands)
    if mode == "asc":
        return by_number, None
    if mode == "desc":
        return list(reversed(by_number)), None

    if mode != "auto":
        return list(cands), f"未知的排序方式 {mode!r}，已按 DOM 序处理"

    dom = list(cands)
    if len(dom) < 3:
        return dom, None
    if [c.url for c in dom] == [c.url for c in by_number]:
        return dom, None
    return dom, "DOM 序与图片编号不一致，已保留 DOM 序（可用 --order asc/desc 覆盖）"
