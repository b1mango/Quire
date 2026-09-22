"""8book.com(无限小说网)跨域宿主适配。

实测(2026-09-22,真实 Chrome 153 + curl)确认的站点结构:

* 目录页 ``https://8book.com/novelbooks/<id>/`` 的章节链接指向伪装域
  ``//finance.binaccount.com/read/<id>/?<章节id>``(协议相对跨域链接,
  通用同源过滤会全部丢弃);
* 章节阅读页骨架(``/read/<id>/?<章节id>``)服务端不含正文(``#text`` 为空),
  正文由页内脚本按 ``/txt/4/<id>/<章节id><盐>.html`` 取回注入——盐取自页面
  内嵌章节 id 列表的末元素(按书派生);
* 伪装域是 Referer 门控:``Referer: https://8book.com/`` 给真实阅读页/正文,
  无 Referer 给「稅美人」税务博客诱饵(catch-all,任意路径同一份);
* 8book.com 与伪装域的静态资源对非浏览器 TLS 指纹硬封(curl 403),
  目录页必须走真实浏览器渲染;正文接口(curl 带 Referer 实测可达)。

适配分两层,全部只认实测确认的 URL 形态,不匹配就原样放行:

* ``catalogue_script``:渲染后的目录页 DOM 里把跨域章节链接重建为同源
  ``/read/<id>/?<章节id>`` 锚点(与书旗同一物化通道),绕开同源过滤;
  章节顺序按页面出现顺序(阅读顺序),不按 id 排序;
* ``allowed_hosts``:真实浏览器渲染 8book 页面时放行伪装域子请求
  (目录页可能加载该域资源;经同源章节地址渲染时正文 XHR 是同站的)。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from ..parse.minidom import Document

_HOST = "8book.com"
_CONTENT_HOST = "finance.binaccount.com"
_REFERER = "https://8book.com/"
_BOOK_PATH = re.compile(r"^/novelbooks/(\d+)/?$")

#: 目录物化脚本:把指向伪装域的章节链接重建为同源 /read/ 锚点容器。
#: 只收 ``/read/<id>/?<章节id>`` 形态且书号与当前页一致的链接;
#: 同一章节多次出现取首次,顺序保持页面阅读顺序。
CATALOGUE_SCRIPT = r"""(() => {
 const m = location.pathname.match(/^\/novelbooks\/(\d+)\/?$/);
 if (!m) return false;
 const bookId = m[1];
 const seen = new Map();
 document.querySelectorAll('a[href*="finance.binaccount.com/read/"]').forEach(a => {
   const href = a.href.match(/\/read\/(\d+)\/\?(\d+)/);
   if (!href || href[1] !== bookId) return;
   const title = (a.textContent || '').trim();
   if (!title || title.length > 60 || seen.has(href[2])) return;
   seen.set(href[2], title);
 });
 if (seen.size < 2) return false;
 let container = document.querySelector('#quire-catalogue');
 if (!container) {
   container = document.createElement('section'); container.id = 'quire-catalogue';
   document.body.prepend(container);
 }
 seen.forEach((title, chid) => {
   const a = document.createElement('a');
   a.href = '/read/' + bookId + '/?' + chid;
   a.textContent = title; container.append(a);
 });
 return true;
})()"""


def is_catalogue_page(url: str) -> bool:
    parts = urlsplit(url)
    return (parts.hostname or "").lower() == _HOST and _BOOK_PATH.match(parts.path) is not None


_CHAPTER_PATH = re.compile(r"^/read/\d+/$")

#: 章节就绪脚本:阅读页骨架不含正文,页内脚本异步取回注入 ``#text``;
#: 正文注入完成(或站点如实给出空章标记)即视为页面完整,不再等广告/
#: 长连接子请求——它们会让通用稳定等待永不收敛。
CHAPTER_READY_SCRIPT = r"""(() => {
 const t = document.querySelector('#text');
 if (!t) return false;
 return (t.innerText || '').trim().length > 30 || t.querySelector('.empty') !== null;
})()"""


def ready_script(url: str) -> str | None:
    """目录页返回物化脚本,章节页(含伪装域续页)返回就绪脚本;其他页面不干预。"""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host == _HOST and _BOOK_PATH.match(parts.path) is not None:
        return CATALOGUE_SCRIPT
    if (
        host in {_HOST, _CONTENT_HOST}
        and _CHAPTER_PATH.match(parts.path) is not None
        and parts.query
    ):
        return CHAPTER_READY_SCRIPT
    return None


def allowed_hosts(url: str) -> frozenset[str]:
    """真实浏览器渲染时放行的额外主机。

    8book.com 页面:``www.8book.com``(阅读页 jQuery/bootstrap 来源,缺了它
    页内取正文的脚本根本不会执行)与 ``finance.binaccount.com``(正文伪装域)。
    伪装域阅读页(分页续页会被直接导航到这里):脚本反指 8book.com/www,
    缺一页内脚本同样不执行。
    """
    host = (urlsplit(url).hostname or "").lower()
    if host == _HOST:
        return frozenset({_CONTENT_HOST, "www.8book.com"})
    if host == _CONTENT_HOST and _CHAPTER_PATH.match(urlsplit(url).path) is not None:
        return frozenset({_HOST, "www.8book.com"})
    return frozenset()


def is_content_mirror(source_url: str, final_url: str) -> bool:
    """章节页会 301 到伪装域(同路径,查询允许带 ``_N`` 分页后缀),视为同一内容。

    只认 ``8book.com/read/<id>/?<章节id>`` → ``finance.binaccount.com`` 同路径
    这一种实测形态;其他跨域跳转仍按"跳转到其他站点"如实拒绝。
    """
    source, target = urlsplit(source_url), urlsplit(final_url)
    if (source.hostname or "").lower() != _HOST:
        return False
    if (target.hostname or "").lower() != _CONTENT_HOST:
        return False
    if _CHAPTER_PATH.match(source.path) is None or source.path != target.path:
        return False
    if source.query == target.query:
        return True
    paged = re.fullmatch(r"(\d+)_\d+", target.query)
    return paged is not None and paged.group(1) == source.query


def navigation_referer(url: str) -> str | None:
    """伪装域阅读页的 Referer 门控:直接导航(无来源)会拿到税务博客诱饵页,
    必须带 ``Referer: https://8book.com/``(2026-09-22 实测:续页渲染直航
    伪装域拿到诱饵并被静默拼接进正文)。
    """
    parts = urlsplit(url)
    if (parts.hostname or "").lower() == _CONTENT_HOST and _CHAPTER_PATH.match(parts.path):
        return _REFERER
    return None


def decoy_marker(url: str, html: str) -> str | None:
    """伪装域阅读页拿到的是诱饵页时返回原因;否则 None。

    诱饵是 WordPress 税务博客(catch-all),没有阅读页的 ``#text`` 正文容器,
    标题/正文带「稅美人」标记。宁可误报也不把诱饵当正文。
    """
    parts = urlsplit(url)
    if (parts.hostname or "").lower() != _CONTENT_HOST or _CHAPTER_PATH.match(parts.path) is None:
        return None
    if 'id="text"' not in html or "稅美人" in html or "稅務行事曆" in html:
        return "正文伪装域返回了诱饵页(可能缺少 Referer 或页面已改版)"
    return None


#: 阅读页里的噪声块:导航(章節列表/上一篇/下一篇,在内容容器内重复出现)、
#: 与章节标题重复的 #subtitle、顶栏日期装饰(续页抽取会误入正文)。
_NAV_SELECTORS = (".prev", ".next", ".chmenus", "#subtitle", ".topbar-date")

#: 站点水印是同形异符拼出的 "8book.com" 变体(⒏ьoОｋ·Ｃом / 8ВｏΟk·СΟm …),
#: 每次出现字符组合都不同,且与正文共用 span.read_spans,只能按文本形态识别:
#: 短、无汉字、带 8 系字符与分隔符。中文小说正文段不会满足这个形态。
_WATERMARK = re.compile(r"^[^\u4e00-\u9fff]{5,20}$")
_WATERMARK_MARKS = frozenset("8⑧⒏⑻８")
_WATERMARK_SEPS = frozenset("·.．。｡•・")


def clean_document(doc: Document) -> None:
    """移除 8book 阅读页的导航块与同形异符水印(在正文抽取前调用)。"""
    for selector in _NAV_SELECTORS:
        for node in doc.select(selector):
            if node.parent is not None:
                node.parent.children.remove(node)
    for node in doc.select("span.read_spans"):
        text = node.text.strip()
        if (
            node.parent is not None
            and _WATERMARK.match(text)
            and any(c in _WATERMARK_MARKS for c in text)
            and any(c in _WATERMARK_SEPS for c in text)
        ):
            node.parent.children.remove(node)
