"""图片型 PDF 当漫画读、条漫判定与条漫窄切片保留。"""

from __future__ import annotations

import io

from PIL import Image
from pypdf import PdfWriter

from quire.image.probe import ImageProbe
from quire.parse.images import Candidate, FilterPolicy, postfilter
from quire.reader import comic, pdfpages


def _image_pdf(sizes) -> io.BytesIO:
    images = [Image.new("RGB", size, (40, 80, 120)) for size in sizes]
    buffer = io.BytesIO()
    images[0].save(buffer, "PDF", save_all=True, append_images=images[1:])
    buffer.seek(0)
    return buffer


def test_image_pdf_is_read_as_pages_and_strip_detected():
    strip = _image_pdf([(400, 960)] * 6)
    assert pdfpages.inspect(strip) == (6, True)
    data, content_type = pdfpages.read_page(strip, 3)
    assert content_type == "image/jpeg" and data[:3] == b"\xff\xd8\xff"
    assert pdfpages.inspect(_image_pdf([(800, 1150)] * 3)) == (3, False)  # 普通漫画页


def test_text_or_broken_pdf_stays_with_system_viewer():
    writer = PdfWriter()
    writer.add_blank_page(300, 400)
    buffer = io.BytesIO()
    writer.write(buffer)
    buffer.seek(0)
    assert pdfpages.inspect(buffer) == (0, False)  # 没有图片页
    assert pdfpages.inspect(io.BytesIO(b"%PDF-1.4 broken")) == (0, False)


def test_cbz_strip_detection(tmp_path):
    import zipfile

    path = tmp_path / "s.cbz"
    with zipfile.ZipFile(path, "w") as archive:
        for n in range(4):
            out = io.BytesIO()
            Image.new("RGB", (300, 800)).save(out, "JPEG")
            archive.writestr(f"{n:03}.jpg", out.getvalue())
    assert comic.is_strip(path)


def test_strip_slices_survive_postfilter_only_at_strip_width():
    thin = Candidate("https://c.test/1.jpg", 0)
    probe = ImageProbe(format="jpeg", width=690, height=8, complete=True)
    kept, _ = postfilter([(thin, probe, 100)], FilterPolicy(strip_width=690))
    assert kept == [thin]
    kept, rejected = postfilter([(thin, probe, 100)], FilterPolicy())
    assert not kept and "尺寸过小" in rejected[0].reason
    banner = ImageProbe(format="jpeg", width=728, height=90, complete=True)
    assert not postfilter([(thin, banner, 100)], FilterPolicy(strip_width=690))[0]
