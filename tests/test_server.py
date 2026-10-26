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


@pytest.fixture
def server(tmp_path):
    app = create_app(tmp_path, {TOKEN: "flex3-light-test", OTHER: "flex3-light-other"})
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
