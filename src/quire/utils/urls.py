"""URL 处理。纯函数，零依赖，可 100% 单测（项目设计.md §17.1 ①）。

这里是"成功率"的第一道关：页面里的图片地址有十几种写法——
相对路径、协议相对、带 query 的、带 hash 的、CSS 里 url() 包着的、
data: 内联的、javascript: 的。归一化做不干净，后面全盘皆输。
"""

from __future__ import annotations

import posixpath
import re
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

#: 这些 scheme 不是"可下载的资源"，必须尽早剔除，否则会污染候选集。
BAD_SCHEMES = frozenset({"data", "javascript", "mailto", "about", "blob", "tel", "sms"})

IMAGE_EXTS = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif", ".jxl", ".jfif", ".pjpeg"}
)

#: content-type → 扩展名。用于 URL 没有扩展名时兜底。
CT_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/pjpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/avif": ".avif",
    "image/jxl": ".jxl",
    "image/svg+xml": ".svg",
}

_WS = re.compile(r"\s+")


def is_usable_url(url: str | None) -> bool:
    """这个字符串能不能当资源地址用。"""
    if not url:
        return False
    s = url.strip()
    if not s or s.startswith("#"):
        return False
    try:
        parts = urlsplit(s)
        if parts.scheme and parts.scheme.lower() not in {"http", "https"}:
            return False
        if parts.scheme and not parts.hostname:
            return False
        return parts.username is None and parts.password is None
    except ValueError:
        return False


def normalize_url(url: str) -> str:
    """去掉 fragment 与首尾空白；保留 query（很多图床把签名放在 query 里）。

    刻意**不**做 path 的 normpath——有些站点的路径大小写敏感，
    且 ``/a/../b`` 未必等价于 ``/b``。只做无风险的清理。
    """
    s = url.strip().replace(" ", "%20")
    if not s:
        return ""
    parts = urlsplit(s)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def join_url(base: str, rel: str) -> str:
    """把页面里的相对地址解析成绝对地址。

    比裸 ``urljoin`` 多处理两件事：
      * ``rel`` 本身已是绝对地址时原样返回（含 query 的场景）；
      * 结果是 ``//host/path`` 时补上 base 的 scheme。
    """
    rel = rel.strip()
    if not rel:
        return ""
    if not is_usable_url(rel):
        return ""

    joined = urljoin(base, rel)
    if joined.startswith("//"):
        joined = f"{urlsplit(base).scheme or 'https'}:{joined}"
    return normalize_url(joined)


def route_fragment(url: str) -> str:
    """Hash 路由是文档身份；普通 #heading 仍只是页内锚点。"""
    fragment = urlsplit(url).fragment
    return fragment if fragment.startswith(("/", "!/")) else ""


def join_document_url(base: str, rel: str) -> str:
    """章节导航保留 SPA 路由；图片资源继续使用 join_url。"""
    rel = rel.strip()
    if rel.startswith(("#/", "#!/")):
        rel = urljoin(base, rel)
    joined = join_url(base, rel)
    fragment = route_fragment(rel) if joined else ""
    return joined + ("#" + fragment if fragment else "")


def host_of(url: str) -> str:
    """取主机名（小写，去端口）。"""
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:  # 畸形 URL（比如 IPv6 括号不配对）
        return ""


def same_host(a: str, b: str) -> bool:
    """同站判断。用于"下一页"遍历时防止跑飞出去。"""
    ha, hb = host_of(a), host_of(b)
    if not ha or not hb:
        return False
    if ha == hb:
        return True
    # www 前缀不算换站
    return ha.removeprefix("www.") == hb.removeprefix("www.")


def path_ext(url: str) -> str:
    """取 URL 路径部分的扩展名（小写，含点）。没有则返回空串。"""
    path = urlsplit(url).path
    name = posixpath.basename(unquote(path))
    dot = name.rfind(".")
    if dot <= 0:
        return ""
    ext = name[dot:].lower()
    return ext if len(ext) <= 6 else ""


def is_image_url(url: str) -> bool:
    """URL 看起来像图片吗。只看扩展名，不联网。"""
    return path_ext(url) in IMAGE_EXTS


def guess_ext(url: str, content_type: str | None = None) -> str:
    """推断落盘扩展名：先信 URL，再信 content-type，最后默认 .jpg。

    默认 .jpg 而不是 .bin 是因为漫画站 99% 是 JPEG；
    而 probe 会在校验阶段修正错误的猜测（见 image/probe.py）。
    """
    ext = path_ext(url)
    if ext in IMAGE_EXTS:
        return ".jpg" if ext == ".jfif" else ext
    if content_type:
        ct = content_type.split(";")[0].strip().lower()
        if ct in CT_TO_EXT:
            return CT_TO_EXT[ct]
    return ".jpg"


def redact(url: str) -> str:
    """用于日志：抹掉 query，避免把 token 写进日志（项目设计.md §20.2）。"""
    try:
        parts = urlsplit(url)
        host = parts.netloc.rsplit("@", 1)[-1]
        return urlunsplit((parts.scheme, host, parts.path, "..." if parts.query else "", ""))
    except ValueError:
        return "[invalid URL]"
