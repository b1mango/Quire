"""异常层次。

退出码语义见 DESIGN.md §20.1：
    1 参数/配置错误 · 2 网络失败 · 3 解析失败 · 4 部分成功 · 5 被反爬 · 6 缺依赖

纪律（DESIGN.md §17.1 ④）：禁止 ``except Exception: pass``。
每个 except 必须二选一——转成领域异常，或记入 report 并附完整上下文。
"""

from __future__ import annotations

from .utils.urls import redact


class QuireError(Exception):
    """所有领域异常的基类。``hint`` 是给用户看的下一步建议，不是堆栈。"""

    exit_code = 1

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


# ---------------------------------------------------------------- 退出码 1
class ConfigError(QuireError):
    """参数或配置错误。跑之前就该知道，属于 fail-fast 类别。"""

    exit_code = 1


# ---------------------------------------------------------------- 退出码 2
class FetchError(QuireError):
    """网络层失败。"""

    exit_code = 2


class HttpStatusError(FetchError):
    def __init__(self, url: str, status: int, *, hint: str | None = None) -> None:
        super().__init__(f"HTTP {status} — {redact(url)}", hint=hint)
        self.url = url
        self.status = status


class FetchTimeoutError(FetchError):
    def __init__(self, url: str, seconds: float, *, hint: str | None = None) -> None:
        super().__init__(f"请求超时（{seconds:g}s）— {redact(url)}", hint=hint)
        self.url = url
        self.seconds = seconds


class NetworkError(FetchError):
    """连不上、DNS 失败、连接重置等。"""


# ---------------------------------------------------------------- 退出码 5
class BlockedError(FetchError):
    """被反爬拦截。单独一类是因为它的处置方式完全不同（升级渲染后端）。"""

    exit_code = 5


# ---------------------------------------------------------------- 退出码 3
class ParseError(QuireError):
    """解析失败。"""

    exit_code = 3


class NoCatalogueError(ParseError):
    """找不到章节列表。"""

    def __init__(self, url: str, *, hint: str | None = None) -> None:
        super().__init__(
            f"这个链接没找到章节列表：{url}",
            hint=hint or "试试直接给章节页的地址，或用 `quire inspect` 看看页面结构。",
        )
        self.url = url


class NoContentError(ParseError):
    """抽不到正文。"""

    def __init__(self, url: str, *, hint: str | None = None) -> None:
        super().__init__(
            f"抽不到正文：{url}",
            hint=hint or "该页可能是图片正文，试试 `--ocr always`，或为本站写一条规则。",
        )
        self.url = url


class NoImagesError(ParseError):
    """页面上没找到任何可用的图片。"""

    def __init__(self, url: str, *, hint: str | None = None) -> None:
        super().__init__(
            f"没找到可下载的图片：{redact(url)}",
            hint=hint or "用 `quire inspect --explain` 看每张图被过滤的原因。",
        )
        self.url = url


# ---------------------------------------------------------------- 退出码 6
class DependencyError(QuireError):
    """缺少可选依赖或离线缺模型。"""

    exit_code = 6


class UnsupportedError(QuireError):
    """明确不支持的场景（JS 逆向解密等），见 DESIGN.md §22.2。"""

    exit_code = 6


# ---------------------------------------------------------------- 退出码 4
class PartialResult(Exception):  # noqa: N818 - 它不是错误，是"带警告的成功"
    """部分成功：产物可用，但有洞。

    当前实现里它只作为语义标记与类型存在，正常流程**不抛**它——
    缺页信息由 ``Report`` 承载，退出码在 CLI 层根据 Report 决定。
    保留这个类型是为了让"退出码 4"有一个可引用的名字。
    """

    exit_code = 4
