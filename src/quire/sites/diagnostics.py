"""Explain rule matches and suggest selectors from actual local DOM structure."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict
from typing import Any

from ..parse.article import extract_article, validate_article
from ..parse.chapters import discover_chapters
from ..parse.images import collect, prefilter
from ..parse.minidom import Document
from ..utils.urls import redact
from .rules import SiteRule


def suggestions(doc: Document) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for anchor in doc.select("a[href]"):
        node = anchor.parent
        if node is None:
            continue
        classes = sorted(c for c in node.classes if re.fullmatch(r"[A-Za-z_][\w-]*", c))
        if classes:
            counts[f"{node.tag}.{classes[0]} a"] += 1
        elif node.get("id") and re.fullmatch(r"[A-Za-z_][\w-]*", node.get("id") or ""):
            counts[f"#{node.get('id')} a"] += 1
    return [{"selector": key, "matches": count} for key, count in counts.most_common(3)]


def diagnose(doc: Document, url: str, rule: SiteRule | None = None) -> dict[str, Any]:
    links = discover_chapters(doc, url, selector=rule.chapter_links if rule else None)
    candidates = collect(
        doc,
        doc.effective_base() or url,
        selector=rule.image_selector if rule else None,
        attrs=rule.image_attrs if rule else None,
    )
    kept, rejected = prefilter(candidates)
    article_ok = False
    if rule and rule.kind == "novel":
        article_ok = validate_article(extract_article(doc, selector=rule.content_selector))[0]
    warnings = []
    if rule and rule.chapter_links and not links:
        warnings.append(
            "chapter_links 命中 0；规则可能已过期。运行 inspect --dump-html 查看页面并调整选择器。"
        )
    if not links and not kept and not article_ok:
        warnings.append("没有可用内容；检查选择器，动态页使用 --render。")
    return {
        "title": doc.title,
        "rule": rule.name if rule else None,
        "kind": rule.kind if rule else ("series" if len(links) >= 2 else "manga"),
        "granularity": "series" if len(links) >= 2 else "chapter",
        "chapters": len(links),
        "images": len(kept),
        "text_valid": article_ok,
        "samples": ([redact(c.url) for c in links[:5]] or [redact(c.url) for c in kept[:5]]),
        "rejections": [{**asdict(r), "url": redact(r.url)} for r in rejected],
        "suggestions": suggestions(doc) if not links else [],
        "warnings": warnings,
    }
