"""正文抽取与质量校验：纯函数，无 I/O（项目设计.md §6.6、§37.3）。

小说和漫画的失败方式不同：漫画缺一张图看得见，小说抽错一整页
（把目录当正文、把推荐位当章节）只有读的时候才发现。所以这里分两步——
先按"文本量 × 无链接程度 × 段落数"挑容器，再用五个判据判断这段文字
到底像不像正文；不通过时给出可操作的原因，由上层决定是跟随下一页
还是把这一章记为失败。

评分用一次后序遍历聚合每个容器的字符数、链接内字符数和段落数（O(n)），
不做"每个候选容器各自遍历一遍子树"的写法，避免大页面上退化成平方级。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..errors import ParseError
from ..text.clean import (
    cjk_ratio,
    clean_paragraphs,
    has_chapter_mark,
    punctuation_density,
    repeated_line_ratio,
    strip_site_suffix,
)
from .minidom import Document, Node

#: 抽取前整棵丢弃的噪声容器：脚本、样式、导航、页脚、评论、表单。
DROP_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "nav",
        "footer",
        "header",
        "aside",
        "form",
        "iframe",
        "button",
        "select",
        "textarea",
        "svg",
        "canvas",
    }
)

#: 这些 class 的元素是站点模板而不是正文：MediaWiki 的编辑链接、导航盒、
#: 隐藏元素等。按 class 丢弃比按标签丢更准——同样的 <div> 里可能是正文。
DROP_CLASSES = frozenset(
    {
        "noprint",
        "no-print",
        "mw-editsection",
        "editsection",
        "printfooter",
        "catlinks",
        "navbox",
        "hidden",
        "screen-reader-text",
        "visually-hidden",
        "advertisement",
        "ads",
        "licensetpl",
        "license",
        "licence",
        "copyright",
    }
)

#: 会产生段落边界的块级标签。
BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "br",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "pre",
        "section",
        "article",
        "table",
        "tr",
        "td",
        "th",
        "dd",
        "dt",
        "figure",
        "figcaption",
        "hr",
    }
)

#: 参与评分的容器标签。
CONTAINER_TAGS = frozenset({"article", "main", "div", "section", "td", "body"})

#: 明确的正文标记优先于页面级 article/main，完整保留其子树。
CONTENT_NAMES = frozenset(
    {
        "content",
        "chapter-content",
        "chapter_content",
        "read-content",
        "read_content",
        "muye-reader-content",  # 番茄小说网页版正文容器(外层的字数/更新时间不算正文)
    }
)
_COMMENT_NAME = re.compile(r"(?:^|[\s_-])(?:comments?|replies|reply)(?:$|[\s_-])", re.I)

#: 少于这么多字符的容器不参与评分——否则每个小 div 都是"候选正文"。
MIN_ARTICLE_CHARS = 200

#: 校验失败的原因码 → 给用户看的说明。
VALIDATION_MESSAGES = {
    "too_short": "正文过短，可能是分页的第一页或选择器选错了",
    "not_cjk_prose": "文本不像中文散文，可能是评论区、免责声明或英文页面",
    "looks_like_index_page": "链接占比过高，看起来是目录页而不是正文",
    "looks_like_word_soup": "几乎没有标点，可能被混淆或抽取到了乱码",
    "template_repeat": "同一行反复出现，像是模板页",
    "error_page": "页面只有错误、加载或验证提示，未取得章节正文",
}

_WS = re.compile(r"\s+")
_ERROR_NOTICE = re.compile(
    r"(?:抱歉[，,！!]?\s*)?"
    r"(?:(?:页面|章节|内容|资源)(?:不存在|未找到|已删除|加载失败)|"
    r"(?:请求|访问|加载|网络|服务器)(?:失败|错误|异常|超时)|"
    r"正在加载|请稍候|请稍后重试|请先登录|访问受限|验证失败|"
    r"请(?:完成|通过)(?:安全|人机|验证码)验证|"
    r"(?:error\s*)?(?:403|404|500|502|503)|not found|access denied)"
    r"(?:[，,。.!！:：；;]\s*(?:请)?(?:稍后重试|刷新(?:页面)?|稍候|稍等|登录|重试))*"
    r"[。.!！]?",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Article:
    """一章（可能由多页拼接）的正文。``paragraphs`` 只含未清洗的原始段落。"""

    title: str
    paragraphs: tuple[str, ...]
    source_url: str = ""
    link_density: float = 0.0
    score: float = 0.0

    @property
    def text(self) -> str:
        return "\n".join(self.paragraphs)

    @property
    def char_count(self) -> int:
        return len(_WS.sub("", self.text))


@dataclass(frozen=True, slots=True)
class Measure:
    """一个容器子树的三项聚合指标。"""

    chars: int
    linked: int
    blocks: int

    @property
    def density(self) -> float:
        return self.linked / self.chars if self.chars else 0.0

    def score(self, *, min_chars: int = MIN_ARTICLE_CHARS) -> float:
        if self.chars < min_chars:
            return 0.0
        return self.chars * (1.0 - self.density) * (1.0 + min(self.blocks, 30) / 30.0)


def dropped(node: Node) -> bool:
    """这个元素是否属于要整棵丢掉的站点模板。"""
    return (
        node.tag in DROP_TAGS
        or bool(node.classes & DROP_CLASSES)
        or _COMMENT_NAME.search(node.attrs.get("class", "")) is not None
        or _COMMENT_NAME.search(node.attrs.get("id", "")) is not None
    )


def measure(doc: Document) -> dict[int, Measure]:
    """一次后序遍历算出每个元素的字符数/链接字符数/段落数。"""
    stats: dict[int, Measure] = {}
    stack: list[tuple[Node, bool]] = [(doc, False)]
    while stack:
        node, visited = stack.pop()
        if node.is_text or dropped(node):
            continue
        if not visited:
            stack.append((node, True))
            stack.extend((child, False) for child in node.children)
            continue
        chars = linked = blocks = 0
        for child in node.children:
            if child.is_text:
                chars += len(_WS.sub("", child.data))
                continue
            found = stats.get(id(child))
            if found is not None:
                chars += found.chars
                linked += found.linked
                blocks += found.blocks
        if node.tag == "a":
            linked = chars
        if node.tag in BLOCK_TAGS and node.own_text:
            blocks += 1
        stats[id(node)] = Measure(chars, linked, blocks)
    return stats


def rank_containers(
    doc: Document, stats: dict[int, Measure] | None = None, *, min_chars: int = MIN_ARTICLE_CHARS
) -> list[tuple[float, Node]]:
    """按得分从高到低列出候选正文容器（``--explain`` 与诊断用）。"""
    measured = stats if stats is not None else measure(doc)
    ranked: list[tuple[float, Node]] = []
    for node in doc.iter_elements():
        if node.tag not in CONTAINER_TAGS:
            continue
        found = measured.get(id(node))
        if found is None:
            continue
        value = found.score(min_chars=min_chars)
        if value > 0:
            ranked.append((value, node))
    ranked.sort(key=lambda item: (-item[0], -_depth(item[1])))
    return ranked


def best_container(
    doc: Document, stats: dict[int, Measure] | None = None, *, min_chars: int = MIN_ARTICLE_CHARS
) -> Node | None:
    """优先可信正文边界；无有效语义边界时按原始文本评分选择。"""
    measured = stats if stats is not None else measure(doc)
    ranked = rank_containers(doc, measured, min_chars=min_chars)
    scopes: list[tuple[int, Node]] = []
    for node in doc.iter_elements():
        found = measured.get(id(node))
        if node.tag not in CONTAINER_TAGS or found is None or not found.chars:
            continue
        names = node.classes | {node.attrs.get("id", "")}
        priority = 3 if names & CONTENT_NAMES else {"article": 2, "main": 1}.get(node.tag, 0)
        if priority:
            scopes.append((priority, node))
    if scopes:
        priority = max(level for level, _ in scopes)
        nodes = [node for level, node in scopes if level == priority]
        boundary = nodes[0]
        # 同级正文标记可能分布于多个兄弟节点；取共同边界，保留短兄弟和尾段。
        for node in nodes[1:]:
            ancestors: set[Node] = set()
            current: Node | None = node
            while current is not None:
                ancestors.add(current)
                current = current.parent
            while boundary not in ancestors and boundary.parent is not None:
                boundary = boundary.parent
        if boundary.tag != "#document" and measured[id(boundary)].score(min_chars=min_chars) > 0:
            return boundary
    return ranked[0][1] if ranked else None


def container_stats(doc: Document, node: Node) -> Measure:
    measured = measure(doc)
    return measured.get(id(node), Measure(0, 0, 0))


def link_density(node: Node) -> float:
    """链接内文字占容器文字的比例。目录页高，正文页低。"""
    chars = linked = 0
    stack: list[tuple[Node, bool]] = [(node, node.tag == "a")]
    while stack:
        current, in_link = stack.pop()
        if current.is_text:
            size = len(_WS.sub("", current.data))
            chars += size
            linked += size if in_link else 0
            continue
        if current.tag == "#document" or (current is not node and dropped(current)):
            continue
        nested = in_link or current.tag == "a"
        stack.extend((child, nested) for child in current.children)
    return linked / chars if chars else 0.0


def extract_paragraphs(node: Node) -> tuple[str, ...]:
    """按块级边界把子树切成段落；嵌套块不会重复计数。"""
    out: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        text = _WS.sub(" ", "".join(buffer)).strip()
        buffer.clear()
        if text:
            out.append(text)

    stack: list[tuple[Node, bool]] = [(node, False)]
    while stack:
        current, closing = stack.pop()
        if closing:
            if current.tag in BLOCK_TAGS:
                flush()
            continue
        if current.is_text:
            buffer.append(current.data)
            continue
        if current.tag == "#document" or dropped(current):
            continue
        if current.tag in BLOCK_TAGS:
            flush()
        stack.append((current, True))
        stack.extend((child, False) for child in reversed(current.children))
    flush()
    return tuple(out)


def page_title(doc: Document, fallback: str = "") -> str:
    """取标题：``<h1>`` 优先于 ``<title>``，并去掉站点后缀。"""
    node = doc.select_one("h1") or doc.find("title")
    text = node.text if node is not None else ""
    cleaned = strip_site_suffix(text) if text else ""
    return cleaned or fallback


def extract_article(
    doc: Document,
    base_url: str = "",
    *,
    selector: str | None = None,
    title: str = "",
    continuation: bool = False,
) -> Article:
    """选择器必须命中；续页仅在标题可辨认为章节时允许短容器参与评分。"""
    resolved_title = title or page_title(doc)
    min_chars = 1 if continuation and has_chapter_mark(resolved_title) else MIN_ARTICLE_CHARS
    measured = measure(doc)
    if selector:
        node = doc.select_one(selector)
        if node is None:
            raise ParseError(
                f"正文选择器没有命中：{selector}",
                hint="用 `quire inspect <url> --selector <css>` 先确认选择器能选到节点。",
            )
    else:
        node = best_container(doc, measured, min_chars=min_chars)
    if node is None:
        return Article(title=resolved_title, paragraphs=(), source_url=base_url)
    stats = measured.get(id(node), Measure(0, 0, 0))
    return Article(
        title=resolved_title,
        paragraphs=extract_paragraphs(node),
        source_url=base_url,
        link_density=stats.density,
        score=stats.score(min_chars=min_chars),
    )


def validate_article(article: Article, *, continuation: bool = False) -> tuple[bool, str]:
    """续页只放宽有章节依据的短正文长度，其余判据及错误页检查仍然生效。"""
    text = article.text.strip()
    size = article.char_count
    if size < MIN_ARTICLE_CHARS:
        if not continuation or not has_chapter_mark(article.title):
            return False, "too_short"
        text = "\n".join(clean_paragraphs(article.paragraphs, title=article.title))
        size = len(_WS.sub("", text))
        if any(_ERROR_NOTICE.fullmatch(line.strip()) for line in text.splitlines()):
            return False, "error_page"
    if not size:
        return False, "too_short"
    if cjk_ratio(text) < 0.3 and size < 800:
        return False, "not_cjk_prose"
    if article.link_density > 0.25:
        return False, "looks_like_index_page"
    if punctuation_density(text) < 0.01:
        return False, "looks_like_word_soup"
    if repeated_line_ratio(article.paragraphs) > 0.5:
        return False, "template_repeat"
    return True, "ok"


def _depth(node: Node) -> int:
    depth = 0
    current = node.parent
    while current is not None:
        depth += 1
        current = current.parent
    return depth
