from __future__ import annotations

import pytest

from quire import cli_ui
from quire.errors import UnsupportedError


class _FakeServer:
    def __init__(self) -> None:
        self.url = "http://127.0.0.1:1/?token=fake"
        self.closed = False

    def serve_forever(self) -> None:
        raise KeyboardInterrupt

    def server_close(self) -> None:
        self.closed = True


def test_cmd_ui_starts_and_stops(monkeypatch, tmp_path):
    fake = _FakeServer()
    made = {}
    monkeypatch.setattr(cli_ui, "module_available", lambda name: True)
    monkeypatch.setattr(cli_ui, "data_home", lambda: tmp_path)

    import quire.server.app as app

    def fake_make(host, port, *, data_root):
        made["host"] = host
        made["port"] = port
        made["data_root"] = data_root
        return fake

    monkeypatch.setattr(app, "make_server", fake_make)
    opened = []
    monkeypatch.setattr(cli_ui.webbrowser, "open", opened.append)

    class Args:
        port = 0
        no_browser = False

    assert cli_ui.cmd_ui(Args()) == 0
    assert made["host"] == "127.0.0.1"
    assert made["data_root"] == tmp_path
    assert opened == [fake.url]
    assert fake.closed


def test_cmd_ui_requires_core(monkeypatch):
    monkeypatch.setattr(cli_ui, "module_available", lambda name: False)
    with pytest.raises(UnsupportedError):
        cli_ui.cmd_ui(type("Args", (), {"port": 0, "no_browser": True})())


def test_cmd_ui_stops_when_shell_dies(monkeypatch, tmp_path):
    import time

    import quire.server.app as app

    class Fake:
        url = "http://127.0.0.1:1/?token=fake"

        def __init__(self) -> None:
            self.shut_down = False

        def serve_forever(self) -> None:
            while not self.shut_down:
                time.sleep(0.05)

        def shutdown(self) -> None:
            self.shut_down = True

        def server_close(self) -> None:
            pass

    fake = Fake()
    monkeypatch.setattr(cli_ui, "module_available", lambda name: True)
    monkeypatch.setattr(cli_ui, "data_home", lambda: tmp_path)
    monkeypatch.setattr(app, "make_server", lambda host, port, *, data_root: fake)
    monkeypatch.setattr(cli_ui.webbrowser, "open", lambda url: None)
    # macOS 最大 pid 为 99998，该进程号必然不存在
    monkeypatch.setenv("QUIRE_SHELL_PID", "999999")

    args = type("Args", (), {"port": 0, "no_browser": True})()
    assert cli_ui.cmd_ui(args) == 0
    assert fake.shut_down
