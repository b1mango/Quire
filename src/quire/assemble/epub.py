"""确定性 EPUB 3 写入器：只接收已清洗的小说章节，流式实现（项目设计.md §37.6）。

两条纪律决定了这里的写法：

- **同输入字节稳定**：时间戳、标识符、条目顺序全部固定，EPUB 3 要求的
  `dcterms:modified` 写成常量而不是当前时钟，便于缓存、校验与回归比对。
- **流式写入**：章节随到随写，不在内存里囤整本正文；任何一次写入失败立即
  标记失败并向上抛，后续写入与收尾都拒绝继续，避免产出半本“能打开”的书。

文本安全分两步，缺一不可：所有外部文本先经 `clean_metadata_text` 去掉 XML 1.0
非法字符（控制符、代理对、U+FFFE/U+FFFF），再做实体转义。清理不能替代转义
（`<` 仍是标签），转义也不能替代清理（代理对根本编码不进 UTF-8）。

本模块只写调用方给的路径：不建目录、不改名、不发布，原子发布由外层负责。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from xml.etree import ElementTree
from xml.sax.saxutils import escape, quoteattr
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from .cover import create_cover
from .epub_style import stylesheet, validate_epub_options
from .font_subset import subset_font
from .models import clean_metadata_text

__all__ = ["EpubChapter", "EpubWriter"]

#: mimetype 必须是首个条目、不压缩，内容不含结尾换行。
_MIMETYPE = b"application/epub+zip"

#: EPUB 3 要求 dcterms:modified，但本模块不引入时钟：固定值保证字节稳定。
_MODIFIED = "1980-01-01T00:00:00Z"

_XML_HEADER = '<?xml version="1.0" encoding="UTF-8"?>'
_CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
_OPF_NS = "http://www.idpf.org/2007/opf"
_DC_NS = "http://purl.org/dc/elements/1.1/"
_XHTML_NS = "http://www.w3.org/1999/xhtml"
_EPUB_NS = "http://www.idpf.org/2007/ops"
_NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"


@dataclass(frozen=True, slots=True)
class EpubChapter:
    """一章已清洗的正文；`index` 为 1 起的阅读顺序，必须严格递增。"""

    index: int
    title: str
    paragraphs: tuple[str, ...]
    source_url: str = ""
    missing_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _ChapterRef:
    """OPF/NAV/NCX 共用的章节条目；`href` 相对 OEBPS 目录。"""

    item_id: str
    href: str
    title: str


def _entry(name: str, compression: int) -> ZipInfo:
    """固定时间戳、创建平台与权限位，两次写入才能逐字节一致。"""
    info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = compression
    info.create_system = 3
    info.external_attr = 0o100600 << 16
    return info


def _esc(value: str) -> str:
    """文本节点：先清理非法字符，再转义 `&` `<` `>`。"""
    return escape(clean_metadata_text(value))


def _attr(value: str) -> str:
    """属性值：清理非法字符后交给 `quoteattr` 处理引号与换行。"""
    return quoteattr(clean_metadata_text(value))


def _container() -> str:
    """META-INF/container.xml：声明包文档位置。"""
    return "\n".join(
        [
            _XML_HEADER,
            f'<container version="1.0" xmlns="{_CONTAINER_NS}">',
            "  <rootfiles>",
            '    <rootfile full-path="OEBPS/content.opf"'
            ' media-type="application/oebps-package+xml"/>',
            "  </rootfiles>",
            "</container>",
        ]
    )


class EpubWriter:
    """把章节流式写进调用方给的临时路径，收尾时补齐 EPUB 3 结构。

    单次上下文：`__enter__` 只能成功一次，退出后不可重新进入。章节必须从 1
    开始、每次加一。任何写入失败都会把写入器标记为失败，之后的 `add_chapter`
    与正常收尾都抛 `RuntimeError`，调用方只能丢弃这个候选文件。
    """

    def __init__(
        self,
        path: Path,
        *,
        title: str,
        source_url: str = "",
        language: str = "zh",
        identifier: str = "",
        style: str = "songti",
        font: str = "",
        cover: bool = True,
    ) -> None:
        validate_epub_options(style, font)
        self._path = path
        self._style = style
        self._font = font
        self._cover = cover
        self.warnings: list[str] = []
        self._title = clean_metadata_text(title)
        self._source_url = clean_metadata_text(source_url)
        self._language = clean_metadata_text(language) or "zh"
        # 显式标识符原样使用；否则用书名 + 来源派生稳定的 urn:quire:。
        resolved = clean_metadata_text(identifier)
        if not resolved:
            digest = hashlib.sha256(f"{title}\n{source_url}".encode()).hexdigest()
            resolved = f"urn:quire:{digest[:40]}"
        self._identifier = resolved
        self._characters = set("目录") if font else set()
        self._chapters: list[_ChapterRef] = []
        self._archive: ZipFile | None = None
        self._used = False
        self._failed = False

    def __enter__(self) -> EpubWriter:
        if self._used:
            raise RuntimeError("EPUB 写入上下文不能重复使用")
        self._used = True
        archive = ZipFile(self._path, "x", compression=ZIP_STORED)
        try:
            archive.writestr(_entry("mimetype", ZIP_STORED), _MIMETYPE)
        except BaseException:
            self._failed = True
            archive.close()
            raise
        self._archive = archive
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        archive = self._archive
        self._archive = None
        if archive is None:
            return
        try:
            # 体内已有异常时只关句柄，不再追加任何条目。
            if exc_type is None:
                if self._failed:
                    raise RuntimeError("写入失败后无法完成 EPUB")
                for name, data in self._documents():
                    archive.writestr(_entry(name, ZIP_DEFLATED), data, compresslevel=6)
        except BaseException:
            self._failed = True
            raise
        finally:
            archive.close()

    def add_chapter(self, chapter: EpubChapter) -> None:
        """写入一章 XHTML；顺序或类型不合法时抛 `ValueError`，且不产出条目。"""
        archive = self._archive
        if archive is None or self._failed:
            raise RuntimeError("EPUB 写入器未打开")
        if type(chapter.index) is not int or chapter.index <= 0:
            raise ValueError("章节索引须为正整数")
        if chapter.index != len(self._chapters) + 1:
            raise ValueError("章节须从 1 开始依次递增")
        item_id = f"chapter-{chapter.index:04d}"
        href = f"text/{item_id}.xhtml"
        title = clean_metadata_text(chapter.title) or f"第 {chapter.index} 章"
        try:
            document = self._chapter_document(chapter, title)
            archive.writestr(
                _entry(f"OEBPS/{href}", ZIP_DEFLATED), document.encode("utf-8"), compresslevel=6
            )
            self._chapters.append(_ChapterRef(item_id=item_id, href=href, title=title))
            if self._font:
                self._characters.update(title)
                for node in ElementTree.fromstring(document)[1]:
                    self._characters.update("".join(node.itertext()))
        except BaseException:
            self._failed = True
            raise

    def _documents(self) -> list[tuple[str, bytes]]:
        """收尾补齐的固定条目；顺序固定，保证字节稳定。"""
        cover = create_cover(self._title) if self._cover else None
        if self._cover and cover is None:
            self.warnings.append("未找到可显示书名的系统中文字体，未生成 EPUB 封面")
        font_data, font_media = (
            subset_font(self._font, self._characters) if self._font else (b"", "")
        )
        font_extension = font_media.removeprefix("font/")
        documents = [
            ("META-INF/container.xml", _container().encode("utf-8")),
            ("OEBPS/content.opf", self._package(cover is not None, font_extension).encode("utf-8")),
            ("OEBPS/nav.xhtml", self._nav().encode("utf-8")),
            ("OEBPS/toc.ncx", self._ncx().encode("utf-8")),
            (
                "OEBPS/style.css",
                stylesheet(self._style, font_extension=font_extension).encode("utf-8"),
            ),
        ]
        if cover is not None:
            documents.extend(
                [
                    ("OEBPS/cover.png", cover),
                    ("OEBPS/cover.xhtml", self._cover_document().encode("utf-8")),
                ]
            )
        if font_extension:
            documents.append((f"OEBPS/fonts/subset.{font_extension}", font_data))
        return documents

    def _cover_document(self) -> str:
        language = _attr(self._language)
        return "\n".join(
            [
                _XML_HEADER,
                "<!DOCTYPE html>",
                f'<html xmlns="{_XHTML_NS}" xml:lang={language} lang={language}>',
                f"  <head><title>{_esc(self._title)}</title></head>",
                '  <body style="margin:0;text-align:center">',
                f'    <img src="cover.png" alt={_attr(self._title)} style="max-width:100%;max-height:100%"/>',
                "  </body>",
                "</html>",
            ]
        )

    def _package(self, has_cover: bool, font_extension: str) -> str:
        """EPUB 3 包文档：元数据只写已确定的字段。"""
        language = _attr(self._language)
        lines = [
            _XML_HEADER,
            f'<package xmlns="{_OPF_NS}" version="3.0" unique-identifier="pub-id"'
            f" xml:lang={language}>",
            f'  <metadata xmlns:dc="{_DC_NS}">',
            f'    <dc:identifier id="pub-id">{_esc(self._identifier)}</dc:identifier>',
            f"    <dc:title>{_esc(self._title)}</dc:title>",
            f"    <dc:language>{_esc(self._language)}</dc:language>",
        ]
        if self._source_url:
            lines.append(f"    <dc:source>{_esc(self._source_url)}</dc:source>")
        if has_cover:
            lines.append('    <meta name="cover" content="cover-image"/>')
        lines.extend(
            [
                f'    <meta property="dcterms:modified">{_MODIFIED}</meta>',
                "  </metadata>",
                "  <manifest>",
                '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml"'
                ' properties="nav"/>',
                '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
                '    <item id="style" href="style.css" media-type="text/css"/>',
            ]
        )
        if has_cover:
            lines.extend(
                [
                    '    <item id="cover-image" href="cover.png" media-type="image/png" properties="cover-image"/>',
                    '    <item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>',
                ]
            )
        if font_extension:
            lines.append(
                f'    <item id="font" href="fonts/subset.{font_extension}" media-type="font/{font_extension}"/>'
            )
        for ref in self._chapters:
            lines.append(
                f"    <item id={_attr(ref.item_id)} href={_attr(ref.href)}"
                ' media-type="application/xhtml+xml"/>'
            )
        lines.extend(["  </manifest>", '  <spine toc="ncx">'])
        if has_cover:
            lines.append('    <itemref idref="cover" linear="yes"/>')
        for ref in self._chapters:
            lines.append(f"    <itemref idref={_attr(ref.item_id)}/>")
        lines.extend(["  </spine>", "</package>"])
        return "\n".join(lines)

    def _nav(self) -> str:
        """EPUB 3 导航文档：`<nav epub:type="toc">` 里按阅读顺序列出章节。"""
        language = _attr(self._language)
        lines = [
            _XML_HEADER,
            "<!DOCTYPE html>",
            f'<html xmlns="{_XHTML_NS}" xmlns:epub="{_EPUB_NS}"'
            f" xml:lang={language} lang={language}>",
            "  <head>",
            '    <meta charset="utf-8"/>',
            f"    <title>{_esc(self._title)}</title>",
            '    <link rel="stylesheet" type="text/css" href="style.css"/>',
            "  </head>",
            "  <body>",
            '    <nav epub:type="toc" id="toc">',
            "      <h1>目录</h1>",
            "      <ol>",
        ]
        for ref in self._chapters:
            lines.append(f"        <li><a href={_attr(ref.href)}>{_esc(ref.title)}</a></li>")
        lines.extend(["      </ol>", "    </nav>", "  </body>", "</html>"])
        return "\n".join(lines)

    def _ncx(self) -> str:
        """EPUB 2 回退目录：`playOrder` 从 1 连续编号。"""
        lines = [
            _XML_HEADER,
            f'<ncx xmlns="{_NCX_NS}" version="2005-1" xml:lang={_attr(self._language)}>',
            "  <head>",
            f'    <meta name="dtb:uid" content={_attr(self._identifier)}/>',
            '    <meta name="dtb:depth" content="1"/>',
            '    <meta name="dtb:totalPageCount" content="0"/>',
            '    <meta name="dtb:maxPageNumber" content="0"/>',
            "  </head>",
            f"  <docTitle><text>{_esc(self._title)}</text></docTitle>",
            "  <navMap>",
        ]
        for order, ref in enumerate(self._chapters, start=1):
            lines.append(
                f'    <navPoint id={_attr(f"navPoint-{order}")} playOrder="{order}">'
                f"<navLabel><text>{_esc(ref.title)}</text></navLabel>"
                f"<content src={_attr(ref.href)}/></navPoint>"
            )
        lines.extend(["  </navMap>", "</ncx>"])
        return "\n".join(lines)

    def _chapter_document(self, chapter: EpubChapter, title: str) -> str:
        """单章 XHTML：缺章只写原因，空章只写占位段，都不伪造正文。"""
        language = _attr(self._language)
        lines = [
            _XML_HEADER,
            "<!DOCTYPE html>",
            f'<html xmlns="{_XHTML_NS}" xml:lang={language} lang={language}>',
            "  <head>",
            '    <meta charset="utf-8"/>',
            f"    <title>{_esc(self._title)} - {_esc(title)}</title>",
            '    <link rel="stylesheet" type="text/css" href="../style.css"/>',
            "  </head>",
            "  <body>",
            f"    <h1>{_esc(title)}</h1>",
        ]
        if chapter.missing_reason is not None:
            reason = _esc(chapter.missing_reason)
            lines.append(f'    <p class="missing">本章抓取失败：{reason}</p>')
        elif chapter.paragraphs:
            lines.extend(f"    <p>{_esc(paragraph)}</p>" for paragraph in chapter.paragraphs)
        else:
            lines.append('    <p class="placeholder">（本章无正文）</p>')
        lines.extend(["  </body>", "</html>"])
        return "\n".join(lines)
