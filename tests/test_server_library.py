"""The page "Meine Aufnahmen" on the server (phase 4, 2026-11-09): sign-in with a ticket, the
lecturer's own recordings only, videos with range requests, approval, publishing as a package."""
from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from flex3_light_server import profiles  # noqa: E402
from flex3_light_server.app import create_app  # noqa: E402
from flex3_light_server.my_recordings import MyRecordings  # noqa: E402
from pipeline import edit, files, render  # noqa: E402
from test_server_pipeline import AUTH, AUTH2, TOKENS  # noqa: E402

MINE, THEIRS = "2026-11-09_digitization-and-programming_1504", "2026-11-09_statistics_0900"


def recording(data: Path, sid: str, user: str, topic: str = "Symbols and Information") -> Path:
    folder = data / "sessions" / sid
    folder.mkdir(parents=True)
    files.write_json(folder / "session.json", {
        "session_id": sid, "course_name": "Digitization and Programming", "topic": topic, "instructor_id": user,
        "recorded_at": "2026-11-09T15:04:03+01:00", "duration_sec": 60, "language": "en", "tracks": []})
    (folder / "screen.mp4").write_bytes(b"0123456789")
    return folder


def proposal(folder: Path) -> None:
    e = edit.new(60.0)
    e["title"] = "Symbols and Information"
    edit.add(e, edit.item("cut", 0.0, 4.0, "Mikrofontest", "agent", 0.9, "proposed"))
    edit.save(folder, e)


def rendered(folder: Path) -> None:
    """What the render step leaves behind for the approved list, without running ffmpeg."""
    e = edit.load(folder)
    for name in ("edited.mp4", "edited.en.vtt", "clip-01.mp4", "clip-01.en.vtt"):
        (folder / name).write_text(name, encoding="utf-8")
    files.write_json(folder / "clips.json", {"clips": [{"file": "clip-01.mp4", "title": "Bits"}]})
    files.write_json(folder / "render.json", {"signature": render.signature(e),
                                              "files": ["edited.mp4", "edited.en.vtt", "clip-01.mp4"]})


def server(tmp_path):
    data = tmp_path / "server"
    client = TestClient(create_app(data, TOKENS, start_worker=False))
    return data, client


def signed_in(client) -> dict:
    ticket = client.post("/library/ticket", headers=AUTH).json()["ticket"]
    session = client.post("/library/session", json={"ticket": ticket}).json()["session"]
    return {"Authorization": f"Bearer {session}"}, session


def test_the_page_signs_in_with_a_ticket_and_shows_only_ones_own_recordings(tmp_path):
    data, client = server(tmp_path)
    recording(data, MINE, "lecturer1")
    recording(data, THEIRS, "lecturer2", "Regression")
    page = client.get("/library/")
    assert page.status_code == 200 and "window.FLEX3_SERVER" in page.text and "<title>FLEX3 Light</title>" in page.text
    assert client.post("/library/api/recordings", json=[]).status_code == 401
    ticket = client.post("/library/ticket", headers=AUTH).json()
    assert ticket["path"] == f"/library/#ticket={ticket['ticket']}"
    assert client.post("/library/session", json={"ticket": "made-up"}).status_code == 401
    session = client.post("/library/session", json={"ticket": ticket["ticket"]}).json()
    assert session["user_name"] == "Lecturer One"
    assert client.post("/library/session", json={"ticket": ticket["ticket"]}).status_code == 401   # used up
    page_auth = {"Authorization": f"Bearer {session['session']}"}
    listed = client.post("/library/api/recordings", json=[], headers=page_auth).json()
    assert [(r["id"], r["title"], r["term"]) for r in listed] == [(MINE, "Symbols and Information", "2026-27 - WS")]
    assert client.post("/library/api/recording", json=[THEIRS], headers=page_auth).json() is None
    assert client.post("/library/api/recordings", json=[], headers=AUTH2).json()[0]["id"] == THEIRS   # a device token
    assert client.post("/library/api/open_recording", json=[MINE], headers=page_auth).status_code == 404
    assert client.post("/library/api/recording", json=[MINE, 1, 2, 3], headers=page_auth).status_code == 422


def test_videos_stream_with_ranges_and_only_to_their_lecturer(tmp_path):
    data, client = server(tmp_path)
    recording(data, MINE, "lecturer1")
    recording(data, THEIRS, "lecturer2")
    _, session = signed_in(client)
    part = client.get(f"/library/media/{MINE}/screen.mp4?s={session}", headers={"Range": "bytes=2-5"})
    assert part.status_code == 206 and part.content == b"2345"
    assert part.headers["content-range"] == "bytes 2-5/10"
    assert client.get(f"/library/media/{MINE}/screen.mp4").status_code == 401
    assert client.get(f"/library/media/{THEIRS}/screen.mp4?s={session}").status_code == 404
    assert client.get(f"/library/media/{MINE}/session.json?s={session}").status_code == 404
    assert client.get(f"/library/media/{MINE}/screen.mp4?s={AUTH['Authorization'][7:]}").status_code == 401  # no tokens in addresses


