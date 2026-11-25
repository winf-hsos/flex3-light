"""The modules on the server's page (since 2026-11-25) and what the app shows of its server.

The modules moved out of the app into the browser: the page changes the lecturer's profile on the
server, and the app takes the change over on its next sync, which it asks for when its window comes
to the front. Under its connection the app shows the server's name and version from /status.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import core  # noqa: E402
from flex3_light_server import profiles  # noqa: E402
from flex3_light_server.app import create_app  # noqa: E402
from lecturer_profile import Profile  # noqa: E402
from test_server_library import signed_in  # noqa: E402
from test_server_pipeline import AUTH, TOKENS  # noqa: E402


def call(client, auth, name, *args):
    r = client.post(f"/library/api/{name}", json=list(args), headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def test_modules_are_kept_on_the_page(tmp_path):
    client = TestClient(create_app(tmp_path / "server", TOKENS, start_worker=False))
    auth, _ = signed_in(client)
    assert call(client, auth, "data")["profile"] == []
    entry = {"id": None, "name": "  Digitalisierung und  Programmierung ", "code": "", "language": "de",
             "ilias_course_id": "123", "opencast_series_id": "", "terms": "LiFi, Claude Code\nLiFi"}
    assert call(client, auth, "save_module", entry) == ""
    d = call(client, auth, "data")
    [m] = d["profile"]
    assert d["modules"] == ["Digitalisierung und Programmierung"]
    assert (m["code"], m["terms"], m["ilias_course_id"]) == ("digitalisierung-und-programmierung", ["LiFi", "Claude Code"], "123")
    assert call(client, auth, "save_module", {**entry, "name": "digitalisierung und programmierung"}) \
        == "Ein Modul mit diesem Namen gibt es schon."
    assert call(client, auth, "save_module", {**entry, "name": ""}) == "Ein Modul braucht einen Namen."
    assert call(client, auth, "save_module", {**m, "name": "Statistik: Teil 1", "code": "Stat 1", "language": "english"}) == ""
    [m2] = call(client, auth, "data")["profile"]
    assert (m2["id"], m2["name"], m2["code"], m2["language"]) == (m["id"], "Statistik Teil 1", "stat-1", "de")
    assert m2["updated_at"] > m["updated_at"]
    call(client, auth, "delete_module", m["id"])
    assert call(client, auth, "data")["profile"] == []
    assert call(client, auth, "save_module", m2) == "Dieses Modul gibt es nicht mehr."
    # the tombstone stays, so a computer that still has the module does not bring it back
    stored = profiles.Store(tmp_path / "server" / "profiles").load("lecturer1")[0][m["id"]]
    assert stored["deleted"] is True


def test_a_module_made_on_the_page_reaches_the_app(tmp_path):
    client = TestClient(create_app(tmp_path / "server", TOKENS, start_worker=False))

    class Client:                                   # the app's ServerClient over the test client
        def get_profile(self):
            return client.get("/profile", headers=AUTH).json()

        def sync_profile(self, modules, preferences=None):
            return client.post("/profile/sync", json={"modules": modules, "preferences": preferences or {}},
                               headers=AUTH).json()

    app = Profile(tmp_path / "profile.json")
    app.sync(Client())
    assert not app.due()
    auth, _ = signed_in(client)
    call(client, auth, "save_module", {"name": "Applications of AI", "language": "en"})
    app.stale = True                                # the window came to the front
    assert app.due()
    app.sync(Client())
    assert [m["name"] for m in app.modules()] == ["Applications of AI"] and not app.due()


def test_status_names_the_server(tmp_path):
    named = TestClient(create_app(tmp_path / "a", TOKENS, start_worker=False, name="KI-Workstation"))
    assert named.get("/status").json()["name"] == "KI-Workstation"
    unnamed = TestClient(create_app(tmp_path / "b", TOKENS, start_worker=False))
    assert unnamed.get("/status").json()["name"]          # the computer's name


def test_the_app_shows_name_and_version_of_its_server(monkeypatch):
    asked = []

    class Client:
        url = "http://workstation.example:1234"

        def about(self):
            asked.append(1)
            return {"name": "KI-Workstation", "version": "52e1c4c", "busy": False}

    c = core.Core.__new__(core.Core)
    c.delivery = SimpleNamespace(client=Client(), reachable=True, wake=threading.Event())
    c.about, c._asking_about, c.session = {"url": "", "name": "", "version": "", "at": 0.0}, False, None
    assert c.server_about() == {"name": "", "version": ""}        # asked in the background
    for _ in range(100):
        if c.about["version"]:
            break
        time.sleep(0.01)
    assert c.server_about() == {"name": "KI-Workstation", "version": "52e1c4c"} and len(asked) == 1
    c.delivery.client = SimpleNamespace(url="http://127.0.0.1:8765")   # another server: nothing until known
    assert c.server_about() == {"name": "", "version": ""}
    c.delivery.client = None
    assert c.server_about() == {"name": "", "version": ""}


def test_the_window_in_front_asks_for_a_sync(tmp_path):
    c = core.Core.__new__(core.Core)
    c.profile = Profile(tmp_path / "profile.json")
    c.delivery = SimpleNamespace(client=object(), wake=threading.Event())
    c.session = None
    c.refresh_profile()
    assert c.profile.stale and c.delivery.wake.is_set()
    c.delivery.client, c.profile.stale = None, False
    c.refresh_profile()                                   # no server: nothing to fetch
    assert not c.profile.stale


def test_the_app_opens_the_modules_on_the_page(monkeypatch):
    opened = []
    monkeypatch.setattr(core.webbrowser, "open", opened.append)
    c = core.Core.__new__(core.Core)
    c.delivery = SimpleNamespace(client=SimpleNamespace(url="http://s", library_ticket=lambda: {"path": "/library/#ticket=t"}))
    assert c.open_on_server("modules") == "" and opened == ["http://s/library/#ticket=t&page=modules"]
