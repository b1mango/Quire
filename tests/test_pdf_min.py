"""PDF writer 的像素级验证。

这里的重点不是"能生成 PDF"，而是**嵌入的图与源图逐像素一致**。
JPEG 直嵌和 PNG 的 Predictor15 透传都是"零转码"路径，
一旦写错，产出的 PDF 照样能打开、照样能看，只是画质悄悄坏了——
这种 bug 只能靠像素比对抓出来。
"""

from __future__ import annotations

import io
import zlib

import pytest
from PIL import Image
from pypdf import PdfReader
from pypdf.generic import NameObject

from quire.assemble.pdf_min import MiniPdfWriter, pdf_text, write_images_to_pdf
from quire.image.probe import probe_bytes


def _unpack_bits(raw: bytes, width: int, height: int, bpc: int) -> bytes:
    """手动拆子字节位深。PIL 的 raw 解码器对 ``L;1`` 之类的模式支持不全，
    这里自己拆反而更可靠，而且测试代码不必抠性能。"""
    per_byte = 8 // bpc
    mask = (1 << bpc) - 1
    row_bytes = (width * bpc + 7) // 8
    out = bytearray(width * height)
    for y in range(height):
        base = y * row_bytes
        for x in range(width):
            byte = raw[base + x // per_byte]
            shift = 8 - bpc * (x % per_byte + 1)
            out[y * width + x] = (byte >> shift) & mask
    return bytes(out)


def _decode_samples(raw: bytes, size: tuple[int, int], bpc: int, mode: str) -> Image.Image:
    """按位深解码样本。子字节位深（1/2/4）在 PDF 里是高位优先打包的。"""
    width, height = size
    if bpc == 8:
        return Image.frombytes(mode, size, raw)
    if bpc == 16:
        # PIL 的 I;16 是**小端**，PDF 是大端，必须换序
        swapped = bytearray(len(raw))
        swapped[0::2] = raw[1::2]
        swapped[1::2] = raw[0::2]
        return Image.frombytes("I;16", size, bytes(swapped)).convert("L")
    if bpc in (1, 2, 4):
        flat = _unpack_bits(raw, width, height, bpc)
        if mode == "P":
            return Image.frombytes("P", size, flat)
        scale = 255 // ((1 << bpc) - 1)
        return Image.frombytes("L", size, bytes(v * scale for v in flat))
    raise AssertionError(f"测试助手不支持 {bpc} 位")


def _extract_image(page) -> Image.Image:  # type: ignore[no-untyped-def]
    """从 PDF 页里取出图像，按 PDF 声明的色彩空间与位深正确解码。"""
    xobj = page["/Resources"]["/XObject"]["/Im0"].get_object()
    raw: bytes = xobj.get_data()  # pypdf 已应用 /Filter 与 /DecodeParms
    width, height = int(xobj["/Width"]), int(xobj["/Height"])
    size = (width, height)
    bpc = int(xobj["/BitsPerComponent"])

    if xobj["/Filter"] == "/DCTDecode":
        return Image.open(io.BytesIO(raw)).convert("RGB")

    cs = xobj["/ColorSpace"]
    cs_name = cs if isinstance(cs, str) else None

    if cs_name is None or str(cs).startswith("["):
        # /Indexed：raw 是索引字节，需要自己挂调色板
        lookup = cs[3].get_object() if hasattr(cs, "__getitem__") else b""
        palette = bytes(lookup)[: 3 * (int(cs[2]) + 1)]
        im = _decode_samples(raw, size, bpc, "P")
        im.putpalette(palette + bytes(768 - len(palette)))
        return im.convert("RGB")

    mode = "L" if cs_name == "/DeviceGray" else "RGB"
    return _decode_samples(raw, size, bpc, mode).convert("RGB")


def _expected(path, *, flatten_alpha: bool = False) -> Image.Image:  # type: ignore[no-untyped-def]
    im = Image.open(path)
    if flatten_alpha and im.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        return bg
    return im.convert("RGB")


def _mean_diff(a: Image.Image, b: Image.Image) -> float:
    assert a.size == b.size, f"尺寸不符：{a.size} vs {b.size}"
    n = a.size[0] * a.size[1] * 3
    return sum(abs(x - y) for x, y in zip(a.tobytes(), b.tobytes(), strict=True)) / n


@pytest.mark.parametrize(
    "filename,flatten",
    [
        ("JPEG_RGB.jpg", False),
        ("PNG_RGB.png", False),
        ("PNG_RGBA.png", True),  # 我们合成到白底
        ("PNG_L.png", False),
        ("PNG_P.png", False),
        ("PNG_1.png", False),
    ],
)
def test_image_embedded_losslessly(image_dir, filename: str, flatten: bool) -> None:  # type: ignore[no-untyped-def]
    src_path = image_dir / filename
    out = image_dir / f"lossless_{filename}.pdf"

    with MiniPdfWriter(out, title="t", dpi=72) as pdf:
        embedded = pdf.add_image_file(src_path)

    assert embedded

    reader = PdfReader(str(out))
    got = _extract_image(reader.pages[0])
    want = _expected(src_path, flatten_alpha=flatten)

    assert _mean_diff(got, want) < 1.0, f"{filename} 嵌入后画质受损"


def test_unsupported_format_returns_false(image_dir) -> None:  # type: ignore[no-untyped-def]
    """WebP micro 档不支持——必须**返回 False** 而不是抛异常，
    这样调用方能降级成占位页继续跑（决策 H）。"""
    out = image_dir / "webp.pdf"
    with MiniPdfWriter(out) as pdf:
        assert pdf.add_image_file(image_dir / "WEBP_RGB.webp") is False
    assert len(PdfReader(str(out)).pages) == 0


def test_page_size_follows_dpi(image_dir) -> None:  # type: ignore[no-untyped-def]
    """800x1200 像素 @150dpi = 384x576 pt。算错会让整本书的比例失真。"""
    from tests.conftest import make_image_bytes

    p = image_dir / "size.jpg"
    p.write_bytes(make_image_bytes("JPEG", "RGB", (800, 1200)))
    out = image_dir / "size.pdf"
    with MiniPdfWriter(out, dpi=150) as pdf:
        pdf.add_image_file(p)
    box = PdfReader(str(out)).pages[0].mediabox
    assert abs(float(box.width) - 384.0) < 0.5
    assert abs(float(box.height) - 576.0) < 0.5


def test_paper_fit_contain(image_dir) -> None:  # type: ignore[no-untyped-def]
    """A4 纸张 contain 模式：页面是 A4，图居中且不超边界。"""
    from tests.conftest import make_image_bytes

    p = image_dir / "paper.jpg"
    p.write_bytes(make_image_bytes("JPEG", "RGB", (800, 1200)))
    out = image_dir / "paper.pdf"
    with MiniPdfWriter(out, paper="a4", fit="contain") as pdf:
        pdf.add_image_file(p)
    box = PdfReader(str(out)).pages[0].mediabox
    assert abs(float(box.width) - 595.28) < 1.0
    assert abs(float(box.height) - 841.89) < 1.0


def test_outline_and_cjk_metadata(image_dir) -> None:  # type: ignore[no-untyped-def]
    """中文元数据必须走 UTF-16BE，否则阅读器里是乱码。"""
    from tests.conftest import make_image_bytes

    p = image_dir / "cjk.jpg"
    p.write_bytes(make_image_bytes("JPEG", "RGB", (200, 280)))
    out = image_dir / "cjk.pdf"
    with MiniPdfWriter(out, title="海贼王 第一卷", author="尾田荣一郎") as pdf:
        pdf.add_image_file(p, title="第 1 话 · 冒险的黎明")
        pdf.add_image_file(p, title="第 2 话 · 戴草帽的路飞")

    reader = PdfReader(str(out))
    assert reader.metadata is not None
    assert reader.metadata.title == "海贼王 第一卷"
    assert reader.metadata.author == "尾田荣一郎"
    titles = [o.title for o in reader.outline]
    assert titles == ["第 1 话 · 冒险的黎明", "第 2 话 · 戴草帽的路飞"]


def test_placeholder_page_is_added(image_dir) -> None:  # type: ignore[no-untyped-def]
    out = image_dir / "ph.pdf"
    with MiniPdfWriter(out, paper="a4") as pdf:
        pdf.add_placeholder(["MISSING PAGE", "page 2 / 2", "https://x/2.jpg", "HTTP 404"])
    reader = PdfReader(str(out))
    assert len(reader.pages) == 1
    text = reader.pages[0].extract_text()
    assert "MISSING PAGE" in text
    assert "HTTP 404" in text


def test_pdf_text_encoding() -> None:
    assert pdf_text("abc") == b"(abc)"
    assert pdf_text("第1话").startswith(b"<FEFF")
    # 括号与反斜杠必须转义，否则会破坏 PDF 语法
    assert pdf_text("a(b)c\\d") == b"(a\\(b\\)c\\\\d)"


def test_write_images_helper(image_dir, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import make_image_bytes

    paths = []
    for i in range(3):
        p = tmp_path / f"{i:03d}.jpg"
        p.write_bytes(make_image_bytes("JPEG", "RGB", (100, 140)))
        paths.append(p)
    out = tmp_path / "book.pdf"
    assert write_images_to_pdf(paths, out, title="book") == 3
    assert len(PdfReader(str(out)).pages) == 3


def test_large_stream_roundtrip(image_dir, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """长内容流容易暴露 /Length 与偏移计算错误。"""
    from tests.conftest import make_image_bytes

    p = tmp_path / "big.jpg"
    p.write_bytes(make_image_bytes("JPEG", "RGB", (600, 900)))
    out = tmp_path / "many.pdf"
    with MiniPdfWriter(out) as pdf:
        for i in range(20):
            pdf.add_image_file(p, title=f"p{i}")
    reader = PdfReader(str(out))
    assert len(reader.pages) == 20
    assert len(reader.outline) == 20
    assert _mean_diff(_extract_image(reader.pages[19]), _expected(p)) < 1.0


def test_png_predictor_passthrough_is_actually_used(image_dir, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """确认 PNG 真的走了透传，而不是碰巧被别的方式解对了。

    如果哪天有人把 Predictor 去掉、改成先解码再压缩，这个测试会失败——
    那会带来明显的 CPU 开销（每页几十毫秒 → 几百毫秒）。
    """
    from tests.conftest import make_image_bytes

    p = tmp_path / "x.png"
    p.write_bytes(make_image_bytes("PNG", "RGB", (64, 64)))
    src_idat = b"".join(
        chunk[8 : 8 + int.from_bytes(chunk[:4], "big")]
        for chunk in _iter_chunks(p.read_bytes())
        if chunk[4:8] == b"IDAT"
    )
    out = tmp_path / "x.pdf"
    with MiniPdfWriter(out) as pdf:
        pdf.add_image_file(p)

    xobj = PdfReader(str(out)).pages[0]["/Resources"]["/XObject"]["/Im0"].get_object()
    assert xobj["/Filter"] == "/FlateDecode"
    assert int(xobj["/DecodeParms"]["/Predictor"]) == 15
    raw = xobj._data  # 未解压的原始流
    assert raw == src_idat, "IDAT 应当被原样透传"
    assert zlib.decompress(raw)  # 确认是合法的 zlib 流


def _iter_chunks(data: bytes):  # type: ignore[no-untyped-def]
    """遍历 PNG chunk（含 length/type/data/crc 完整切片）。"""
    i = 8
    while i + 8 <= len(data):
        length = int.from_bytes(data[i : i + 4], "big")
        yield data[i : i + 12 + length]
        if data[i + 4 : i + 8] == b"IEND":
            break
        i += 12 + length


@pytest.mark.parametrize("color", [(0, 0, 0, 0), (0, 255, 255, 0), (0, 0, 0, 255)])
def test_adobe_cmyk_pdf_decode_preserves_color(tmp_path, color):
    source = io.BytesIO()
    Image.new("CMYK", (20, 20), color).save(source, "JPEG")
    assert probe_bytes(source.getvalue()).adobe_transform == 0
    out = tmp_path / "cmyk.pdf"
    with MiniPdfWriter(out, dpi=72) as pdf:
        assert pdf.add_image_bytes(source.getvalue())
    reader = PdfReader(out)
    image = reader.pages[0]["/Resources"]["/XObject"]["/Im0"]
    assert image["/ColorSpace"] == "/DeviceCMYK"
    assert image["/Decode"] == [1, 0] * 4
    # pypdf applies PDF color interpretation; opening only the JPEG bypasses /Decode.
    got = reader.pages[0].images[0].image.convert("RGB")
    want = Image.open(source).convert("RGB")
    assert _mean_diff(got, want) < 1
    del image[NameObject("/Decode")]
    if color == (0, 0, 0, 0):
        assert _mean_diff(reader.pages[0].images[0].image.convert("RGB"), want) > 200


@pytest.mark.parametrize("mode", ["RGB", "L"])
def test_other_jpeg_colors_are_not_inverted(tmp_path, mode):
    source = io.BytesIO()
    Image.new(mode, (20, 20)).save(source, "JPEG")
    out = tmp_path / "ordinary.pdf"
    with MiniPdfWriter(out) as pdf:
        assert pdf.add_image_bytes(source.getvalue())
    image = PdfReader(out).pages[0]["/Resources"]["/XObject"]["/Im0"]
    assert "/Decode" not in image


def test_cmyk_without_adobe_marker_is_not_automatically_inverted(tmp_path):
    source = io.BytesIO()
    Image.new("CMYK", (20, 20)).save(source, "JPEG")
    data = source.getvalue()
    start = data.index(b"\xff\xee")
    end = start + 2 + int.from_bytes(data[start + 2 : start + 4], "big")
    data = data[:start] + data[end:]
    assert probe_bytes(data).adobe_transform is None
    out = tmp_path / "no-adobe.pdf"
    with MiniPdfWriter(out) as pdf:
        assert pdf.add_image_bytes(data)
    assert "/Decode" not in PdfReader(out).pages[0]["/Resources"]["/XObject"]["/Im0"]


@pytest.mark.parametrize("failure", ["title", "write", "interrupt"])
def test_enter_failure_closes_handle_and_discards_staging(tmp_path, monkeypatch, failure):
    out = tmp_path / "book.pdf"
    out.write_bytes(b"original")
    writer = MiniPdfWriter(out, title="\udcff" if failure == "title" else "book", overwrite=True)
    handles = []
    error = UnicodeEncodeError if failure == "title" else OSError
    if failure == "interrupt":
        error = KeyboardInterrupt
    original_raw = writer._raw

    def record_and_write(data):
        handles.append(writer._fh)
        original_raw(data)
        if failure != "title":
            raise error("simulated initialization failure")
        return 0

    monkeypatch.setattr(writer, "_raw", record_and_write)
    with pytest.raises(error):
        with writer:
            pytest.fail("The body must not run after failed initialization")
    assert handles and all(handle.closed for handle in handles)
    assert writer._fh is None
    assert out.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [out]
