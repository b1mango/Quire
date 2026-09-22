"""parse/packed.py:看漫画手机版打包脚本的解包与图片清单提取。"""

from __future__ import annotations

import pytest

from quire.fetch.simple import Response
from quire.manga import MangaOptions, run_manga
from quire.parse.packed import (
    _lz_decompress_base64,
    extract_script_images,
    extract_smh_reader_images,
)
from tests.mock_site.server import page_image

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="


def _lz_compress_base64(data: str) -> str:
    """lz-string compressToBase64 的测试侧移植,用于构造站点同款 fixture。"""
    dictionary: dict[str, int] = {}
    fresh: set[str] = set()
    word = ""
    enlarge_in, dict_size, num_bits = 2, 3, 2
    out: list[int] = []
    value = 0
    position = 0

    def emit(bit: int) -> None:
        nonlocal value, position
        value = (value << 1) | bit
        if position == 5:
            position = 0
            out.append(value)
            value = 0
        else:
            position += 1

    def emit_value(raw: int, bits: int) -> None:
        for _ in range(bits):
            emit(raw & 1)
            raw >>= 1

    def flush_pending(char: str) -> None:
        nonlocal enlarge_in, num_bits
        if char in fresh:
            code = ord(char)
            if code < 256:
                emit_value(0, num_bits)
                emit_value(code, 8)
            else:
                emit_value(1, num_bits)
                emit_value(code, 16)
            enlarge_in -= 1
            fresh.discard(char)
        else:
            emit_value(dictionary[char], num_bits)
        enlarge_in -= 1
        if enlarge_in == 0:
            enlarge_in = 1 << num_bits
            num_bits += 1

    for char in data:
        if char not in dictionary:
            dictionary[char] = dict_size
            dict_size += 1
            fresh.add(char)
        combined = word + char
        if combined in dictionary:
            word = combined
        else:
            if word:
                flush_pending(word)
            dictionary[combined] = dict_size
            dict_size += 1
            word = char
    if word:
        flush_pending(word)
    emit_value(2, num_bits)
    while True:
        value <<= 1
        if position == 5:
            out.append(value)
            break
        position += 1
    result = "".join(_ALPHABET[v] for v in out)
    padding = {0: "", 1: "===", 2: "==", 3: "="}[len(result) % 4]
    return result + padding


def _pack_page(payload: str, words: list[str]) -> str:
    blob = _lz_compress_base64("|".join(words))
    return (
        "<html><head><title>第01回</title></head><body><script>"
        f"window[\"\\x65\\x76\\x61\\x6c\"](function(p,a,c,k,e,d){{}}('{payload}',62,"
        f"{len(words)},'{blob}'['\\x73\\x70\\x6c\\x69\\x63']('\\x7c')))"
        "</script></body></html>"
    )


def test_lz_base64_round_trip():
    for text in ["SMH|reader|images|sl|preInit", "斗破苍穹|act_01|seemh-001-c8d2.jpg.webp"]:
        assert _lz_decompress_base64(_lz_compress_base64(text)) == text


def test_extracts_reader_image_list():
    payload = '0.1({"2":["/ps3/x/a.jpg","/ps3/x/b.jpg"],"3":{"e":111,"m":"sig"}}).4();'
    html = _pack_page(payload, ["SMH", "reader", "images", "sl", "preInit"])
    assert extract_smh_reader_images(html) == [
        "https://us.hamreus.com/ps3/x/a.jpg?e=111&m=sig",
        "https://us.hamreus.com/ps3/x/b.jpg?e=111&m=sig",
    ]


def test_extracts_and_percent_encodes_non_ascii_paths():
    payload = '0.1({"2":["/ps3/d/斗破苍穹/a.jpg"],"3":{"e":111,"m":"sig"}}).4();'
    html = _pack_page(payload, ["SMH", "reader", "images", "sl", "preInit"])
    (url,) = extract_smh_reader_images(html)
    assert "%E6%96%97%E7%A0%B4%E8%8B%8D%E7%A9%B9" in url


#: 测试侧 LZ 压缩器对部分字节流不能往返(结尾标记缺陷),这组词典实测可用。
#: 索引:files=0, path=1, imgData=2, sl=3, SMH=6。
_IMGDATA_WORDS = ["files", "path", "imgData", "sl", "qq", "b2", "SMH", "cb"]


