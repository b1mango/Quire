"""Per-site page-completion scripts, evaluated inside the loaded page while rendering.

每次滚动迭代执行一次,返回真即认为页面完整(目录已物化/正文已注入),
不再等待可能永不收敛的子请求。书旗目录页物化虚拟目录;8book 目录页
物化跨域目录、章节页等待正文注入。
"""

from urllib.parse import urlsplit

from ..sites import eightbook

SHUQI_CATALOGUE = r"""(() => {
 const state = document.querySelector('#app')?.__vue__?.$store?.state?.chapter;
 const rows = state?.chapterList, book = state?.book;
 const expected = Number(book?.chapterNum);
 if (!Array.isArray(rows) || !expected || rows.length !== expected || state.isFetching) return false;
 const bookId = location.pathname.match(/^\/catalog\/(\d+)\/?$/)?.[1];
 if (!bookId || String(book.bookId) !== bookId || rows.length > 20000) return false;
 const ids = rows.map(r => String(r.chapterId));
 if (ids.some(id => !/^\d+$/.test(id)) || new Set(ids).size !== rows.length || rows.some(r => !r.chapterName)) return false;
 let container = document.querySelector('#quire-catalogue');
 if (!container) {
   container = document.createElement('section'); container.id = 'quire-catalogue';
   const title = document.createElement('h1'); title.textContent = book.bookName;
   container.append(title);
   rows.forEach((row, index) => {
     const a = document.createElement('a');
     a.href = '/reader/' + bookId + '?forceChapterIndex=' + index + '&forceChapterId=' + row.chapterId;
     a.textContent = row.chapterName; container.append(a);
   });
   document.body.prepend(container);
 }
 return true;
})()"""


def ready_script(url: str) -> str | None:
    import re

    parsed = urlsplit(url)
    if parsed.hostname == "t.shuqi.com" and re.fullmatch(r"/catalog/\d+/?", parsed.path):
        return SHUQI_CATALOGUE
    return eightbook.ready_script(url)
