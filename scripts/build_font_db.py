"""生成字体反混淆的指纹库(开发工具,不入运行时;产物 fontdb.bin 入库)。

用法:
    .venv/bin/python scripts/build_font_db.py <思源黑体TTC路径>

- 输入:SourceHanSans TTC(实测番茄混淆字体自述派生自 SourceHanSansSC
  2.002,指纹库须用同一版本生成;TTC 从 adobe-fonts/source-han-sans
  发布页获取,不随仓库分发)。库覆盖 TTC 里全部 SC 字重(番茄正文用
  Normal,字重 500/700 的混淆字体也能匹配)。
- 字符集:参考字体 cmap 全量(4 万余码位)。同一轮廓对应多个码位时
  (如兼容字形)优先取 GB2312 收录的常用字,其次取小码位。
- 产物 1:src/quire/parse/fontdb.bin(lzma 压缩;字符 + 16 字节轮廓摘要)。
- 产物 2:tests/fixtures/obfuscated-sans.woff + obfuscated-truth.json,
  一个把 40 个常用字重映射到 PUA 码位的小字体,供测试模拟站点混淆。
"""

from __future__ import annotations

import argparse
import io
import json
import lzma
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quire.parse.fontmap import _outline_signature, signature_digest  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "src/quire/parse/fontdb.bin"
FIXTURE_FONT = ROOT / "tests/fixtures/obfuscated-sans.woff"
FIXTURE_TRUTH = ROOT / "tests/fixtures/obfuscated-truth.json"

#: 测试夹具用字:含简/繁结构差大的字,足以检验比对不是瞎猜。
FIXTURE_CHARS = "的一是不了我人在他有这个上们来到时大地为子中你说生国年着就那和要她出也得里后自以会家可下而过天去"


def target_charset() -> set[str]:
    """指纹库覆盖的字符集:ASCII 可打印 + GB2312 全表 + CJK 基本块。

    番茄实测会把 ASCII 字母数字也换成 PUA(用字体的半角字形),GB2312
    里只有全角字母,必须单收;「稜」这类 GB2312 外的常用字用基本块兜底。
    """
    chars = {chr(cp) for cp in range(0x20, 0x7F)}
    chars |= {chr(cp) for cp in range(0x4E00, 0xA000)}
    for lead in range(0xA1, 0xF8):
        for trail in range(0xA1, 0xFF):
            try:
                chars.add(bytes([lead, trail]).decode("gb2312"))
            except UnicodeDecodeError:
                continue
    return chars


def sc_fonts(ttc_path: Path):
    """TTC 里全部 SC 字重子字体(排除半角 HW 变体)。进程短命,不逐个 close。"""
    from fontTools.ttLib import TTCollection

    for font in TTCollection(str(ttc_path), lazy=True).fonts:
        family = next((n.toUnicode() for n in font["name"].names if n.nameID == 1), "")
        if "Source Han Sans SC" in family:
            yield font


def build_db(ttc_path: Path) -> None:
    common = target_charset()
    entries: dict[bytes, str] = {}
    for font in sc_fonts(ttc_path):
        cmap = font.getBestCmap() or {}
        for codepoint, glyph_name in cmap.items():
            char = chr(codepoint)
            # 站点只会拿常用字做混淆(冷僻字没有混淆价值);库只收 GB2312
            # 字集(常用字+ASCII+基本块),混淆字体若含库外字形就如实失败,不猜。
            if char not in common:
                continue
            entries.setdefault(signature_digest(_outline_signature(font, glyph_name)), char)
    sys.stdout.write(f"指纹 {len(entries)} 条\n")
    payload = bytearray(b"QFDB1" + struct.pack("<I", len(entries)))
    for digest, char in entries.items():
        payload += char.encode("utf-16-be") + digest
    packed = lzma.compress(bytes(payload), preset=9)
    DB_PATH.write_bytes(packed)
    sys.stdout.write(f"写入 {DB_PATH} ({len(packed)} bytes)\n")


def build_fixture(ttc_path: Path) -> None:
    """从参考字体抽 40 个字,把 cmap 重映射到 PUA,模拟站点的混淆字体。"""
    from fontTools import subset
    from fontTools.ttLib import TTFont

    source = TTFont(str(ttc_path), fontNumber=_normal_index(ttc_path))
    cmap = source.getBestCmap() or {}
    truth = {hex(0xE000 + i): char for i, char in enumerate(FIXTURE_CHARS)}
    glyph_names = [cmap[ord(char)] for char in FIXTURE_CHARS]
    options = subset.Options()
    options.name_IDs = ["*"]
    subsetter = subset.Subsetter(options)
    subsetter.populate(glyphs=glyph_names)
    subsetter.subset(source)
    for table in source["cmap"].tables:
        if table.isUnicode():
            table.cmap.clear()
            table.cmap.update({0xE000 + i: name for i, name in enumerate(glyph_names)})
    buffer = io.BytesIO()
    source.flavor = "woff"
    source.save(buffer)
    FIXTURE_FONT.write_bytes(buffer.getvalue())
    FIXTURE_TRUTH.write_text(
        json.dumps(truth, ensure_ascii=False, indent=2, sort_keys=True) + "\n", "utf-8"
    )
    sys.stdout.write(f"写入 {FIXTURE_FONT} 与 {FIXTURE_TRUTH}\n")


def _normal_index(ttc_path: Path) -> int:
    """TTC 中 SC Normal 字重的序号(夹具按它抽字形)。"""
    from fontTools.ttLib import TTCollection

    for index, font in enumerate(TTCollection(str(ttc_path), lazy=True).fonts):
        family = next((n.toUnicode() for n in font["name"].names if n.nameID == 1), "")
        full = next((n.toUnicode() for n in font["name"].names if n.nameID == 4), "")
        if "SC" in family and "HW" not in family and full.endswith("Normal"):
            return index
    raise SystemExit("TTC 里找不到 Source Han Sans SC Normal")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("font", type=Path, help="SourceHanSans TTC 路径")
    args = parser.parse_args()
    if not args.font.is_file():
        raise SystemExit(f"字体不存在:{args.font}")
    build_db(args.font)
    build_fixture(args.font)


if __name__ == "__main__":
    main()
