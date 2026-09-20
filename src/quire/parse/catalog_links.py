"""Explicit cross-site reader links allowed in known public catalogs."""

import re
from urllib.parse import urlsplit


def is_linked_reader(base_url: str, url: str) -> bool:
    target = urlsplit(url)
    return (
        urlsplit(base_url).hostname in {"www.gugu5.cc", "gugu5.cc"}
        and target.hostname == "ac.qq.com"
        and bool(re.fullmatch(r"/ComicView/index/id/\d+/cid/\d+", target.path))
    )
