from __future__ import annotations

from pathlib import Path

import pytest

from quire.errors import ParseError
from quire.importer import files, txt
from quire.importer.naming import filename_metadata
from quire.reader.epub import Epub


@pytest.mark.parametrize(
    ("name", "title", "author"),
    [
        ("《春山》作者：青禾.txt", "春山", "青禾"),
        ("《春山》（青禾）.txt", "春山", "青禾"),
        ("春山 作者：青禾.txt", "春山", "青禾"),
        ("春山_青禾.txt", "春山", "青禾"),
        ("春山-青禾.txt", "春山", "青禾"),
        ("【整理】春山(精校版)[全本].TXT", "春山", ""),
        ("春山-2026.txt", "春山-2026", ""),
    ],
)
def test_filename_metadata(name, title, author):
    assert filename_metadata(name) == (title, author)


@pytest.mark.parametrize("encoding", ["gb18030", "utf-16"])
def test_txt_decoding_metadata_and_cleanup(tmp_path: Path, encoding: str):
    source = tmp_path / "input"
    source.write_bytes(
        (
            "书名：新书\r\n作者：阿青\r\n简介文字。\r\n第一卷\r\n"
            "第1章 初见\r\n第一段正文。\r\n请记住本站网址\r\n"
            "第1章 初见\r\n这里仍是第一章。\r\nChapter 2 The next\r\n第二段正文。\r\n"
        ).encode(encoding)
    )
    result = files.prepare(source, "旧书_旧作者.txt", tmp_path / "out", "heiti", "")
    assert (result.title, result.author, result.kind) == ("新书", "阿青", "novel")
    assert result.chapters == 3
    text = result.files[1][1].read_text("utf-8")
    assert "前言" in text and "第一卷" in text and "Chapter 2" in text
    assert "请记住本站" not in text
    assert "这里仍是第一章。" in text
    assert "1 行" in " ".join(result.notes)
    assert Epub(result.files[0][1]).sections == 3


def test_no_heading_splits_at_paragraph_boundaries(tmp_path: Path):
    source = tmp_path / "input"
    source.write_text("甲" * 8000 + "\n" + "乙" * 20, encoding="utf-8")
    result = files.prepare(source, "长文.txt", tmp_path / "out", "songti", "")
    assert result.chapters == 2
    assert "第 1 段\n\n" in result.files[1][1].read_text("utf-8")
    assert "8000" in " ".join(result.notes)


def test_invalid_txt_and_cleanup(tmp_path: Path):
    source = tmp_path / "input"
    source.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ParseError):
        txt.analyze(source, "书", "")
    source.write_text("请记住本站网址\n", encoding="utf-8")
    with pytest.raises(ParseError):
        files.prepare(source, "书.txt", tmp_path / "out", "songti", "")
    assert not list((tmp_path / "out" / "书").iterdir())


def test_volume_restart_chapters_and_stray_heading(tmp_path: Path):
    volumes = "".join(
        f"第{v}卷\n"
        + "".join(
            f"第{c}章\n" + "".join(f"{v}卷{c}章第{k}句。\n" for k in range(5)) for c in (1, 2)
        )
        for v in ("一", "二")
    )
    source = tmp_path / "vol.txt"
    source.write_text(volumes, encoding="utf-8")
    result = files.prepare(source, "分卷.txt", tmp_path / "out", "songti", "")
    assert result.chapters == 4  # 每卷都从第 1 章编号，不能被并掉
    stray = tmp_path / "stray.txt"
    stray.write_text(
        "第一章\n" + "".join(f"很长的正文第{k}句。\n" for k in range(3000)), encoding="utf-8"
    )
    result = files.prepare(stray, "孤标题.txt", tmp_path / "out2", "songti", "")
    assert result.chapters > 1 and "未识别到章节标题" in "".join(
        result.notes
    )  # 只有 1 个标题：按字数分段
