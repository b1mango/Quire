"""站点页面清理:thepaperbooks 正文栏的链接列表噪声移除。"""

from __future__ import annotations

from quire.parse.article import extract_article, validate_article
from quire.parse.minidom import parse
from quire.sites.cleanup import clean_document

PROSE = "　　他們善用天賦、揮灑汗水，堅持、不認輸，讓比賽發光，也讓世界看見臺灣。" * 12

PAGE = f"""<html><body><main><div class="main_content"><div class="row">
<div class="col-lg-8">
  <h1 class="entry-title">測試文章標題</h1>
  <div class="entry-bottom firstBook"><p>{PROSE}</p></div>
  <div class="entry-bottom"><div class="entry-main-content"><ul>
    <li><a href="https://a.thepaperbooks.com/article/1">相關文章一</a></li>
    <li><a href="https://b.thepaperbooks.com/article/2">相關文章二</a></li>
  </ul></div></div>
  <div class="tags"><a href="/tag/1">標籤</a></div>
</div></div></div></main></body></html>"""

URL = "https://sport.thepaperbooks.com/read/265347/?13781380"


def test_cleanup_lets_article_pass_validation() -> None:
    doc = parse(PAGE, base_url=URL)
    clean_document(doc, URL)
    article = extract_article(doc)
    usable, code = validate_article(article)
    assert usable, code
    assert "善用天賦" in article.text
    assert "相關文章" not in article.text


def test_cleanup_leaves_unknown_sites_alone() -> None:
    doc = parse(PAGE, base_url="https://unknown.example.com/")
    clean_document(doc, "https://unknown.example.com/")
    assert doc.select(".entry-main-content")  # 未登记的站点不动
