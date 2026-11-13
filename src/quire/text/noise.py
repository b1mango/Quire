"""保守的小说噪声模板与网址片段识别。"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence

MAX_AD_LENGTH = 40
REPEATED_MIN_CHAPTERS = 5
REPEATED_CHAPTER_RATIO = 0.5
URL_REVIEW_RULE = "网址片段待复核"
QUOTES = frozenset("“”‘’「」『』\"'")
_WIDE_ASCII = str.maketrans({chr(i): chr(i - 0xFEE0) for i in range(0xFF01, 0xFF5F)})
#: 只认常见站点后缀：避免把 Mr.Li、J.K.Rowling、notes.txt 这类正文当成网址。
_WEB_TLDS = (
    "com|net|org|cn|cc|la|info|top|xyz|me|tw|hk|co|io|vip|club|site|biz|so|tv|in|us|online|"
    "wang|ink|pw|ws|win|fun|life|live|app|link"
)
_URL = re.compile(
    r"(?<![a-z0-9_.@-])(?:https?://)?"
    r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:" + _WEB_TLDS + r")(?![a-z0-9-])"
    r"(?::[0-9]{1,5})?(?:/[a-z0-9_~:/?#\[\]@!$&()*+,;=%.-]*)?",
    re.IGNORECASE,
)
#: 句中网址只有带协议或 www. 时才直接删除；裸域名留在正文里，进复核清单
_STRONG_URL = re.compile(r"^(?:https?://|www\.)", re.IGNORECASE)
_SITE_NOTICE = (
    r"(?:请)?记住本站(?:域名|网址)?"
    r"(?:\s*[:：]?\s*(?:https?://|www\.)[a-z0-9.-]+(?:/[a-z0-9/?=&_%#.-]*)?)?"
)
_PAGE_NOTICE = r"本章未完[，,；;]?\s*(?:请)?点击下一[页頁](?:继续阅读)?"
_LEGACY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("站点提示", re.compile(rf"^(?:{_SITE_NOTICE}|{_PAGE_NOTICE})[。.!！]?$", re.I)),
    (
        "求票推广",
        re.compile(r"^(?:(?:求(?:推荐票|月票|订阅|收藏|打赏)|投推荐票)[，,、\s]*)+[。.!！]?$"),
    ),
    ("符号行", re.compile(r"^[\W_]{1,12}$")),
    ("页码", re.compile(r"^第?\s*[（(]?\s*\d{1,3}\s*/\s*\d{1,3}\s*[）)]?\s*[页頁]$")),
    ("分隔线", re.compile(r"^[-—_=~*·#＝－]{3,200}[:：]?$")),
    (
        # 网盘 TXT/EPUB 的推广句：「更多精校小说尽在知轩藏书下载：…」「更多电子书请访问爱下电子书」。
        # 要同时出现书站字样，「更多的人请到后院去」这类正文不受影响
        "站点推广",
        re.compile(
            r"^(?:更多|最新|全本|免费|精校|好书|欢迎|本书|本站)[^。！？]{0,30}"
            r"(?:尽在|请访问|请到|请上|访问|下载|首发于)"
            r"(?=.*(?:电子书|小说|藏书|书城|书库|书屋|txt|网站|网址|中文网|书网|本站|https?://|www\.))"
            r"[^。！？]{0,80}$"
            r"|^本书由.{0,16}(?:藏书|书城|书库|小说|电子书|论坛|txt|网站|中文网).{0,16}(?:整理|收集|制作|提供)"
            r"[^。！？]{0,30}$",
            re.I,
        ),
    ),
    (
        "站点导航",
        re.compile(
            r"^(?:(?:上一[章页頁节節回话話]|下一[章页頁节節回话話]|章[节節]目[录錄]|目[录錄]|"
            r"返回目[录錄]|返回书页|加入书签|投推荐票|章节报错)[\s|｜/·]*){2,}$"
        ),
    ),
    ("站点提示", re.compile(r"^温馨提示[:：].{0,120}(?:回车|Enter|←|→|书签|翻页).{0,120}$", re.I)),
    (
        "版权声明",
        re.compile(
            r"^(?:Copyright\b|©|版权所有).{0,80}$|^.{0,60}All Rights Reserved\.?$"
            r"|^.{0,30}(?:本站所有小说为转载作品|所有章节均由网友上传).{0,100}$",
            re.I,
        ),
    ),
    ("页码", re.compile(r"^[\s\d０-９]{1,12}$")),
    (
        "站点导航",
        re.compile(
            r"^(?:回|返回|前往)?(?:目[录錄]|下一[回章節话頁页]|上一[回章節话頁页]|"
            r"下一[页頁]|上一[页頁]|章节目录|加入书签|收藏本站|顶部|底部|编辑|編輯|展開|收起)$"
        ),
    ),
)
_AD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "站点推广",
        re.compile(
            r"^(?:天才)?一秒记住本站(?:地址|域名|网址)?(?:[:：]\s*\S+)?[。.!！]?$"
            r"|^(?:最新章节|看最新章节)请到\s*(?:【(?:笔趣阁|顶点小说|[^】]{1,16}小说网)】|"
            r"(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,24}(?:/[a-z0-9_/?=&.%#-]*)?)"
            r"\s*(?:阅读)?[。.!！]?$"
            r"|^手机用户请(?:浏览|访问|到)\s*(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,24}"
            r"(?:/[a-z0-9_/?=&.%#-]*)?\s*阅读"
            r"(?:[，,]更优质的阅读体验)?[。.!！]?$"
            r"|^更多精彩(?:小说|TXT电子书)[，,]?(?:尽在|请访问)\s*"
            r"(?:[\u3400-\u9fff]{1,12}小说网\s*)?(?:https?://)?"
            r"(?:[a-z0-9-]+\.)+[a-z]{2,24}[。.!！]?$"
            r"|^【[\u3400-\u9fffA-Za-z0-9_-]{1,16}(?:中文网|小说网|阅读网|文学网)首发】$"
            r"|^(?:笔趣阁|顶点小说|[\u3400-\u9fff]{1,12}(?:小说网|中文网))"
            r"(?:首发|无弹窗)[。.!！]?$"
            r"|^本书由【[^】]{1,16}】整理[，,]版权归作者所有[，,]请勿用作商业用途[。.!！]?$"
            r"|^如果觉得本书不错[，,]请收藏本站[，,]加入书签[，,]方便下次阅读[。.!！]?$"
            r"|^百度搜索\s*[\u3400-\u9fffA-Za-z0-9_-]{1,15}\s*"
            r"(?:八一中文网|笔趣阁|顶点小说)\s*即可找到本书最新章节[。.!！]?$"
            r"|^[→>]+点击下载APP[，,]免费看全本[←<]+$",
            re.I,
        ),
    ),
    (
        "求票推广",
        re.compile(
            r"^(?:(?:求(?:推荐票|月票|订阅|收藏|打赏|推荐)|投推荐票|加入书签|收藏本站)"
            r"[，,、!！。\s]*)+$"
        ),
    ),
    (
        "章节提示",
        re.compile(
            r"^(?:[（(]?本章完[）)]?|本章未完[，,]?(?:请)?翻页|"
            r"本章未完[，,]?(?:请)?点击下一页继续阅读\s*>+\s*"
            r"(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,24})[。.!！]?$",
            re.I,
        ),
    ),
    ("站点标识", re.compile(r"^(?:笔趣阁|顶点小说|无弹窗)[。.!！]?$")),
    ("分隔线", re.compile(r"^[-—_=]{3,40}$")),
    (
        "社群推广",
        re.compile(
            r"^(?:加入|关注|添加|加)?(?:QQ群|书友群|公众号|微信)(?:号|群|号码)?"
            r"(?:[:：]\s*[a-z0-9_-]+)?[。.!！]?$",
            re.I,
        ),
    ),
)
_SITE_SIGNATURE = re.compile(
    r"^(?:【[\u3400-\u9fffA-Za-z0-9_-]{2,24}"
    r"(?:小说网|阅读网|文学网|中文网|书库|书城|书阁)】|"
    r"[\u3400-\u9fffA-Za-z0-9_-]{2,24}"
    r"(?:小说网|阅读网|文学网|中文网|书库|书城|书阁))(?:首发|版权所有)?$"
)
_PROSE_WORDS = re.compile(r"[我你他她它说走去来进从在是了把被让给望看想问叫着过]")


def has_quotes(line: str) -> bool:
    return any(char in QUOTES for char in line)


def _url_spans(line: str, *, strong_only: bool = False) -> tuple[tuple[int, int], ...]:
    folded = line.translate(_WIDE_ASCII)
    spans = []
    for match in _URL.finditer(folded):
        if strong_only and not _STRONG_URL.match(match.group(0)):
            continue
        end = match.end()
        while end > match.start() and folded[end - 1] in ".,!?:;)":
            end -= 1
        spans.append((match.start(), end))
    return tuple(spans)


def noise_rule(line: str) -> str | None:
    """只识别完整模板，不把引号或叙述句中的关键词当作整行广告。"""
    text = line.strip()
    if not text or has_quotes(text):
        return None
    spans = _url_spans(text)
    if len(spans) == 1 and spans[0] == (0, len(text)):
        return "网址行"
    for name, pattern in _LEGACY_PATTERNS:
        if pattern.fullmatch(text):
            return name
    if len(re.sub(r"\s+", "", text)) <= MAX_AD_LENGTH:
        folded = text.translate(_WIDE_ASCII)
        for name, pattern in _AD_PATTERNS:
            if pattern.fullmatch(folded):
                return name
    return None


def remove_url_fragments(line: str) -> tuple[str, int]:
    if has_quotes(line):
        return line, 0
    spans = _url_spans(line, strong_only=True)
    for start, end in reversed(spans):
        line = line[:start] + line[end:]
    if spans:
        line = re.sub(r"\s{2,}", " ", line).strip()
    return line, len(spans)


def has_url(line: str) -> bool:
    return bool(_url_spans(line))


def is_site_signature(line: str) -> bool:
    text = re.sub(r"\s+", "", line)
    return bool(
        len(text) <= MAX_AD_LENGTH
        and not has_quotes(text)
        and not _PROSE_WORDS.search(text)
        and _SITE_SIGNATURE.fullmatch(text)
    )


def repeated_site_signatures(chapters: Sequence[tuple[str, ...]], total_chapters: int) -> set[str]:
    counts: Counter[str] = Counter()
    for paragraphs in chapters:
        counts.update({line for line in paragraphs if is_site_signature(line)})
    return {
        line
        for line, count in counts.items()
        if count >= REPEATED_MIN_CHAPTERS and count >= total_chapters * REPEATED_CHAPTER_RATIO
    }
