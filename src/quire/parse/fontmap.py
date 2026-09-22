"""字体反混淆:把自定义字体的私有码位(PUA)还原成真实汉字(纯函数,无 I/O)。

站点把正文常用字映射到 PUA 码位,浏览器靠页面引用的自定义 woff/otf 字体
才能显示成人能读的字。字体文件里的字形轮廓仍是真实汉字的形状,因此可以
用 fontTools 取出每个 PUA 码位的轮廓,与内置指纹库比对还原。

指纹库(``fontdb.bin``,与本模块同目录)由 ``scripts/build_font_db.py``
用思源黑体生成:每个常用汉字一条「规范化轮廓哈希 → 字符」记录。番茄小说
的混淆字体实测派生自思源黑体(字体 name 表自述 SourceHanSansSC),轮廓
可以精确匹配。匹配只接受哈希一致的结果;认不出的码位
不猜,抛 ``ParseError``,由上层如实报失败而不是输出乱码书。
"""

from __future__ import annotations

import hashlib
import io
import lzma
import re
import struct
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit

from ..errors import ParseError

if TYPE_CHECKING:
    from fontTools.ttLib import TTFont

#: 私有使用区(基本多文种平面)。番茄等站的混淆码位都在这个区间。
_PUA = re.compile(r"[-]")

#: 页面里出现这么多 PUA 字符才认为存在字体混淆(避免误伤零星符号)。
MIN_PUA_CHARS = 8

#: 指纹库文件名(与本模块同目录,随包分发)。
DB_NAME = "fontdb.bin"

#: 轮廓规范化后坐标量化步长(千分之一 em 取整)。
_SCALE = 1000

#: 规范化轮廓签名:((操作名, ((x, y), ...)), ...),坐标已按千分 em 取整。
Signature = tuple[tuple[str, tuple[tuple[int, int], ...]], ...]

#: 指纹库二进制格式:magic + 条目数,每条目 = 字符(UTF-16BE 2 字节) + 哈希 16 字节。
_MAGIC = b"QFDB1"


def pua_chars(text: str) -> frozenset[str]:
    """文本里出现的 PUA 码位集合。"""
    return frozenset(_PUA.findall(text))


def needs_deobfuscation(text: str) -> bool:
    """文本是否含有成规模的字体混淆字符。"""
    return len(_PUA.findall(text)) >= MIN_PUA_CHARS


def find_font_urls(html: str, base_url: str = "") -> list[str]:
    """提取内联字体声明，保留签名查询参数并解析相对地址。"""
    urls: dict[str, int] = {}
    for match in re.finditer(r"@font-face\s*\{([^}]*)\}", html, re.I):
        for raw in re.findall(r"url\(\s*([^)]*?)\s*\)", match[1], re.I):
            url = urljoin(base_url, raw.strip().strip("\"'"))
            parts = urlsplit(url)
            extension = parts.path.rsplit(".", 1)[-1].lower()
            if parts.scheme not in {"http", "https"} or not parts.hostname:
                continue
            if parts.username is not None or parts.password is not None:
                continue
            if extension in {"woff", "otf", "ttf", "woff2"}:
                urls.setdefault(url, {"woff": 0, "otf": 1, "ttf": 2}.get(extension, 3))
    return sorted(urls, key=lambda u: urls[u])


def _open_font(data: bytes) -> TTFont:
    from fontTools.ttLib import TTFont

    try:
        return TTFont(io.BytesIO(data), lazy=True)
    except Exception as exc:  # fontTools 抛多种内部异常,统一成可读的解析错误
        raise ParseError("字体文件无法解析", hint=str(exc) or exc.__class__.__name__) from exc


def _outline_signature(font: TTFont, glyph_name: str) -> Signature:
    """字形的规范化轮廓签名:操作序列 + 千分 em 取整坐标(与字号无关)。"""
    from fontTools.pens.recordingPen import RecordingPen

    glyph_set = font.getGlyphSet()
    pen = RecordingPen()
    try:
        glyph_set[glyph_name].draw(pen)
    except Exception as exc:
        raise ParseError("字形轮廓读取失败", hint=glyph_name) from exc
    upm = font["head"].unitsPerEm or 1000

    def norm(point: tuple[float, float]) -> tuple[int, int]:
        return (round(point[0] * _SCALE / upm), round(point[1] * _SCALE / upm))

    ops: list[tuple[str, tuple[tuple[int, int], ...]]] = []
    for operator, args in pen.value:
        ops.append((operator, tuple(norm(p) for p in args if isinstance(p, tuple))))
    return tuple(ops)


def signature_digest(signature: Signature) -> bytes:
    """轮廓签名的 16 字节摘要(库与运行时共用同一算法)。"""
    return hashlib.sha1(repr(signature).encode()).digest()[:16]


def _font_signatures(data: bytes) -> dict[str, tuple[str, Signature]]:
    """字体里全部 PUA 码位 → (字形名, 轮廓签名)。"""
    font = _open_font(data)
    cmap = font.getBestCmap() or {}
    out: dict[str, tuple[str, Signature]] = {}
    for codepoint, glyph_name in cmap.items():
        if 0xE000 <= codepoint <= 0xF8FF:
            out[chr(codepoint)] = (glyph_name, _outline_signature(font, glyph_name))
    return out


@lru_cache(maxsize=1)
def _reference_db() -> dict[bytes, str]:
    """内置指纹库:轮廓摘要 → 汉字。缺库时返回空表,匹配会如实失败。"""
    path = Path(__file__).with_name(DB_NAME)
    if not path.exists():
        return {}
    raw = lzma.decompress(path.read_bytes())
    if raw[:5] != _MAGIC:
        raise ParseError("字体指纹库损坏", hint=str(path))
    (count,) = struct.unpack_from("<I", raw, 5)
    entries: dict[bytes, str] = {}
    offset = 9
    for _ in range(count):
        char = raw[offset : offset + 2].decode("utf-16-be")
        digest = raw[offset + 2 : offset + 18]
        entries[digest] = char
        offset += 18
    return entries


def build_mapping(font_data: bytes, db: dict[bytes, str] | None = None) -> dict[str, str]:
    """PUA 字符 → 汉字的映射。任一码位认不出都整体失败,不输出半猜的书。"""
    signatures = _font_signatures(font_data)
    if not signatures:
        raise ParseError("混淆字体里没有 PUA 字形,无法还原")
    reference = db if db is not None else _reference_db()
    if not reference:
        raise ParseError("缺少字体指纹库 fontdb.bin,无法还原混淆字体")
    mapping: dict[str, str] = {}
    unknown: list[str] = []
    for char, (_name, signature) in signatures.items():
        hit = reference.get(signature_digest(signature))
        if hit is not None:
            mapping[char] = hit
            continue
        unknown.append(char)
    if unknown:
        raise ParseError(
            f"混淆字体有 {len(unknown)} 个字形与指纹库不匹配",
            hint="站点可能更换了基础字体;不猜测,以避免输出错字。",
        )
    return mapping


def translate(text: str, mapping: dict[str, str]) -> str:
    """用映射替换文本里的 PUA 字符;映射外的 PUA 原样保留(由调用方计数)。"""
    if not mapping:
        return text
    return text.translate({ord(char): mapped for char, mapped in mapping.items()})
