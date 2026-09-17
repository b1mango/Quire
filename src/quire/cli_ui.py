"""``quire ui``：启动本地 Web UI（项目设计.md §3.3、§7）。

只绑定回环地址，随机端口 + 随机令牌；令牌只出现在本机终端与
本次启动的浏览器地址里，服务停止即失效。
"""

from __future__ import annotations

import argparse
import webbrowser

from .cli_console import EXIT_OK, data_home, info, module_available
from .errors import UnsupportedError


def cmd_ui(args: argparse.Namespace) -> int:
    if not all(
        module_available(name)
        for name in ("httpx", "PIL", "pypdf", "websockets", "quire.core_manga")
    ):
        raise UnsupportedError("Web UI 需要 core 能力", hint="安装 quire-local[core]")
    from .server.app import make_server

    server = make_server("127.0.0.1", args.port, data_root=data_home())
    info(f"卷帙 Web UI：{server.url}")
    info("此地址含本次启动的访问令牌；按 Ctrl+C 停止。")
    if not args.no_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        info("已停止")
    finally:
        server.server_close()
    return EXIT_OK
