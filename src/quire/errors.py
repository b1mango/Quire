"""异常层次。

退出码语义见 项目设计.md §20.1：
    1 参数/配置错误 · 2 网络失败 · 3 解析失败 · 4 部分成功 · 5 被反爬 · 6 缺依赖

纪律（项目设计.md §17.1 ④）：禁止 ``except Exception: pass``。
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
        super().__init__(f"HTTP {status}: {redact(url)}", hint=hint)
        self.url = url
        self.status = status


class NetworkError(FetchError):
    """连不上、DNS 失败、连接重置等。"""


class LedgerError(QuireError):
    """Local ledger, cache integrity or state transition failure."""

    exit_code = 2


# ---------------------------------------------------------------- 退出码 5
class BlockedError(FetchError):
    """站点拒绝或 robots 禁止访问；停止对应请求。"""

    exit_code = 5


# ---------------------------------------------------------------- 退出码 3
class ParseError(QuireError):
    """解析失败。"""

    exit_code = 3


class NoImagesError(ParseError):
    """页面上没找到任何可用的图片。"""

    def __init__(self, url: str, *, hint: str | None = None) -> None:
        super().__init__(
            f"没找到可下载的图片：{redact(url)}",
            hint=hint or "用 `quire inspect --explain` 看每张图被过滤的原因。",
        )
        self.url = url


# ---------------------------------------------------------------- 退出码 6


class UnsupportedError(QuireError):
    """明确不支持的场景（JS 逆向解密等），见 项目设计.md §22.2。"""

    exit_code = 6
