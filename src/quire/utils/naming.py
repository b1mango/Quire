"""命名与排序。纯函数，零依赖，可 100% 单测（DESIGN.md §17.1 ①）。

两件事最容易毁掉可用性，都在这里：
  1. 文件名清洗不干净 → 导出时报错或产生诡异的文件名；
  2. 自然排序做错 → 第 10 页排在第 2 页前面，整本书错乱。
"""

from __future__ import annotations

import re
from pathlib import Path

#: Windows 与 macOS 上都会出问题的字符，外加控制字符（在 safe_filename 里单独处理）。
_UNSAFE_CHARS = '<>:"/\\|?*'

#: 这些名字在 Windows 上保留，macOS 上虽合法但换机器就炸，统一规避。
_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_DIGITS = re.compile(r"(\d+)")


def safe_filename(name: str, *, max_len: int = 100, default: str = "untitled") -> str:
    """把任意标题清洗成能安全落盘的文件名（不含目录部分）。

    * 替换非法字符为全角同形字（``/`` → ``／``），而不是删掉——
      删掉会让"第1/2话"变成"第12话"，语义都变了。
    * 折叠连续空白、去掉首尾空白与点号（macOS 上尾随点会被静默吞掉）。
    * 超长按**字符**截断（中文标题按字节截会切出半个字）。
    """
    if not name:
        return default

    out = []
    for ch in name:
        if ch in _UNSAFE_CHARS:
            # 用全角同形字替换，保留视觉语义
            out.append(chr(ord(ch) + 0xFEE0))
        elif _CTRL.match(ch):
            out.append(" ")
        else:
            out.append(ch)
    s = "".join(out)

    s = re.sub(r"\s+", " ", s).strip().strip(".")

    s = s[:max_len].rstrip().rstrip(".")
    while len(s.encode("utf-8")) > 220:
        s = s[:-1]

    if not s:
        return default
    if s.upper() in _RESERVED:
        s = f"_{s}"
    return s


def natural_key(text: str) -> tuple[tuple[int, int, str], ...]:
    """自然排序键：``p2.jpg`` < ``p10.jpg``。

    返回可比较的元组，数字段用 ``(0, int, "")``，文本段用 ``(1, 0, str)``，
    保证 "" 与数字比较时不抛 TypeError。
    """
    parts = []
    for chunk in _DIGITS.split(text):
        if not chunk:
            continue
        if chunk.isdigit():
            # 前导零长的排后面（001 < 01 的直觉相反，这里按数值+原串定序）
            parts.append((0, int(chunk), chunk))
        else:
            parts.append((1, 0, chunk))
    return tuple(parts)


def pad_width(total: int) -> int:
    """卷号补零位宽。5 卷用 1 位，30 卷用 2 位，120 卷用 3 位。"""
    return max(1, len(str(max(1, total))))


def volume_label(index: int, total: int) -> str:
    """卷标签：``第01卷``。位宽按总卷数决定，保证字典序 == 阅读序。"""
    return f"第{index:0{pad_width(total)}d}卷"


def unique_path(path: Path) -> Path:
    """目标已存在时返回 ``name (1).ext``，依次递增。

    不用时间戳：用户要的是"再存一份"，不是"一堆看不懂的文件名"。
    """
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(1, 1000):
        candidate = path.with_name(f"{stem} ({i}){suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"同名文件过多：{path}")


def image_filename(index: int, ext: str, *, total: int = 0) -> str:
    """页图片的落盘名：``001.jpg``。位宽随总页数走，保证字典序正确。"""
    width = max(3, pad_width(total)) if total else 3
    ext = ext if ext.startswith(".") else f".{ext}"
    return f"{index:0{width}d}{ext}"
