"""Tests for the receiving server: parts in, session folder out."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from flex3_light_server.app import create_app  # noqa: E402
from flex3_light_server.assemble import duration  # noqa: E402

TOKEN, OTHER = "test-token", "other-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SID = "2026-10-26_test-module_1400"


def clip(path: Path, seconds: float, freq: int) -> Path:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=s=320x180:r=30:d={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000:duration={seconds}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                    str(path)], check=True)
    return path


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    d = tmp_path_factory.mktemp("clips")
    return [clip(d / f"{i}.mp4", 2, 440) for i in range(3)]


LAPTOP, OFFICE = "laptop-token", "office-token"


@pytest.fixture
def server(tmp_path):
    app = create_app(tmp_path, {TOKEN: "flex3-light-test", OTHER: "flex3-light-other",
                                LAPTOP: {"device_id": "flex3-light-laptop", "user_id": "lecturer1", "user_name": "Lecturer One"},
                                OFFICE: {"device_id": "flex3-light-office", "user_id": "lecturer1", "user_name": "Lecturer One"}})
    return TestClient(app), tmp_path


def put(client, track, index, path, sid=SID, headers=AUTH):
    data = path.read_bytes()
    return client.put(f"/sessions/{sid}/parts/{track}/{index}", content=data,
                      headers={**headers, "X-Content-SHA256": hashlib.sha256(data).hexdigest()})


def session_json(sid=SID):
    return {"session_id": sid, "status": "unassigned", "course_name": "Test", "recorded_at": "2026-10-26T14:00:00+01:00",
            "duration_sec": 6, "language": "de",
            "consent": {"recording": True, "publication": False, "voice_clone": False},
            "consent_source": "restricted_on_device", "device_id": "ignored", "software_version": "0.1.0"}


def wait(client, sid=SID, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        st = client.get(f"/sessions/{sid}", headers=AUTH).json()
        if st["state"] != "assembling":
            return st
        time.sleep(0.2)
    raise TimeoutError


def test_full_session(server, clips):
    client, data = server
    assert client.put(f"/sessions/{SID}", headers=AUTH).json() == {"state": "open"}
    for i, c in enumerate(clips):
        for track in ("mix", "screen", "camera"):
            assert put(client, track, i, c).status_code == 200
    assert put(client, "mix", 1, clips[1]).status_code == 200            # a repeated part does no harm
    assert client.get(f"/sessions/{SID}", headers=AUTH).json()["parts"]["screen"] == [0, 1, 2]
    r = client.post(f"/sessions/{SID}/complete", headers=AUTH,
                    json={"session": session_json(), "parts": {"mix": 3, "screen": 3, "camera": 3}})
    assert r.status_code == 202
    assert wait(client)["state"] == "complete"
    folder = data / "sessions" / SID
    assert sorted(p.name for p in folder.iterdir()) == ["camera.mp4", "recording.mp4", "screen.mp4", "session.json"]
    meta = json.loads((folder / "session.json").read_text(encoding="utf-8"))
    assert meta["device_id"] == "flex3-light-test"
    assert meta["tracks"] == [{"file": "screen.mp4", "kind": "screen"}, {"file": "camera.mp4", "kind": "camera"}]
    assert abs(duration(folder / "recording.mp4") - 6) < 0.5
    assert not (data / "incoming" / SID).exists()
    # completing again is harmless, deleting a complete session is refused
    assert client.post(f"/sessions/{SID}/complete", headers=AUTH, json={}).json() == {"state": "complete"}
    assert client.delete(f"/sessions/{SID}", headers=AUTH).status_code == 409


def test_mix_only_has_no_tracks_field(server, clips):
    client, data = server
    client.put(f"/sessions/{SID}", headers=AUTH)
    put(client, "mix", 0, clips[0])
    client.post(f"/sessions/{SID}/complete", headers=AUTH, json={"session": session_json(), "parts": {"mix": 1}})
    assert wait(client)["state"] == "complete"
    assert "tracks" not in json.loads((data / "sessions" / SID / "session.json").read_text(encoding="utf-8"))


def test_missing_part_is_refused(server, clips):
    client, _ = server
    client.put(f"/sessions/{SID}", headers=AUTH)
    put(client, "mix", 0, clips[0])
    r = client.post(f"/sessions/{SID}/complete", headers=AUTH, json={"session": session_json(), "parts": {"mix": 2}})
    assert r.status_code == 409
    assert r.json()["detail"] == {"missing": {"mix": [1]}}


def test_missing_field_is_refused(server, clips):
    client, _ = server
    client.put(f"/sessions/{SID}", headers=AUTH)
    put(client, "mix", 0, clips[0])
    s = session_json()
    del s["language"]
    r = client.post(f"/sessions/{SID}/complete", headers=AUTH, json={"session": s, "parts": {"mix": 1}})
    assert r.status_code == 422


def test_checksum_mismatch(server, clips):
    client, _ = server
    client.put(f"/sessions/{SID}", headers=AUTH)
    r = client.put(f"/sessions/{SID}/parts/mix/0", content=b"broken", headers={**AUTH, "X-Content-SHA256": "0" * 64})
    assert r.status_code == 400
    assert client.get(f"/sessions/{SID}", headers=AUTH).json()["parts"]["mix"] == []


def test_tokens_and_ownership(server, clips):
    client, _ = server
    assert client.put(f"/sessions/{SID}", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.put(f"/sessions/{SID}").status_code == 401
    client.put(f"/sessions/{SID}", headers=AUTH)
    other = {"Authorization": f"Bearer {OTHER}"}
    assert client.put(f"/sessions/{SID}", headers=other).status_code == 409
    assert put(client, "mix", 0, clips[0], headers=other).status_code == 409


def test_invalid_ids(server, clips):
    client, _ = server
    assert client.put("/sessions/..%2Fetc", headers=AUTH).status_code in (400, 404)
    assert client.put("/sessions/Not_Valid", headers=AUTH).status_code == 400
    client.put(f"/sessions/{SID}", headers=AUTH)
    assert put(client, "slides", 0, clips[0]).status_code == 400


def test_delete_open_session(server, clips):
    client, data = server
    client.put(f"/sessions/{SID}", headers=AUTH)
    put(client, "mix", 0, clips[0])
    assert client.delete(f"/sessions/{SID}", headers=AUTH).json() == {"state": "deleted"}
    assert not (data / "incoming" / SID).exists()
    assert client.delete(f"/sessions/{SID}", headers=AUTH).json() == {"state": "deleted"}


# ------------------------------------------------------------------ profiles

def module(mid, name, t, **extra):
    return {"id": mid, "name": name, "code": extra.pop("code", ""), "language": extra.pop("language", "de"),
            "updated_at": t, "deleted": extra.pop("deleted", False), **extra}


def sync(client, token, modules):
    r = client.post("/profile/sync", headers={"Authorization": f"Bearer {token}"}, json={"modules": modules})
    assert r.status_code == 200, r.text
    return {e["id"]: e for e in r.json()["modules"]}


def test_profile_merges_two_devices(server):
    client, _ = server
    sync(client, LAPTOP, [module("m1", "Digitalisierung", 1, code="dup")])
    # the office PC was offline and changed the same module later, and added one
    got = sync(client, OFFICE, [module("m1", "Digitalisierung und Programmierung", 2, code="dup"),
                                module("m2", "Applications of AI", 2, code="aai", language="en")])
    assert got["m1"]["name"] == "Digitalisierung und Programmierung" and got["m2"]["language"] == "en"
    # the laptop still sends its older state: the newer change stays
    got = sync(client, LAPTOP, [module("m1", "Digitalisierung", 1, code="dup")])
    assert got["m1"]["name"] == "Digitalisierung und Programmierung"
    # a deletion stays as a tombstone and is not brought back by an old copy
    sync(client, LAPTOP, [module("m2", "", 3, deleted=True)])
    got = sync(client, OFFICE, [module("m2", "Applications of AI", 2, code="aai")])
    assert got["m2"]["deleted"] is True
    r = client.get("/profile", headers={"Authorization": f"Bearer {OFFICE}"}).json()
    assert r["user_id"] == "lecturer1" and r["user_name"] == "Lecturer One" and len(r["modules"]) == 2


def test_profile_needs_a_lecturer_and_valid_entries(server):
    client, _ = server
    assert client.get("/profile", headers=AUTH).status_code == 403          # a device token without a person
    bad = client.post("/profile/sync", headers={"Authorization": f"Bearer {LAPTOP}"},
                      json={"modules": [module("m1", "X", 1, code="Not Valid")]})
    assert bad.status_code == 422


def test_complete_names_the_lecturer(server, clips):
    client, data = server
    auth = {"Authorization": f"Bearer {LAPTOP}"}
    client.put(f"/sessions/{SID}", headers=auth)
    put(client, "mix", 0, clips[0], headers=auth)
    s = {**session_json(), "instructor_id": "someone-else"}
    client.post(f"/sessions/{SID}/complete", headers=auth, json={"session": s, "parts": {"mix": 1}})
    end = time.time() + 60
    while not (data / "sessions" / SID / "session.json").exists() and time.time() < end:
        time.sleep(0.2)
    meta = json.loads((data / "sessions" / SID / "session.json").read_text(encoding="utf-8"))
    assert (meta["instructor_id"], meta["instructor_name"], meta["device_id"]) == \
        ("lecturer1", "Lecturer One", "flex3-light-laptop")
