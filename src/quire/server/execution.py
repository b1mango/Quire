"""任务执行线程的启动（自 jobs.py 拆出，守住模块行数门禁，同 files.py 惯例）。

``Thread.start`` 本身失败（如系统线程耗尽）时线程体根本不会运行，
执行槽必须在这里释放并如实落定，否则槽位永久占用、队列停摆。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .job_state import Job
    from .settings import UiSettings

_LOG = logging.getLogger(__name__)


def launch(
    job: Job,
    settings: UiSettings,
    thread_main: Callable[[Job, UiSettings], None],
    release: Callable[[Job], None],
    settle: Callable[..., None],
    pump: Callable[[], None],
) -> None:
    """启动任务执行线程；``start`` 失败时释放槽位、如实落定并补泵。"""
    try:
        threading.Thread(
            target=thread_main, args=(job, settings), daemon=True, name=f"quire-{job.id}"
        ).start()
    except Exception as exc:
        _LOG.exception("job %s thread failed to start", job.id)
        release(job)
        settle(job, "failed", f"任务失败：{type(exc).__name__}")
        pump()