def test_the_cut_is_approved_and_published_as_a_package(tmp_path):
    data, client = server(tmp_path)
    folder = recording(data, MINE, "lecturer1")
    proposal(folder)
    auth, session = signed_in(client)
    call = lambda name, *args: client.post(f"/library/api/{name}", json=list(args), headers=auth).json()  # noqa: E731
    d = call("recording", MINE)
    assert d["stations"]["cut"] == "review"
    assert "offen" in call("approve_cut", MINE, True)                     # the proposal is still open
    item = d["cut"]["edit"]["items"][0]["id"]
    assert call("change_cut_item", MINE, item, {"state": "accepted"}) == ""
    assert call("set_cut_metadata", MINE, " Bits and Bytes ", "") == ""
    assert call("approve_cut", MINE, True) == ""
    worker = client.app.state.worker
    assert worker.runner.queued(folder)                                    # the server renders it
    assert call("recording", MINE)["stations"]["cut"] == "queued"
    assert client.get(f"/library/package/{MINE}?s={session}").status_code == 404
    rendered(folder)
    assert call("recording", MINE)["stations"]["publish"] == "open"
    plan = call("publish_plan", MINE)
    assert plan["term"] == "2026-27 - WS" and plan["module"] == "Digitization and Programming"
    assert plan["files"][0] == "2026-11-09 - Digitization and Programming - Bits and Bytes.mp4"   # the title card
    assert files.read_json(folder / "session.json").get("consent") is None
    assert call("publish", MINE) == ""
    meta = files.read_json(folder / "session.json")                       # publishing is the release
    assert meta["consent"] == {"recording": True, "publication": True, "voice_clone": False}
    assert meta["consent_source"] == "register" and meta["topic"] == "Symbols and Information"
    package = client.get(f"/library/package/{MINE}?s={session}")
    assert package.status_code == 200 and "attachment" in package.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(package.content)) as z:
        assert sorted(z.namelist()) == sorted(plan["files"])
        for name in plan["files"]:                                        # one by one, for a browser that unpacks
            one = client.get(f"/library/package/{MINE}/{name}?s={session}")
            assert one.status_code == 200 and one.content == z.read(name)
    assert client.get(f"/library/package/{MINE}/..%2Fsession.json?s={session}").status_code == 404
    assert client.get(f"/library/package/{MINE}/{plan['files'][0]}", headers=AUTH2).status_code == 404
    d = call("recording", MINE)
    assert d["stations"]["publish"] == "done" and d["publish"]["package"]
    assert (folder / "edited.mp4").exists()                               # nothing moved on the server
    assert client.get(f"/library/package/{MINE}", headers=AUTH2).status_code == 404


def test_settings_and_terms_changed_on_the_page_reach_the_profile(tmp_path):
    data, client = server(tmp_path)
    recording(data, MINE, "lecturer1")
    store = profiles.Store(data / "profiles")
    store.save("lecturer1", {"m1": {"id": "m1", "updated_at": 1.0, "deleted": False, "name": "Digitization and Programming",
                                    "code": "", "language": "en", "ilias_course_id": "", "opencast_series_id": "",
                                    "terms": ["ASCII"]}}, {})
    auth, _ = signed_in(client)
    assert client.post("/library/api/set_program_rule", json=[MINE, "Code.exe", "cut"], headers=auth).json() == ""
    prefs = store.load("lecturer1")[1]
    assert prefs["cut.cut_programs"]["value"] == ["code.exe"]
    assert prefs["cut.cut_titles"]["value"] == sorted(["posteingang", "inbox", "gmail", "whatsapp web", "outlook"])
    stamp = prefs["cut.cut_programs"]["updated_at"]
    assert client.post("/library/api/set_cut_settings", json=[{"cut_programs": ["code.exe"]}], headers=auth).status_code == 200
    assert store.load("lecturer1")[1]["cut.cut_programs"]["updated_at"] == stamp     # unchanged keeps its time
    mine = MyRecordings(data, "lecturer1", client.app.state.worker, store, lambda key: __import__("threading").Lock())
    mine._add_term(mine._find_module("Digitization and Programming"), "Unicode")
    module = store.load("lecturer1")[0]["m1"]
    assert module["terms"] == ["ASCII", "Unicode"] and module["updated_at"] > 1.0     # the app's sync takes it


def test_a_deleted_recording_goes_to_the_servers_trash(tmp_path):
    data, client = server(tmp_path)
    recording(data, MINE, "lecturer1")
    auth, _ = signed_in(client)
    assert client.post("/library/api/delete_recording", json=[MINE], headers=auth).json() == ""
    assert not (data / "sessions" / MINE).exists()
    assert [p.name.startswith(MINE) for p in (data / "trash").iterdir()] == [True]
    assert client.post("/library/api/recordings", json=[], headers=auth).json() == []
    assert json.loads(next((data / "trash").iterdir()).joinpath("session.json").read_text(encoding="utf-8"))["topic"]


def test_the_app_opens_the_page_in_the_browser_signed_in(monkeypatch):
    import core
    from types import SimpleNamespace

    class Client:
        url = "http://127.0.0.1:8765"

        def library_ticket(self):
            return {"ticket": "t1", "path": "/library/#ticket=t1"}

    opened = []
    monkeypatch.setattr(core.webbrowser, "open", opened.append)
    c = core.Core.__new__(core.Core)
    c.delivery = SimpleNamespace(client=Client())
    assert c.open_on_server() == "" and c.open_on_server("settings") == ""
    assert opened == ["http://127.0.0.1:8765/library/#ticket=t1", "http://127.0.0.1:8765/library/#ticket=t1&page=settings"]
    c.delivery.client = None
    assert c.open_on_server() != "" and len(opened) == 2


def test_what_follows_a_recording_is_set_on_the_page(tmp_path):
    data, client = server(tmp_path)
    auth, _ = signed_in(client)
    assert client.post("/library/api/data", json=[], headers=auth).json()["after_recording"] == "nothing"
    client.post("/library/api/set_after_recording", json=["cut"], headers=auth)
    d = client.post("/library/api/data", json=[], headers=auth).json()
    assert d["after_recording"] == "cut" and d["cut"]["auto"] is True
    assert client.post("/library/api/set_after_recording", json=["everything"], headers=auth).status_code == 422
