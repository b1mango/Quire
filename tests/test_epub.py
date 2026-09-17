"""EPUB 3 写入器的结构、转义、确定性与失败契约测试（项目设计.md §37.6、§37.7）。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest

from quire.assemble.epub import EpubChapter, EpubWriter

TITLE = '卷帙 <Title>&"</Title><Web>injected</Web> ../../outside'
URL = "https://example.test/book?token=private-query#fragment"
CONTROL = "\x07"
NASTY_TITLE = f'第一章 <Title>&"</Title> \U0001f600 {CONTROL}'
NASTY_PARAGRAPH = f'<p class="x">&amp; & < > " \' \U0001f600 中文 {CONTROL} 结束'

CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
XHTML_NS = "http://www.w3.org/1999/xhtml"
EPUB_NS = "http://www.idpf.org/2007/ops"
NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"

FIXED_FILES = (
    "META-INF/container.xml",
    "OEBPS/content.opf",
    "OEBPS/nav.xhtml",
    "OEBPS/toc.ncx",
    "OEBPS/style.css",
)


def writer(path: Path, *, title: str = TITLE, source_url: str = URL, **values: str) -> EpubWriter:
    return EpubWriter(path, title=title, source_url=source_url, **values)


def chapter_xhtml(path: Path, index: int) -> ET.Element:
    with ZipFile(path) as result:
        return ET.fromstring(result.read(f"OEBPS/text/chapter-{index:04d}.xhtml"))


def xml_part(path: Path, name: str) -> ET.Element:
    with ZipFile(path) as result:
        return ET.fromstring(result.read(name))


def identifier_of(path: Path) -> str:
    opf = xml_part(path, "OEBPS/content.opf")
    value = opf.findtext(f"{{{OPF_NS}}}metadata/{{{DC_NS}}}identifier")
    assert value is not None
    return value


def manifest_of(opf: ET.Element) -> dict[str, ET.Element]:
    items = opf.findall(f"{{{OPF_NS}}}manifest/{{{OPF_NS}}}item")
    return {item.get("id", ""): item for item in items}


def spine_hrefs(opf: ET.Element) -> list[str]:
    manifest = manifest_of(opf)
    refs = opf.findall(f"{{{OPF_NS}}}spine/{{{OPF_NS}}}itemref")
    return [manifest[ref.get("idref", "")].get("href", "") for ref in refs]


def test_structure_manifest_spine_and_navigation(tmp_path: Path) -> None:
    path = tmp_path / "temporary"
    chapters = [
        EpubChapter(1, "第一章 启程", ("正文甲。", "正文乙。")),
        EpubChapter(2, "第二章 归途", ("正文丙。",)),
    ]
    with writer(path) as output:
        for chapter in chapters:
            assert output.add_chapter(chapter) is None
    with ZipFile(path) as result:
        assert result.testzip() is None
        infos = result.infolist()
        assert infos[0].filename == "mimetype"
        assert infos[0].compress_type == ZIP_STORED
        assert result.read("mimetype") == b"application/epub+zip"
        names = result.namelist()
        for name in (
            *FIXED_FILES,
            "OEBPS/text/chapter-0001.xhtml",
            "OEBPS/text/chapter-0002.xhtml",
        ):
            assert name in names
        assert "OEBPS/text/chapter-0003.xhtml" not in names
        for info in infos:
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.create_system == 3
            assert info.external_attr == 0o100600 << 16
        for name in names[1:]:
            assert result.getinfo(name).compress_type == ZIP_DEFLATED
        for name in names:
            if name.endswith((".xml", ".opf", ".xhtml", ".ncx")):
                ET.fromstring(result.read(name))

        container = ET.fromstring(result.read("META-INF/container.xml"))
        rootfile = container.find(f"{{{CONTAINER_NS}}}rootfiles/{{{CONTAINER_NS}}}rootfile")
        assert rootfile is not None
        assert rootfile.get("full-path") == "OEBPS/content.opf"
        assert rootfile.get("media-type") == "application/oebps-package+xml"

        opf = ET.fromstring(result.read("OEBPS/content.opf"))
        assert opf.get("version") == "3.0"
        assert opf.get("unique-identifier") == "pub-id"
        spine = opf.find(f"{{{OPF_NS}}}spine")
        assert spine is not None
        assert spine.get("toc") == "ncx"
        manifest = manifest_of(opf)
        assert set(manifest) == {"nav", "ncx", "style", "chapter-0001", "chapter-0002"}
        for item in manifest.values():
            assert f"OEBPS/{item.get('href')}" in names
            assert item.get("media-type")
        assert manifest["nav"].get("properties") == "nav"
        assert manifest["nav"].get("media-type") == "application/xhtml+xml"
        assert manifest["ncx"].get("media-type") == "application/x-dtbncx+xml"
        assert manifest["style"].get("media-type") == "text/css"
        expected = ["text/chapter-0001.xhtml", "text/chapter-0002.xhtml"]
        assert spine_hrefs(opf) == expected

        nav = ET.fromstring(result.read("OEBPS/nav.xhtml"))
        toc = nav.find(f".//{{{XHTML_NS}}}nav")
        assert toc is not None
        assert toc.get(f"{{{EPUB_NS}}}type") == "toc"
        links = toc.findall(f"{{{XHTML_NS}}}ol/{{{XHTML_NS}}}li/{{{XHTML_NS}}}a")
        assert [link.get("href") for link in links] == expected
        assert [link.text for link in links] == ["第一章 启程", "第二章 归途"]

        ncx = ET.fromstring(result.read("OEBPS/toc.ncx"))
        points = ncx.findall(f"{{{NCX_NS}}}navMap/{{{NCX_NS}}}navPoint")
        assert [point.get("playOrder") for point in points] == ["1", "2"]
        contents = [point.find(f"{{{NCX_NS}}}content") for point in points]
        assert [node is not None for node in contents] == [True, True]
        assert [node.get("src") for node in contents if node is not None] == expected
        labels = [point.findtext(f"{{{NCX_NS}}}navLabel/{{{NCX_NS}}}text") for point in points]
        assert labels == ["第一章 启程", "第二章 归途"]
        assert ncx.findtext(f"{{{NCX_NS}}}docTitle/{{{NCX_NS}}}text") == TITLE

        body = ET.fromstring(result.read("OEBPS/text/chapter-0001.xhtml"))
        assert [node.text for node in body.findall(f"{{{XHTML_NS}}}body/{{{XHTML_NS}}}p")] == [
            "正文甲。",
            "正文乙。",
        ]
        assert result.read("OEBPS/style.css").startswith(b"html {")


def test_book_without_chapters_is_still_valid(tmp_path: Path) -> None:
    path = tmp_path / "temporary"
    with writer(path):
        pass
    with ZipFile(path) as result:
        assert result.testzip() is None
        assert result.namelist() == ["mimetype", *FIXED_FILES]
        opf = ET.fromstring(result.read("OEBPS/content.opf"))
        assert spine_hrefs(opf) == []
        nav = ET.fromstring(result.read("OEBPS/nav.xhtml"))
        assert nav.findall(f".//{{{XHTML_NS}}}nav/{{{XHTML_NS}}}ol/{{{XHTML_NS}}}li") == []
        ncx = ET.fromstring(result.read("OEBPS/toc.ncx"))
        assert ncx.findall(f"{{{NCX_NS}}}navMap/{{{NCX_NS}}}navPoint") == []


def test_metadata_identifier_source_and_fixed_timestamp(tmp_path: Path) -> None:
    path = tmp_path / "temporary"
    with writer(path, language="en", identifier="urn:isbn:9780000000000") as output:
        output.add_chapter(EpubChapter(1, "One", ("body",)))
    opf = xml_part(path, "OEBPS/content.opf")
    metadata = opf.find(f"{{{OPF_NS}}}metadata")
    assert metadata is not None
    assert metadata.findtext(f"{{{DC_NS}}}identifier") == "urn:isbn:9780000000000"
    assert metadata.findtext(f"{{{DC_NS}}}title") == TITLE
    assert metadata.findtext(f"{{{DC_NS}}}language") == "en"
    assert metadata.findtext(f"{{{DC_NS}}}source") == URL
    assert metadata.findtext(f"{{{OPF_NS}}}meta") == "1980-01-01T00:00:00Z"
    modified = metadata.find(f"{{{OPF_NS}}}meta")
    assert modified is not None
    assert modified.get("property") == "dcterms:modified"
    assert opf.get("unique-identifier") == "pub-id"
    assert opf.get("{http://www.w3.org/XML/1998/namespace}lang") == "en"

    plain = tmp_path / "plain"
    with writer(plain, source_url="") as output:
        output.add_chapter(EpubChapter(1, "第一章", ("正文。",)))
    plain_opf = xml_part(plain, "OEBPS/content.opf")
    assert plain_opf.find(f"{{{OPF_NS}}}metadata/{{{DC_NS}}}source") is None
    assert plain_opf.findtext(f"{{{OPF_NS}}}metadata/{{{DC_NS}}}language") == "zh"


def test_generated_identifier_is_deterministic_urn(tmp_path: Path) -> None:
    paths = [tmp_path / "first", tmp_path / "second"]
    for path in paths:
        with writer(path) as output:
            output.add_chapter(EpubChapter(1, "第一章", ("正文。",)))
    digest = hashlib.sha256(f"{TITLE}\n{URL}".encode()).hexdigest()[:40]
    expected = f"urn:quire:{digest}"
    assert identifier_of(paths[0]) == identifier_of(paths[1]) == expected
    assert len(digest) == 40
    assert all(char in "0123456789abcdef" for char in digest)

    other = tmp_path / "other"
    with writer(other, title="另一本书") as output:
        output.add_chapter(EpubChapter(1, "第一章", ("正文。",)))
    assert identifier_of(other) != expected


def test_escaping_and_control_characters(tmp_path: Path) -> None:
    path = tmp_path / "temporary"
    chapter = EpubChapter(1, NASTY_TITLE, (NASTY_PARAGRAPH,), URL + CONTROL)
    with writer(path, title=TITLE + CONTROL, source_url=URL + CONTROL) as output:
        output.add_chapter(chapter)
    with ZipFile(path) as result:
        for name in result.namelist():
            assert b"\x07" not in result.read(name)
        raw = result.read("OEBPS/text/chapter-0001.xhtml")
        root = ET.fromstring(raw)
        assert root.tag == f"{{{XHTML_NS}}}html"
        assert [node.tag for node in root.iter()] == [
            f"{{{XHTML_NS}}}html",
            f"{{{XHTML_NS}}}head",
            f"{{{XHTML_NS}}}meta",
            f"{{{XHTML_NS}}}title",
            f"{{{XHTML_NS}}}link",
            f"{{{XHTML_NS}}}body",
            f"{{{XHTML_NS}}}h1",
            f"{{{XHTML_NS}}}p",
        ]
        clean_title = NASTY_TITLE.replace(CONTROL, "")
        assert root.findtext(f"{{{XHTML_NS}}}body/{{{XHTML_NS}}}h1") == clean_title
        assert (
            root.findtext(f"{{{XHTML_NS}}}head/{{{XHTML_NS}}}title") == f"{TITLE} - {clean_title}"
        )
        paragraphs = root.findall(f"{{{XHTML_NS}}}body/{{{XHTML_NS}}}p")
        assert [node.text for node in paragraphs] == [NASTY_PARAGRAPH.replace(CONTROL, "")]
        # 正文里的标签被当文本，不会变成元素。
        assert root.find(".//Title") is None

        opf = ET.fromstring(result.read("OEBPS/content.opf"))
        assert opf.findtext(f"{{{OPF_NS}}}metadata/{{{DC_NS}}}title") == TITLE
        assert opf.findtext(f"{{{OPF_NS}}}metadata/{{{DC_NS}}}source") == URL
        nav = ET.fromstring(result.read("OEBPS/nav.xhtml"))
        assert nav.findtext(f".//{{{XHTML_NS}}}a") == clean_title


def test_missing_and_empty_chapters_keep_their_positions(tmp_path: Path) -> None:
    path = tmp_path / "temporary"
    chapters = [
        EpubChapter(1, "第一章", ("正文。",)),
        EpubChapter(2, "", (), missing_reason='缺章 <timeout> & "重试"'),
        EpubChapter(3, "第三章", ()),
    ]
    with writer(path) as output:
        for chapter in chapters:
            output.add_chapter(chapter)
    missing = chapter_xhtml(path, 2)
    assert missing.findtext(f"{{{XHTML_NS}}}body/{{{XHTML_NS}}}h1") == "第 2 章"
    notes = missing.findall(f"{{{XHTML_NS}}}body/{{{XHTML_NS}}}p")
    assert [note.get("class") for note in notes] == ["missing"]
    assert notes[0].text == '本章抓取失败：缺章 <timeout> & "重试"'
    assert missing.findall(f".//{{{XHTML_NS}}}p[@class='placeholder']") == []

    empty = chapter_xhtml(path, 3)
    assert empty.findtext(f"{{{XHTML_NS}}}body/{{{XHTML_NS}}}h1") == "第三章"
    placeholders = empty.findall(f"{{{XHTML_NS}}}body/{{{XHTML_NS}}}p")
    assert [node.get("class") for node in placeholders] == ["placeholder"]
    assert placeholders[0].text == "（本章无正文）"
    assert empty.findall(f".//{{{XHTML_NS}}}p[@class='missing']") == []

    # 缺章 / 空章不改写位置：spine、nav、ncx 仍是 1、2、3。
    expected = [f"text/chapter-{index:04d}.xhtml" for index in (1, 2, 3)]
    opf = xml_part(path, "OEBPS/content.opf")
    assert spine_hrefs(opf) == expected
    nav = xml_part(path, "OEBPS/nav.xhtml")
    links = nav.findall(f".//{{{XHTML_NS}}}nav/{{{XHTML_NS}}}ol/{{{XHTML_NS}}}li/{{{XHTML_NS}}}a")
    assert [link.get("href") for link in links] == expected
    assert [link.text for link in links] == ["第一章", "第 2 章", "第三章"]
    ncx = xml_part(path, "OEBPS/toc.ncx")
    points = ncx.findall(f"{{{NCX_NS}}}navMap/{{{NCX_NS}}}navPoint")
    assert [point.get("playOrder") for point in points] == ["1", "2", "3"]


def test_identical_input_produces_identical_bytes(tmp_path: Path) -> None:
    paths = [tmp_path / "first", tmp_path / "second"]

    def build(path: Path) -> None:
        with writer(path) as output:
            output.add_chapter(EpubChapter(1, NASTY_TITLE, (NASTY_PARAGRAPH,), URL))
            output.add_chapter(EpubChapter(2, "", (), URL, missing_reason="抓取失败"))
            output.add_chapter(EpubChapter(3, "第三章", ()))

    for path in paths:
        build(path)
    assert paths[0].read_bytes() == paths[1].read_bytes()
    first = paths[0].read_bytes()
    with pytest.raises(FileExistsError):
        build(paths[0])
    assert paths[0].read_bytes() == first


def test_context_lifecycle_errors(tmp_path: Path) -> None:
    path = tmp_path / "temporary"
    output = writer(path)
    assert not path.exists()
    chapter = EpubChapter(1, "第一章", ("正文。",))
    with pytest.raises(RuntimeError, match="not open"):
        output.add_chapter(chapter)
    with output:
        with pytest.raises(RuntimeError, match="reused"):
            output.__enter__()
        output.add_chapter(chapter)
    with pytest.raises(RuntimeError, match="not open"):
        output.add_chapter(EpubChapter(2, "第二章", ("正文。",)))
    with pytest.raises(RuntimeError, match="reused"):
        output.__enter__()
    output.__exit__(None, None, None)


@pytest.mark.parametrize("index", [0, -1, True, 1.0, "1"])
def test_non_positive_or_non_integer_index_is_rejected(tmp_path: Path, index: object) -> None:
    path = tmp_path / "temporary"
    with writer(path) as output:
        with pytest.raises(ValueError, match="positive integer"):
            output.add_chapter(EpubChapter(index, "非法", ("正文。",)))  # type: ignore[arg-type]
        # 校验失败不污染写入器：补齐合法章节后仍能正常收尾。
        output.add_chapter(EpubChapter(1, "补章", ("正文。",)))
    opf = xml_part(path, "OEBPS/content.opf")
    assert spine_hrefs(opf) == ["text/chapter-0001.xhtml"]


def test_out_of_order_index_is_rejected_without_poisoning(tmp_path: Path) -> None:
    path = tmp_path / "temporary"
    with writer(path) as output:
        output.add_chapter(EpubChapter(1, "第一章", ("正文。",)))
        with pytest.raises(ValueError, match="increase by one"):
            output.add_chapter(EpubChapter(3, "第三章", ("正文。",)))
        with pytest.raises(ValueError, match="increase by one"):
            output.add_chapter(EpubChapter(1, "第一章", ("正文。",)))
        output.add_chapter(EpubChapter(2, "第二章", ("正文。",)))
    opf = xml_part(path, "OEBPS/content.opf")
    assert spine_hrefs(opf) == ["text/chapter-0001.xhtml", "text/chapter-0002.xhtml"]


@pytest.mark.parametrize("exception", [RuntimeError, KeyboardInterrupt])
def test_body_exception_closes_without_finalizing(
    tmp_path: Path, exception: type[BaseException]
) -> None:
    path = tmp_path / "temporary"
    with pytest.raises(exception, match="cancelled"):
        with writer(path) as output:
            handle = output._archive
            output.add_chapter(EpubChapter(1, "第一章", ("正文。",)))
            raise exception("cancelled")
    assert handle is not None and handle.fp is None
    with ZipFile(path) as result:
        assert result.namelist() == ["mimetype", "OEBPS/text/chapter-0001.xhtml"]
        assert result.testzip() is None


def test_failed_write_marks_the_writer_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = ZipFile.writestr

    def fail(self: ZipFile, name: object, data: bytes, *args: object, **kwargs: object) -> None:
        if getattr(name, "filename", "").endswith(".xhtml"):
            raise OSError("disk failure")
        original(self, name, data, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ZipFile, "writestr", fail)
    output = writer(tmp_path / "temporary")
    with pytest.raises(RuntimeError, match="failed write"):
        with output:
            with pytest.raises(OSError, match="disk failure"):
                output.add_chapter(EpubChapter(1, "第一章", ("正文。",)))
            with pytest.raises(RuntimeError, match="not open"):
                output.add_chapter(EpubChapter(2, "第二章", ("正文。",)))
    assert output._archive is None


def test_write_to_closed_archive_fails_and_stops(tmp_path: Path) -> None:
    path = tmp_path / "temporary"
    output = writer(path)
    with pytest.raises(RuntimeError, match="failed write"):
        with output:
            assert output._archive is not None
            output._archive.close()
            with pytest.raises(ValueError, match="closed"):
                output.add_chapter(EpubChapter(1, "第一章", ("正文。",)))
            with pytest.raises(RuntimeError, match="not open"):
                output.add_chapter(EpubChapter(1, "第一章", ("正文。",)))
    with ZipFile(path) as result:
        assert result.namelist() == ["mimetype"]
