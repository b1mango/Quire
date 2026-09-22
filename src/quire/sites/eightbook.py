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

_HOST = "8book.com"
_CONTENT_HOST = "finance.binaccount.com"
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
    """目录页返回物化脚本,章节页返回就绪脚本;其他页面不干预。"""
    parts = urlsplit(url)
    if (parts.hostname or "").lower() != _HOST:
        return None
    if _BOOK_PATH.match(parts.path) is not None:
        return CATALOGUE_SCRIPT
    if _CHAPTER_PATH.match(parts.path) is not None and parts.query:
        return CHAPTER_READY_SCRIPT
    return None


def allowed_hosts(url: str) -> frozenset[str]:
    """真实浏览器渲染 8book 页面时放行的额外主机。

    ``www.8book.com``:阅读页的 jQuery/bootstrap 从该域加载,缺了它页内
    取正文的脚本根本不会执行;``finance.binaccount.com``:正文伪装域。
    """
    if (urlsplit(url).hostname or "").lower() == _HOST:
        return frozenset({_CONTENT_HOST, "www.8book.com"})
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
