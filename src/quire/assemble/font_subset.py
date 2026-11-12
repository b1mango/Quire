"""把用户指定的字体裁为 EPUB 实际使用的字符。"""

from __future__ import annotations

from io import BytesIO

from fontTools import subset
from fontTools.ttLib import TTFont

from ..errors import ConfigError


def subset_font(path: str, characters: set[str]) -> tuple[bytes, str]:
    try:
        with TTFont(path, fontNumber=0, recalcTimestamp=False) as font:
            options = subset.Options()
            options.drop_tables += ["DSIG", "meta"]
            subsetter = subset.Subsetter(options=options)
            subsetter.populate(unicodes={ord(char) for char in characters})
            subsetter.subset(font)
            font.flavor = None
            media_type = "font/otf" if font.sfntVersion == "OTTO" else "font/ttf"
            stream = BytesIO()
            font.save(stream)
            return stream.getvalue(), media_type
    except Exception as exc:
        raise ConfigError("EPUB 字体文件不存在、无法读取或无法解析") from exc