def test_extracts_desktop_imgdata_variant():
    """桌面版章节页:SMH.imgData 的 files+path 拼在桌面 CDN 主机上。"""
    payload = (
        '6.2({"0":["seemh-001-aa11.JPG.webp","seemh-002-bb22.JPG.webp"],'
        '"1":"/ps1/g/x/07/","3":{"e":111,"m":"sig"}});'
    )
    html = _pack_page(payload, _IMGDATA_WORDS)
    assert extract_smh_reader_images(html) == [
        "https://eu.hamreus.com/ps1/g/x/07/seemh-001-aa11.JPG.webp?e=111&m=sig",
        "https://eu.hamreus.com/ps1/g/x/07/seemh-002-bb22.JPG.webp?e=111&m=sig",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        '6.2({"0":["a.JPG.webp"],"1":"/ps1/"});',  # 缺 sl 签名
        '6.2({"0":"a.JPG.webp","1":"/ps1/","3":{"e":111,"m":"sig"}});',  # files 不是数组
        '6.2({"0":["a.JPG.webp"],"1":"ps1/","3":{"e":111,"m":"sig"}});',  # path 非 / 开头
    ],
)
def test_desktop_imgdata_malformed_returns_empty(payload):
    assert extract_smh_reader_images(_pack_page(payload, _IMGDATA_WORDS)) == []


@pytest.mark.parametrize(
    "html",
    [
        "",
        "<html><body><img src='/1.jpg'></body></html>",  # 无打包脚本
        _pack_page(
            '0.1({"2":"/ps3/x/a.jpg","3":{"e":111,"m":"sig"}}).4();',
            ["SMH", "reader", "images", "sl", "preInit"],
        ),  # images 不是数组
        _pack_page(
            '0.1({"2":["/ps3/x/a.jpg"]}).4();', ["SMH", "reader", "images", "sl", "preInit"]
        ),  # 缺 sl 签名
        "eval(function(p,a,c,k,e,d){}('junk',62,5,'!!!not-base64!!!'['splic']('|')))",
    ],
)
def test_malformed_or_absent_payload_returns_empty(html):
    assert extract_smh_reader_images(html) == []


def test_manga_falls_back_to_packed_script_images(tmp_path):
    payload = '0.1({"2":["/ps3/x/a.jpg","/ps3/x/b.jpg"],"3":{"e":111,"m":"sig"}}).4();'
    page = _pack_page(payload, ["SMH", "reader", "images", "sl", "preInit"])

    class FakeFetcher:
        def get(self, url, **kwargs):
            content = (
                page.encode()
                if url.endswith("/chapter")
                else page_image(1 if url.endswith("a.jpg?e=111&m=sig") else 2)
            )
            return Response(url, 200, {}, content, 0)

    result = run_manga(
        "https://m.manhuagui.example/chapter",
        tmp_path / "book.pdf",
        fetcher=FakeFetcher(),
        options=MangaOptions(rate=1000, retries=0, concurrency=2),
    )
    assert result.pages_written == 2
    assert result.pages_failed == 0


def test_extracts_nuxt_page_url_list():
    html = (
        "<html><body><img src='https://static.example.com/pre.cur'>"
        "<script>data:{chapterInfo:{chapter_id:192726,page_url:["
        '"https:\\u002F\\u002Fimg.example.com\\u002Fc\\u002F01.jpg?sign=a",'
        '"https:\\u002F\\u002Fimg.example.com\\u002Fc\\u002F02.jpg?sign=b",'
        '"https:\\u002F\\u002Fimg.example.com\\u002Fc\\u002F03.jpg?sign=c"'
        "]}}}</script></body></html>"
    )
    assert extract_script_images(html) == [
        "https://img.example.com/c/01.jpg?sign=a",
        "https://img.example.com/c/02.jpg?sign=b",
        "https://img.example.com/c/03.jpg?sign=c",
    ]


def test_page_url_list_ignores_malformed_and_picks_longest():
    html = (
        'page_url:["not-a-url",]'
        ' page_url:["https://img.example.com/a.jpg",'
        '"https://img.example.com/b.jpg"]'
    )
    assert extract_script_images(html) == [
        "https://img.example.com/a.jpg",
        "https://img.example.com/b.jpg",
    ]


def test_script_list_wins_over_cursor_noise(tmp_path):
    html = (
        "<html><head><title>第1回</title></head><body>"
        "<img src='https://static.example.com/next.cur'>"
        "<img src='https://img.example.com/c/02.jpg?sign=b'>"
        "<script>x={page_url:["
        '"https://img.example.com/c/01.jpg?sign=a",'
        '"https://img.example.com/c/02.jpg?sign=b",'
        '"https://img.example.com/c/03.jpg?sign=c"'
        "]}</script></body></html>"
    )

    class FakeFetcher:
        def get(self, url, **kwargs):
            content = html.encode() if url.endswith("/chapter") else page_image(1)
            return Response(url, 200, {}, content, 0)

    result = run_manga(
        "https://www.zaimanhua.example/chapter",
        tmp_path / "book.pdf",
        fetcher=FakeFetcher(),
        options=MangaOptions(rate=1000, retries=0, concurrency=2),
    )
    assert result.pages_written == 3
    assert result.pages_failed == 0
