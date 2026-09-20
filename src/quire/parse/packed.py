"""脚本内嵌的整章图片清单提取(DOM 收集的兜底/补充)。

两种已知藏法:

* 看漫画系(m.manhuagui.com)手机版:Dean Edwards packer 的 eval 脚本,字典串
  先经 LZString.decompressFromBase64 再按 ``|`` 切分(站点自定义
  ``String.prototype.splic``),解包后得到
  ``SMH.reader({...,"images":[...],"sl":{"e":..,"m":".."}})``;
  图片地址 = CDN 主机 + 路径 + ``?e=..&m=..`` 签名(超时即换,必须现取现用)。
* Nuxt 系 SPA(在漫画 zaimanhua 等):内联 ``page_url:["...","..."]`` 数组,
  DOM 只渲染当前页,``\\u002F`` 转义交给 json 解析。

纯函数、零依赖;任何一步对不上都返回空表,不抛异常——
站点改版随时可能发生,不能把整次采集拖垮。
"""

from __future__ import annotations

import json
import re
from urllib.parse import quote

#: packer 调用尾部的四个参数:payload、进制、词条数、LZ/base64 字典串。
_PACKER = re.compile(
    r"\}\('((?:[^'\\]|\\.)*)',(\d{1,3}),(\d{1,4}),'([A-Za-z0-9+/=]{32,})'\[",
    re.S,
)

#: 手机版阅读器的图片 CDN。桌面版(i.hamreus.com)是另一套页面,不在此列。
_CDN_HOST = "https://us.hamreus.com"

_BASE64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"

_MAX_DICT_CHARS = 512 * 1024
_MAX_WORDS = 5000
_MAX_UNPACKED = 2 * 1024 * 1024


def _lz_decompress_base64(blob: str) -> str:
    """LZString.decompressFromBase64 的纯 Python 移植(6-bit 字典、32 重置)。"""
    if len(blob) > _MAX_DICT_CHARS:
        return ""
    try:
        data = [_BASE64_ALPHABET.index(char) for char in blob]
    except ValueError:
        return ""
    value, position, index = data[0], 32, 1

    def read_bits(count: int) -> int:
        nonlocal value, position, index
        bits, power = 0, 1
        while power != 1 << count:
            bit = value & position
            position >>= 1
            if position == 0:
                if index >= len(data):
                    raise IndexError("dictionary string exhausted")
                position, value, index = 32, data[index], index + 1
            bits |= (1 if bit else 0) * power
            power <<= 1
        return bits

    try:
        mode = read_bits(2)
        if mode == 0:
            first = chr(read_bits(8))
        elif mode == 1:
            first = chr(read_bits(16))
        else:
            return ""
        dictionary = {0: chr(0), 1: chr(1), 2: chr(2), 3: first}
        dict_size, num_bits, enlarge_in = 4, 3, 4
        result = [first]
        word = first
        while True:
            code = read_bits(num_bits)
            if code == 0:
                dictionary[dict_size] = chr(read_bits(8))
                dict_size += 1
                code = dict_size - 1
                enlarge_in -= 1
            elif code == 1:
                dictionary[dict_size] = chr(read_bits(16))
                dict_size += 1
                code = dict_size - 1
                enlarge_in -= 1
            elif code == 2:
                return "".join(result)
            if enlarge_in == 0:
                enlarge_in = 1 << num_bits
                num_bits += 1
            entry = dictionary.get(code)
            if entry is None:
                if code != dict_size:
                    return ""
                entry = word + word[0]
            result.append(entry)
            dictionary[dict_size] = word + entry[0]
            dict_size += 1
            enlarge_in -= 1
            word = entry
            if enlarge_in == 0:
                enlarge_in = 1 << num_bits
                num_bits += 1
    except IndexError:
        return ""


