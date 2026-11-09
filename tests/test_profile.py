"""The lecturer's profile: seeded locally, edited offline, merged with the server and other devices."""
from __future__ import annotations

import json
import random
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "server"))
import delivery  # noqa: E402
import lecturer_profile as lp  # noqa: E402
from flex3_light_server import profiles as server_profiles  # noqa: E402
from flex3_light_server.app import create_app  # noqa: E402
from server_client import ServerClient  # noqa: E402

LAPTOP, OFFICE = "laptop-token", "office-token"
MODULE_MD = """# Module

## Wie ein Modul beschrieben wird
kennung: so nicht

## Digitalisierung und Programmierung
kennung: dup
kuerzel: DuP
art: Bachelor · Wintersemester · Englisch

## Planung und Vorbereitung wissenschaftlicher Projekte
kuerzel: PVwP
art: Bachelor · Deutsch
"""


@pytest.fixture
def server(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    who = {"user_id": "lecturer1", "user_name": "Lecturer One"}
    app = create_app(tmp_path / "server", {LAPTOP: {"device_id": "laptop", **who},
                                           OFFICE: {"device_id": "office", **who}, "device-only": "kiosk"})
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True


def names(p: lp.Profile) -> list[str]:
    return [m["name"] for m in p.modules()]


def test_seed_from_markdown_and_archive(tmp_path):
    md = tmp_path / "module.md"
    md.write_text(MODULE_MD, encoding="utf-8")
    (tmp_path / "archive" / "2026-27 - WS" / "Applications of Artificial Intelligence").mkdir(parents=True)
    (tmp_path / "archive" / "2026-27 - WS" / "digitalisierung und programmierung").mkdir()   # same module
    seeds = {e["name"]: e for e in lp.seed_entries(tmp_path / "archive", "2026-27 - WS", str(md))}
    assert sorted(seeds) == ["Applications of Artificial Intelligence", "Digitalisierung und Programmierung",
                             "Planung und Vorbereitung wissenschaftlicher Projekte"]
    assert (seeds["Digitalisierung und Programmierung"]["code"], seeds["Digitalisierung und Programmierung"]["language"]) \
        == ("dup", "en")
    assert seeds["Planung und Vorbereitung wissenschaftlicher Projekte"]["code"] == "pvwp"


def test_local_changes_without_server(tmp_path):
    p = lp.Profile(tmp_path / "profile.json")
    assert p.is_new
    m = p.add("Big Data Analytics", language="en")
    assert m["code"] == "big-data-analytics" and p.dirty()
    p.update(m["id"], code="bda")
    p.delete(p.add("Wegwerf")["id"])
    again = lp.Profile(tmp_path / "profile.json")             # survives a restart
    assert names(again) == ["Big Data Analytics"] and again.find("big data  analytics")["code"] == "bda"


def test_first_sync_drops_duplicate_seeds_and_two_devices_converge(tmp_path, server):
    laptop = lp.Profile(tmp_path / "laptop.json")
    laptop.seed([{"name": "Digitalisierung und Programmierung", "code": "dup"}, {"name": "Empirisches Arbeiten"}])
    laptop.sync(ServerClient(server, LAPTOP))
    assert laptop.user() == {"user_id": "lecturer1", "user_name": "Lecturer One"} and not laptop.dirty()
    # the office PC seeds the same modules on its first start: no duplicates on the server
    office = lp.Profile(tmp_path / "office.json")
    office.seed([{"name": "Digitalisierung und Programmierung"}, {"name": "Applications of AI"}])
    office.sync(ServerClient(server, OFFICE))
    assert names(office) == ["Applications of AI", "Digitalisierung und Programmierung", "Empirisches Arbeiten"]
    # both edit offline, then sync: the newer change of each entry wins, deletions stay
    dup = office.find("Digitalisierung und Programmierung")
    laptop.sync(ServerClient(server, LAPTOP))
    laptop.update(laptop.find("Digitalisierung und Programmierung")["id"], language="en")
    time.sleep(0.01)
    office.update(dup["id"], code="dup2")
    office.delete(office.find("Empirisches Arbeiten")["id"])
    office.sync(ServerClient(server, OFFICE))
    laptop.sync(ServerClient(server, LAPTOP))
    office.sync(ServerClient(server, OFFICE))
    for p in (laptop, office):
        assert names(p) == ["Applications of AI", "Digitalisierung und Programmierung"]
        assert p.find("Digitalisierung und Programmierung")["code"] == "dup2"     # the later change wins
    assert laptop.modules() == office.modules()


def test_client_and_server_merge_agree():
    rnd = random.Random(7)
    for _ in range(200):
        a = {"id": "m", "name": rnd.choice("ab"), "code": rnd.choice(["x", "y"]), "language": "de",
             "ilias_course_id": "", "opencast_series_id": "", "updated_at": float(rnd.randint(0, 2)),
             "deleted": rnd.random() < 0.3}
        b = dict(a, name=rnd.choice("ab"), updated_at=float(rnd.randint(0, 2)), deleted=rnd.random() < 0.3)
        assert lp._newer(a, b) == server_profiles.newer(a, b) == lp._newer(b, a)


def test_delivery_syncs_the_profile_when_the_server_comes_back(tmp_path, server):
    p = lp.Profile(tmp_path / "profile.json")
    d = delivery.Delivery(tmp_path / "raw", ServerClient("http://127.0.0.1:9", LAPTOP, timeout=2), profile=p).start()
    p.add("Offline angelegt")
    time.sleep(3)
    assert d.connection() == "offline" and p.dirty()
    d.client = ServerClient(server, LAPTOP)        # the server is reachable again
    d.server_down_until = 0
    end = time.time() + 60
    while p.dirty() and time.time() < end:
        time.sleep(0.2)
    assert not p.dirty() and d.connection() == "online"
    office = lp.Profile(tmp_path / "office.json")
    office.sync(ServerClient(server, OFFICE))
    assert names(office) == ["Offline angelegt"]


def test_device_token_without_lecturer(tmp_path, server):
    p = lp.Profile(tmp_path / "profile.json")
    d = delivery.Delivery(tmp_path / "raw", ServerClient(server, "device-only"), profile=p).start()
    end = time.time() + 30
    while p.status != "no-profile" and time.time() < end:
        time.sleep(0.2)
    assert p.status == "no-profile"
    time.sleep(1)
    assert d.connection() == "online"         # the server answers, recordings can still be delivered


# ------------------------------------------------------------------ preferences (phase 2, 2026-11-09)

def test_preferences_reach_the_other_device_key_by_key(tmp_path, server):
    laptop, office = lp.Profile(tmp_path / "laptop.json"), lp.Profile(tmp_path / "office.json")
    laptop.set_preferences({"cut.pause_min": 30.0, "cut.pause_mode": "card", "after_recording": "nothing"})
    laptop.sync(ServerClient(server, LAPTOP))
    office.sync(ServerClient(server, OFFICE))
    assert office.preferences("cut.") == {"pause_min": 30.0, "pause_mode": "card"}
    # both change a different setting offline: neither change is lost
    laptop.set_preferences({"cut.pause_min": 60.0, "cut.pause_mode": "card"})     # the mode did not change
    time.sleep(0.01)
    office.set_preferences({"after_recording": "cut"})
    for p, token in ((office, OFFICE), (laptop, LAPTOP), (office, OFFICE)):
        p.sync(ServerClient(server, token))
    for p in (laptop, office):
        assert p.preference("cut.pause_min") == 60.0 and p.preference("after_recording") == "cut"
    # the same setting changed on both: the later change wins everywhere
    laptop.set_preferences({"cut.pause_mode": "cut"})
    time.sleep(0.01)
    office.set_preferences({"cut.pause_mode": "shorten"})
    for p, token in ((laptop, LAPTOP), (office, OFFICE), (laptop, LAPTOP)):
        p.sync(ServerClient(server, token))
    assert laptop.preference("cut.pause_mode") == office.preference("cut.pause_mode") == "shorten"


def test_an_unchanged_preference_is_not_a_change(tmp_path):
    p = lp.Profile(tmp_path / "profile.json")
    assert p.set_preferences({"after_recording": "cut"})
    revision, stamp = p.data["revision"], p.data["preferences"]["after_recording"]["updated_at"]
    assert not p.set_preferences({"after_recording": "cut"})
    assert p.data["revision"] == revision and p.data["preferences"]["after_recording"]["updated_at"] == stamp
    assert lp.Profile(tmp_path / "profile.json").preference("after_recording") == "cut"   # saved at once


def test_old_settings_move_into_the_profile_and_the_server_wins(tmp_path, server, monkeypatch):
    import core
    saved = []
    monkeypatch.setattr(core, "save_settings", lambda s: saved.append(dict(s)))
    office = lp.Profile(tmp_path / "office.json")
    office.set_preferences({"cut.pause_min": 90.0})
    office.sync(ServerClient(server, OFFICE))
    settings = {"raw_dir": "R", "after_recording": "transcribe", "language": "en",
                "cut": {"pause_min": 45.0, "cut_titles": ["inbox"], "auto": True}}
    laptop = lp.Profile(tmp_path / "laptop.json")
    core._move_preferences_to_profile(settings, laptop)
    assert settings == {"raw_dir": "R"} and saved == [{"raw_dir": "R"}]       # only the device's own settings stay
    assert laptop.preference("after_recording") == "transcribe" and laptop.preference("language") == "en"
    laptop.sync(ServerClient(server, LAPTOP))
    assert laptop.preference("cut.pause_min") == 90.0                          # set on the server before: it wins
    assert laptop.preference("cut.cut_titles") == ["inbox"]                    # unknown there: taken over
    assert laptop.preference("cut.auto") is None                               # the old switch stays behind
    core._move_preferences_to_profile(settings, laptop)
    assert len(saved) == 1                                                     # only once


def test_server_checks_preferences_and_keeps_them(tmp_path):
    from fastapi.testclient import TestClient
    client = TestClient(create_app(tmp_path, {LAPTOP: {"device_id": "laptop", "user_id": "lecturer1"}}))
    auth = {"Authorization": f"Bearer {LAPTOP}"}
    for bad in ({"Cut.Pause": {"value": 1, "updated_at": 1}}, {"cut.pause_min": {"value": 1}},
                {"cut.notes": {"value": "x" * 9000, "updated_at": 1}}, ["cut.pause_min"]):
        assert client.post("/profile/sync", headers=auth, json={"modules": [], "preferences": bad}).status_code == 422
    good = {"cut.cut_programs": {"value": ["outlook.exe"], "updated_at": 5}}
    assert client.post("/profile/sync", headers=auth, json={"modules": [], "preferences": good}).status_code == 200
    assert client.post("/profile/sync", headers=auth, json={"modules": []}).json()["preferences"] == {
        "cut.cut_programs": {"value": ["outlook.exe"], "updated_at": 5.0}}             # an older app sends none
    assert client.get("/profile", headers=auth).json()["preferences"]["cut.cut_programs"]["value"] == ["outlook.exe"]
