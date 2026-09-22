"""模块误放检测:URL 形态先验与 probe/提交阶段的明确报错。"""

from __future__ import annotations

import asyncio

import pytest

from quire.errors import ConfigError
from quire.server.endpoints import submit_job
from quire.server.probe import probe_url
from quire.sites.kind_guard import check_placement, expected_kind

MANGA_URLS = [
    "https://ac.qq.com/Comic/ComicInfo/id/531040",
    "https://ac.qq.com/ComicView/index/id/531040/cid/1",
    "https://www.manhuagui.com/comic/1234/",
    "https://m.manhuagui.com/comic/1234/",
    "https://www.mycomic.com/comic/1234",
    "https://www.dongmanmanhua.cn/BOY/x/list",
    "https://manga.bilibili.com/detail/mc1234",
]
NOVEL_URLS = [
    "https://quanben.io/n/xiangzilidedaming/list.html",
    "https://www.hetushu.com/book/5763/index.html",
    "https://t.shuqi.com/book/1234.html",
    "https://www.qidian.com/book/1234/",
]


@pytest.mark.parametrize("url", MANGA_URLS)
def test_manga_urls_detected(url: str) -> None:
    assert expected_kind(url) == "manga"
    with pytest.raises(ConfigError, match="漫画链接"):
        check_placement(url, "novel")
    check_placement(url, "manga")
    check_placement(url, None)


@pytest.mark.parametrize("url", NOVEL_URLS)
def test_novel_urls_detected(url: str) -> None:
    assert expected_kind(url) == "novel"
    with pytest.raises(ConfigError, match="小说链接"):
        check_placement(url, "manga")
    check_placement(url, "novel")
    check_placement(url, None)


def test_unknown_or_unrestricted_urls_pass() -> None:
    assert expected_kind("https://example.test/anything") is None
    assert expected_kind("https://ac.qq.com/") is None  # 腾讯首页不限于漫画
    check_placement("https://example.test/anything", "novel")
    check_placement("https://example.test/anything", "manga")


def test_probe_rejects_misplaced_url_before_fetch() -> None:
    """probe 阶段就报错,不发起任何网络请求。"""
    with pytest.raises(ConfigError, match="漫画链接"):
        asyncio.run(probe_url(MANGA_URLS[0], kind="novel", capture_mode="catalogue"))
    with pytest.raises(ConfigError, match="小说链接"):
        asyncio.run(probe_url(NOVEL_URLS[0], kind="manga", capture_mode="catalogue"))


def test_submit_job_rejects_misplaced_url() -> None:
    payload = {
        "kind": "novel",
        "url": MANGA_URLS[0],
        "title": "误放",
        "formats": ["txt"],
    }
    with pytest.raises(ConfigError, match="漫画链接"):
        submit_job(None, payload)  # type: ignore[arg-type]
