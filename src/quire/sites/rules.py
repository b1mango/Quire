"""Strict local TOML rules; no executable expressions or network access."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from ..errors import ConfigError
from ..models import MangaOptions, NovelOptions
from ..parse.minidom import SelectorError, parse


@dataclass(frozen=True, slots=True)
class SiteRule:
    name: str
    domains: tuple[str, ...]
    kind: str = "manga"
    chapter_links: str | None = None
    volume_selector: str | None = None
    image_selector: str | None = None
    image_attrs: tuple[str, ...] | None = None
    content_selector: str | None = None
    next_selector: str | None = None
    last_verified: str = ""
    order: str = "auto"
    remove: tuple[str, ...] = ()
    next_page: str | None = None
    chapter_order: str = "auto"

    def manga(self, options: MangaOptions) -> MangaOptions:
        return replace(
            options,
            selector=options.selector or self.image_selector,
            attrs=options.attrs or self.image_attrs,
            order=self.order if options.order == "auto" else options.order,
            remove=self.remove,
            next_selector=self.next_page,
        )

    def novel(self, options: NovelOptions) -> NovelOptions:
        return replace(
            options,
            content_selector=options.content_selector or self.content_selector,
            chapter_selector=options.chapter_selector or self.chapter_links,
            next_selector=options.next_selector or self.next_selector,
        )


def rule_path(root: Path, name: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,120}", name) or ".." in name:
        raise ConfigError("规则名仅允许小写字母、数字、点、横线和下划线")
    path = root / "sites" / f"{name}.toml"
    if path.is_symlink() or path.parent.is_symlink():
        raise ConfigError("规则路径不能是符号链接")
    return path


def _table(value: object, allowed: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) - allowed:
        raise ConfigError("规则含未知字段或表结构错误")
    return value


def _selector(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("选择器须为非空字符串")
    try:
        parse("").select(value)
    except SelectorError as exc:
        raise ConfigError(f"规则选择器无效：{exc}") from exc
    return value


def load_rule(root: Path, name: str) -> SiteRule:
    path = rule_path(root, name)
    try:
        if path.stat().st_size > 64 * 1024:
            raise ConfigError("规则文件超过 64 KiB")
        raw = tomllib.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"无法读取 TOML 规则：{name}") from exc
    data = _table(
        raw,
        {"version", "name", "domains", "kind", "last_verified", "catalogue", "chapter", "novel"},
    )
    if type(data.get("version")) is not int or data["version"] != 1:
        raise ConfigError("规则 version 必须为 1")
    domains = data.get("domains")
    if (
        not isinstance(domains, list)
        or not domains
        or any(
            not isinstance(d, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", d) for d in domains
        )
    ):
        raise ConfigError("domains 须为非空的小写域名列表（不含协议、端口和路径）")
    kind = data.get("kind", "manga")
    if not isinstance(kind, str) or kind not in {"manga", "novel"}:
        raise ConfigError("规则 kind 须为 manga 或 novel")
    verified = data.get("last_verified", "")
    if isinstance(verified, date):
        verified = verified.isoformat()
    if not isinstance(verified, str):
        raise ConfigError("last_verified 须为日期字符串")
    if verified:
        try:
            date.fromisoformat(verified)
        except ValueError as exc:
            raise ConfigError("last_verified 须为 YYYY-MM-DD") from exc
    catalogue = _table(data.get("catalogue", {}), {"chapter_links", "volumes", "order"})
    chapter_order = catalogue.get("order", "auto")
    if not isinstance(chapter_order, str) or chapter_order not in {"auto", "dom", "asc", "desc"}:
        raise ConfigError("catalogue.order 须为 auto/dom/asc/desc")
    manga = _table(
        data.get("chapter", {}),
        {"image_selector", "image_attrs", "remove", "order", "next_page", "headers"},
    )
    novel = _table(data.get("novel", {}), {"content", "next"})
    attrs = manga.get("image_attrs")
    if attrs is not None and (
        not isinstance(attrs, list)
        or not attrs
        or any(
            not isinstance(a, str) or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]*", a) for a in attrs
        )
    ):
        raise ConfigError("chapter.image_attrs 须为图片属性名列表")
    order = manga.get("order", "auto")
    if not isinstance(order, str) or order not in {"auto", "dom", "asc", "desc"}:
        raise ConfigError("chapter.order 须为 auto/dom/asc/desc")
    removed = manga.get("remove", [])
    if not isinstance(removed, list):
        raise ConfigError("chapter.remove 须为选择器列表")
    for selector in removed:
        _selector(selector)
    headers = _table(manga.get("headers", {}), {"Referer"})
    if headers and headers["Referer"] != "{page_url}":
        raise ConfigError("规则 Referer 仅支持 {page_url}，凭据请勿写入规则")
    return SiteRule(
        name,
        tuple(domains),
        str(kind),
        _selector(catalogue.get("chapter_links")),
        _selector(catalogue.get("volumes")),
        _selector(manga.get("image_selector")),
        tuple(attrs) if isinstance(attrs, list) else None,
        _selector(novel.get("content")),
        _selector(novel.get("next")),
        verified,
        order,
        tuple(removed),
        _selector(manga.get("next_page")),
        chapter_order,
    )


def resolve_rule(root: Path, url: str, name: str | None = None) -> SiteRule | None:
    host = urlsplit(url).hostname
    if name:
        rule = load_rule(root, name)
        if host not in rule.domains:
            raise ConfigError("指定规则的 domains 不包含此站点")
        return rule
    matches = [load_rule(root, p.stem) for p in sorted((root / "sites").glob("*.toml"))]
    matches = [rule for rule in matches if host in rule.domains]
    if len(matches) > 1:
        raise ConfigError("多个规则匹配此站点，请用 --site 指定")
    return matches[0] if matches else None


def new_rule(root: Path, name: str) -> Path:
    path = rule_path(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = f'''version = 1
domains = ["{name}"]
kind = "manga"
last_verified = ""

[catalogue]
chapter_links = ".chapter-list a"
# volumes = ".volume"  # 每卷的容器；标题取 h2/h3 或 data-title

[chapter]
image_selector = ".reader img"
# image_attrs = ["data-src", "src"]

# [novel]
# content = "#content"
# next = "a.next-page"
'''
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(content)
    except FileExistsError as exc:
        raise ConfigError("规则已存在，保留原文件") from exc
    return path
