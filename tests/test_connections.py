"""The two connections of the app: the server on the workstation and a test server on this computer."""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core  # noqa: E402

MAIN, TEST = "http://workstation.example:8765", "http://127.0.0.1:8765"


class Vault:
    """The Credential Manager, in memory."""

    def __init__(self):
        self.store = {}

    def get(self, name):
        return self.store.get(name)

    def put(self, name, secret):
        self.store[name] = secret

    def delete(self, name):
        self.store.pop(name, None)


@pytest.fixture
def app(monkeypatch):
    vault, started, saved = Vault(), [], []
    monkeypatch.setattr(core.services, "credentials", vault)
    monkeypatch.setattr(core, "save_settings", lambda s: saved.append(dict(s)))
    monkeypatch.setattr(core.local_server, "ensure", started.append)
    devices = iter(range(1, 10))
    monkeypatch.setattr(core.server_api, "register",
                        lambda url, key, name: {"token": f"token-{key}", "device_id": f"device-{next(devices)}"})
    c = core.Core.__new__(core.Core)
    c.settings, c.session = {}, None
    c.delivery = SimpleNamespace(client=None, reachable=None, server_down_until=0.0, wake=threading.Event(),
                                 sending=lambda: False, connection=lambda: "connecting")
    c.profile = SimpleNamespace(status="local", data={"user_name": "N"})
    return SimpleNamespace(core=c, vault=vault, started=started)


def test_a_code_fills_the_connection_its_address_belongs_to(app):
    c = app.core
    assert c.connect_server(f"FLX3-a@{TEST}") == ""
    assert app.vault.store == {"server-test": "token-FLX3-a"} and c.delivery.client.url == TEST
    assert c.connect_server(f"FLX3-b@{MAIN}/") == ""
    assert app.vault.store == {"server-test": "token-FLX3-a", "server": "token-FLX3-b"}
    assert c.settings["server_url"] == MAIN and c.settings["device_id"] == "device-2"
    info = c.server_info()
    assert info["active"] == "main" and [s["kind"] for s in info["servers"]] == ["main", "test"]
    assert "token" not in str(info)


def test_switching_keeps_both_and_starts_the_test_server(app):
    c = app.core
    c.connect_server(f"FLX3-a@{TEST}")
    c.connect_server(f"FLX3-b@{MAIN}")
    app.started.clear()
    assert c.use_server("test") == ""
    assert c.settings["server_url"] == TEST and c.settings["device_id"] == "device-1"
    assert c.delivery.client.url == TEST and app.started == [TEST]
    assert c.use_server("main") == "" and c.delivery.client.url == MAIN
    assert c.use_server("other") != ""


def test_no_switch_while_a_recording_is_partly_on_the_server(app):
    c = app.core
    c.connect_server(f"FLX3-a@{TEST}")
    c.connect_server(f"FLX3-b@{MAIN}")
    c.delivery.sending = lambda: True
    assert "zum Teil" in c.use_server("test") and c.settings["server_url"] == MAIN
    assert c.use_server("main") == ""                     # the one in use: nothing changes


class Starting:
    """A server that answers only after a few tries, like the test server while it starts."""

    def __init__(self, tries, profile=None):
        self.tries, self.profile = tries, profile

    def health(self):
        self.tries -= 1
        if self.tries > 0:
            raise OSError("connection refused")
        if self.profile is not None:              # the uploader syncs the profile once the server answers
            self.profile.status = "online"


def test_after_a_switch_the_window_waits_for_the_server_and_its_profile(app, monkeypatch):
    c = app.core
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    c.delivery.client = Starting(3, c.profile)
    assert c.await_server() == "" and c.profile.status == "online"
    assert c.delivery.wake.is_set() and c.delivery.client.tries == 0


def test_a_server_that_never_answers_ends_the_wait(app, monkeypatch):
    c = app.core
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(core.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    c.delivery.client = Starting(10 ** 6)
    assert "antwortet noch nicht" in c.await_server(timeout=45)
    c.delivery.client = None
    assert c.await_server() == ""                         # nothing to wait for without a server


def test_disconnecting_forgets_only_the_connection_in_use(app):
    c = app.core
    c.connect_server(f"FLX3-a@{TEST}")
    c.connect_server(f"FLX3-b@{MAIN}")
    assert c.disconnect_server() == ""
    assert app.vault.store == {"server-test": "token-FLX3-a"} and "server_url" not in c.settings
    assert c.delivery.client is None and c.server_info()["active"] == ""
    assert c.use_server("test") == "" and c.delivery.client.url == TEST


def test_the_one_connection_of_before_becomes_the_test_server(app):
    app.vault.put("server", "old-token")
    s = {"server_url": TEST, "device_id": "laptop"}
    core._move_connection_into_servers(s)
    assert s["servers"] == {"test": {"url": TEST, "device_id": "laptop"}}
    assert app.vault.store == {"server-test": "old-token"}
    assert core.server_client(s).url == TEST
    core._move_connection_into_servers(s)                 # once only
    assert app.vault.store == {"server-test": "old-token"}
