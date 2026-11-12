from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from quire.reader import comic, txt
from quire.reader.archive import read_entry, safe_path


def test_txt_chapters_and_gb18030(tmp_path: Path) -> None:
    path = tmp_path / "book.txt"
    path.write_bytes("第一章 初见\n正文 <内容>\n第二回 重逢\n再见".encode("gb18030"))
    sections = txt.sections(path)
    assert [section.title for section in sections] == ["第一章 初见", "第二回 重逢"]
    assert sections[0].html == "<h1>第一章 初见</h1><p>正文 &lt;内容&gt;</p>"
    assert sections[1].html == "<h1>第二回 重逢</h1><p>再见</p>"


def test_txt_without_headings_and_cache_invalidation(tmp_path: Path) -> None:
    path = tmp_path / "book.txt"
    path.write_text("正文" * 5000, encoding="utf-8")
    sections = txt.sections(path)
    assert len(sections) == 2
    assert sections[0].title == "第 1 段"
    assert sections[0].html == f"<p>{'正文' * 4000}</p>"
    path.write_text("新的正文", encoding="utf-8")
    assert txt.sections(path)[0].html == "<p>新的正文</p>"


def test_comic_natural_order_and_ignored_files(tmp_path: Path) -> None:
    path = tmp_path / "book.cbz"
    with ZipFile(path, "w") as archive:
        for name in (
            "p10.jpg",
            "p2.jpg",
            "__MACOSX/p1.jpg",
            ".hidden.png",
            "folder/.p1.jpg",
            "info.txt",
        ):
            archive.writestr(name, name)
    assert comic.pages(path) == ("p2.jpg", "p10.jpg")
    assert comic.read_page(path, 0) == (b"p2.jpg", "image/jpeg")
    with pytest.raises(IndexError):
        comic.read_page(path, 2)


def test_archive_rejects_zip_bomb_and_traversal(tmp_path: Path) -> None:
    path = tmp_path / "unsafe.zip"
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("bomb.png", b"0" * 100000)
        archive.writestr("../outside.png", b"image")
    with ZipFile(path) as archive:
        with pytest.raises(ValueError, match="压缩比"):
            read_entry(archive, "bomb.png")
        with pytest.raises(ValueError):
            read_entry(archive, "../outside.png")
    with pytest.raises(ValueError):
        comic.pages(path)
    for name in ("%2e%2e/image.png", "/image.png", "..%2fimage.png"):
        with pytest.raises(ValueError):
            safe_path(unquote(name))


def test_literal_percent_zip_name_and_open_streams(tmp_path: Path) -> None:
    path = tmp_path / "book.cbz"
    with ZipFile(path, "w") as archive:
        archive.writestr("page%20name.png", b"image")
    with path.open("rb") as stream:
        assert comic.pages(stream) == ("page%20name.png",)
        assert comic.read_page(stream, 0) == (b"image", "image/png")
    text_path = tmp_path / "book.txt"
    text_path.write_text("正文", encoding="utf-8")
    with text_path.open("rb") as stream:
        assert txt.sections(text_path, stream)[0].html == "<p>正文</p>"
