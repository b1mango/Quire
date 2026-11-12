from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from quire.assemble.epub import EpubChapter, EpubWriter
from quire.reader.epub import Epub


def _book(path: Path, *, nav: bool = True) -> None:
    manifest = '<item id="chapter" href="text/one.xhtml" media-type="application/xhtml+xml"/>'
    manifest += '<item id="image" href="images/a.png" media-type="image/png"/>'
    manifest += (
        '<item id="toc" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        if nav
        else '<item id="toc" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
    )
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "META-INF/container.xml",
            '<container><rootfile full-path="OEBPS/content.opf"/></container>',
        )
        archive.writestr(
            "OEBPS/content.opf",
            f'<package><manifest>{manifest}</manifest><spine><itemref idref="chapter"/></spine></package>',
        )
        archive.writestr(
            "OEBPS/text/one.xhtml",
            '<html><head><title>忽略</title></head><body><h1>章节</h1><script>坏内容</script><p style="color:red" onclick="x()">正文 &amp; 内容</p><a href="javascript:alert(1)">坏链接</a><a href="#ok">锚点</a><img src="../images/a.png" onerror="x()" alt="图"/><img src="https://example.com/a.png"/></body></html>',
        )
        archive.writestr("OEBPS/images/a.png", b"png")
        if nav:
            archive.writestr(
                "OEBPS/nav.xhtml",
                '<html xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><a href="text/one.xhtml#top">目录章</a></nav></body></html>',
            )
        else:
            archive.writestr(
                "OEBPS/toc.ncx",
                '<ncx><navMap><navPoint><navLabel><text>旧目录章</text></navLabel><content src="text/one.xhtml"/></navPoint></navMap></ncx>',
            )


def test_epub_nav_and_sanitized_section(tmp_path: Path) -> None:
    path = tmp_path / "book.epub"
    _book(path)
    book = Epub(path)
    assert book.toc == [{"title": "目录章", "section": 0}]
    assert book.sections == 1
    html = str(book.section(0, "id")["html"])
    assert '<img src="/api/books/id/read/res/images/a.png?format=epub" alt="图">' in html
    assert '<a href="#ok">锚点</a>' in html
    assert "正文 &amp; 内容" in html
    for forbidden in (
        "script",
        "坏内容",
        "onclick",
        "onerror",
        "style",
        "javascript:",
        "example.com",
        "忽略",
    ):
        assert forbidden not in html
    assert book.resource("images/a.png") == (b"png", "image/png")
    with pytest.raises(ValueError):
        book.resource("%2e%2e/images/a.png")


def test_epub_ncx_fallback(tmp_path: Path) -> None:
    path = tmp_path / "book.epub"
    _book(path, nav=False)
    assert Epub(path).toc == [{"title": "旧目录章", "section": 0}]


def test_exported_epub_nav(tmp_path: Path) -> None:
    path = tmp_path / "export.epub"
    with EpubWriter(path, title="测试", cover=False) as writer:
        writer.add_chapter(EpubChapter(1, "第一章", ("内容",)))
    assert Epub(path).toc == [{"title": "第一章", "section": 0}]


def test_epub_rejects_zip_bomb_and_xml_entity(tmp_path: Path) -> None:
    path = tmp_path / "bomb.epub"
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("META-INF/container.xml", b"x" * 100000)
    with pytest.raises(ValueError):
        Epub(path)
    with ZipFile(path, "w") as archive:
        archive.writestr("META-INF/container.xml", '<!DOCTYPE x [<!ENTITY a "x">]><container/>')
    with pytest.raises(ValueError):
        Epub(path)


@pytest.mark.parametrize("kind", ["PUBLIC", "SYSTEM"])
def test_epub_accepts_external_doctype_without_entity(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "doctype.epub"
    declaration = (
        '<!DOCTYPE container PUBLIC "-//example//DTD test//EN" "https://invalid.example/test.dtd">'
        if kind == "PUBLIC"
        else '<!DOCTYPE container SYSTEM "https://invalid.example/test.dtd">'
    )
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "META-INF/container.xml",
            declaration + '<container><rootfile full-path="book.opf"/></container>',
        )
        archive.writestr("book.opf", "<package><manifest/><spine/></package>")
    assert Epub(path).sections == 0


@pytest.mark.parametrize("container", ["<container>", "<container/>"])
def test_epub_rejects_invalid_xml_and_structure(tmp_path: Path, container: str) -> None:
    path = tmp_path / "invalid.epub"
    with ZipFile(path, "w") as archive:
        archive.writestr("META-INF/container.xml", container)
    with pytest.raises(ValueError):
        Epub(path)


def test_epub_literal_percent_paths_and_open_stream(tmp_path: Path) -> None:
    path = tmp_path / "percent.epub"
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "META-INF/container.xml", '<container><rootfile full-path="book.opf"/></container>'
        )
        archive.writestr(
            "book.opf",
            '<package><manifest><item id="x" href="text%2520part.xhtml" media-type="application/xhtml+xml"/><item id="i" href="a%2520b.png" media-type="image/png"/></manifest><spine><itemref idref="x"/></spine></package>',
        )
        archive.writestr("text%20part.xhtml", '<html><body><img src="a%2520b.png"/></body></html>')
        archive.writestr("a%20b.png", b"png")
    with path.open("rb") as stream:
        book = Epub(stream)
        assert "res/a%2520b.png?format=epub" in book.section(0, "id")["html"]
        assert book.resource("a%20b.png") == (b"png", "image/png")
