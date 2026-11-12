"""EPUB 版式预设和用户提供的字体路径校验。"""

from __future__ import annotations

from pathlib import Path

from ..errors import ConfigError

EPUB_STYLES = ("songti", "heiti", "classic")


def validate_epub_options(style: str, font: str) -> None:
    if not isinstance(style, str) or style not in EPUB_STYLES:
        raise ConfigError("EPUB 版式须为 songti、heiti 或 classic")
    if not isinstance(font, str) or (font and not Path(font).is_absolute()):
        raise ConfigError("EPUB 字体须为绝对路径，或留空")


def stylesheet(style: str, *, font_extension: str = "") -> str:
    family = "sans-serif" if style == "heiti" else "serif"
    prefix = '"Quire Embedded", ' if font_extension else ""
    line_height = "1.6" if style == "heiti" else "1.8"
    paragraph_margin = "0.25em" if style == "heiti" else "0.4em"
    heading_margin = "2em 0 0.8em" if style == "classic" else "1.4em 0 0.7em"
    heading_family = ""
    if style == "classic":
        heading_family = (
            f'  font-family: {prefix}"Kaiti SC", "STKaiti", "KaiTi", serif;\n'
            "  letter-spacing: 0.12em;\n"
        )
    face = ""
    if font_extension:
        face = (
            '@font-face {\n  font-family: "Quire Embedded";\n'
            f'  src: url("fonts/subset.{font_extension}");\n}}\n'
        )
    return (
        face
        + f"html {{\n  font-family: {prefix}{family};\n}}\n"
        + f"body {{\n  margin: 0 5%;\n  line-height: {line_height};\n"
        "  text-align: justify;\n  text-justify: inter-ideograph;\n}\n"
        + f"h1 {{\n  font-size: 1.3em;\n  margin: {heading_margin};\n"
        "  text-align: center;\n"
        + heading_family
        + "}\n"
        + f"p {{\n  margin: 0 0 {paragraph_margin};\n  text-indent: 2em;\n}}\n"
        "p.missing,\np.placeholder {\n  opacity: 0.65;\n  text-indent: 0;\n}\n"
    )