def _unescape_js(text: str) -> str:
    """只处理反斜杠转义,其余字节原样保留(避免 unicode_escape 弄坏中文)。"""

    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        if token.startswith(("x", "u")):
            try:
                return chr(int(token[1:], 16))
            except ValueError:
                return token
        return {"n": "\n", "r": "\r", "t": "\t"}.get(token, token)

    return re.sub(r"\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|.)", replace, text)


def _packer_key(radix: int, code: int) -> str:
    out = ""
    while code >= radix:
        code, rem = divmod(code, radix)
        out = (chr(rem + 29) if rem > 35 else _BASE36[rem]) + out
    rem = code
    return (chr(rem + 29) if rem > 35 else _BASE36[rem]) + out


def _unpack(payload: str, radix: int, count: int, words: list[str]) -> str:
    if not 2 <= radix <= 62 or not 1 <= count <= _MAX_WORDS or len(words) < count:
        return ""
    table = {}
    for code in range(count - 1, -1, -1):
        key = _packer_key(radix, code)
        table[key] = words[code] or key
    unpacked = re.sub(r"\b\w+\b", lambda m: table.get(m.group(0), m.group(0)), payload)
    return unpacked if len(unpacked) <= _MAX_UNPACKED else ""


def _reader_payload(unpacked: str) -> dict[str, object] | None:
    start = unpacked.find("SMH.reader(")
    if start < 0:
        return None
    brace = unpacked.find("{", start)
    if brace < 0:
        return None
    depth = 0
    for pos in range(brace, len(unpacked)):
        char = unpacked[pos]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(unpacked[brace : pos + 1])
                except ValueError:
                    return None
                return data if isinstance(data, dict) else None
    return None


def extract_smh_reader_images(html: str) -> list[str]:
    """从页面 HTML 提取看漫画手机版的整章图片地址,按页序返回。"""
    for match in _PACKER.finditer(html):
        payload, radix, count, blob = (
            match.group(1),
            int(match.group(2)),
            int(match.group(3)),
            match.group(4),
        )
        dictionary = _lz_decompress_base64(blob)
        if not dictionary:
            continue
        unpacked = _unpack(_unescape_js(payload), radix, count, dictionary.split("|"))
        if not unpacked:
            continue
        data = _reader_payload(unpacked)
        if data is None:
            continue
        images = data.get("images")
        sign = data.get("sl")
        if (
            not isinstance(images, list)
            or not images
            or not isinstance(sign, dict)
            or not isinstance(sign.get("e"), int)
            or not isinstance(sign.get("m"), str)
        ):
            continue
        urls = []
        for path in images[:2000]:
            if not isinstance(path, str) or not path.startswith("/"):
                break
            urls.append(f"{_CDN_HOST}{quote(path, safe='/')}?e={sign['e']}&m={sign['m']}")
        if urls:
            return urls
    return []


#: Nuxt/SSR 内联状态里的整章数组:``page_url:["https://..","https://.."]``。
_PAGE_URL_LIST = re.compile(r'page_url:\[((?:"(?:[^"\\]|\\.)*"(?:\s*,\s*)*)+)\]')


def extract_page_url_list(html: str) -> list[str]:
    """提取 Nuxt 内联状态里的 ``page_url`` 整章地址,取最长的一份。"""
    best: list[str] = []
    for match in _PAGE_URL_LIST.finditer(html):
        try:
            raw = json.loads("[" + match.group(1) + "]")
        except ValueError:
            continue
        urls = [u for u in raw if isinstance(u, str) and u.startswith(("http://", "https://"))]
        if len(urls) > len(best):
            best = urls
    return best[:2000]


def extract_script_images(html: str) -> list[str]:
    """脚本内嵌清单的统一入口,返回两种藏法中更长的一份。"""
    from .tencent import extract_tencent_images

    tencent = extract_tencent_images(html)
    if tencent:
        return tencent
    smh = extract_smh_reader_images(html)
    page_urls = extract_page_url_list(html)
    return smh if len(smh) >= len(page_urls) else page_urls
