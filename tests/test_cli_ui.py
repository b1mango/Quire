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
