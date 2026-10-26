"""End-to-end test of the delivery without OBS: synthetic segments in, archive and server session folder out."""
from __future__ import annotations

import json
import os
import socket
import subprocess
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
import split  # noqa: E402
from flex3_light_server.app import create_app  # noqa: E402
from server_client import ServerClient  # noqa: E402

TOKEN = "test-token"


def wide_segment(path: Path, seconds: float = 3) -> Path:
    """A raw OBS-like segment: 3840x1200 canvas, 30 fps, AAC 48 kHz."""
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=s=3840x1200:r=30:d={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                    str(path)], check=True)
    old = time.time() - 10                     # finished long enough ago
    os.utime(path, (old, old))
    return path


@pytest.fixture(scope="module")
def segments(tmp_path_factory):
    d = tmp_path_factory.mktemp("segments")
    return [wide_segment(d / f"2026-10-26 14-0{i}-00.mkv") for i in range(3)]


@pytest.fixture
def server(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    data = tmp_path / "server"
    config = uvicorn.Config(create_app(data, {TOKEN: "flex3-light-test"}), host="127.0.0.1", port=port,
                            log_level="warning")
    srv = uvicorn.Server(config)
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", data
    srv.should_exit = True


@pytest.fixture(autouse=True)
def no_recycle_bin(monkeypatch):
    monkeypatch.setattr(delivery, "recycle", lambda p: p.unlink())


def wait_for(cond, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.2)
    raise TimeoutError


def begin(d: delivery.Delivery, archive: Path, camera=True):
    return d.begin("Digitalisierung und Programmierung", "Schleifen", camera, (1920, 1152), True, archive,
                   {"language": "de", "device_id": "flex3-light-test"})


def test_session_is_archived_and_delivered(tmp_path, server, segments):
    url, data = server
    d = delivery.Delivery(tmp_path / "raw", ServerClient(url, TOKEN)).start()
    s = begin(d, tmp_path / "archive")
    assert s.id.startswith("20") and "_digitalisierung-und-programmierung_" in s.id
    # OBS writes the first two segments; the second is still open, so only the first is processed
    for f in segments[:2]:
        (s.raw_dir / f.name).write_bytes(f.read_bytes())
        os.utime(s.raw_dir / f.name, (time.time() - 10,) * 2)
    s.add_span(1.0, 1.5)
    wait_for(lambda: s.data["segments"] and s.data["segments"][0]["processed"])
    time.sleep(1)
    assert len(s.data["segments"]) == 1
    wait_for(lambda: len(s.data["segments"][0]["uploaded"]) == 3)       # uploaded while still recording
    (s.raw_dir / segments[2].name).write_bytes(segments[2].read_bytes())
    os.utime(s.raw_dir / segments[2].name, (time.time() - 10,) * 2)
    d.stop(s, save=True)
    wait_for(lambda: s.data["state"] == "done")
    a = s.data["archive"]
    assert Path(a["screen"]).name == f"{s.id[:10]} - Schleifen - Bildschirm.mp4"
    assert abs(split.duration(Path(a["screen"])) - 9) < 0.5
    assert abs(split.duration(Path(a["camera"])) - 9) < 0.5
    folder = data / "sessions" / s.id
    assert sorted(p.name for p in folder.iterdir()) == ["camera.mp4", "recording.mp4", "screen.mp4", "session.json"]
    meta = json.loads((folder / "session.json").read_text(encoding="utf-8"))
    assert meta["status"] == "unassigned" and meta["duration_sec"] == 9 and meta["course_name"].startswith("Digital")
    assert abs(split.duration(folder / "recording.mp4") - 9) < 0.5
    assert not s.folder.exists()                    # nothing left locally but the archive


def test_discard_deletes_on_the_server(tmp_path, server, segments):
    url, data = server
    d = delivery.Delivery(tmp_path / "raw", ServerClient(url, TOKEN)).start()
    s = begin(d, tmp_path / "archive", camera=False)
    for f in segments[:2]:
        (s.raw_dir / f.name).write_bytes(f.read_bytes())
        os.utime(s.raw_dir / f.name, (time.time() - 10,) * 2)
    wait_for(lambda: s.data["segments"] and len(s.data["segments"][0]["uploaded"]) == 2)
    assert (data / "incoming" / s.id).exists()
    d.stop(s, save=False)
    wait_for(lambda: s.data["state"] == "done")
    assert not (data / "incoming" / s.id).exists()
    assert not s.folder.exists()
    assert not (tmp_path / "archive").exists()


def test_without_server_only_archives(tmp_path, segments):
    d = delivery.Delivery(tmp_path / "raw").start()
    s = begin(d, tmp_path / "archive", camera=False)
    (s.raw_dir / segments[0].name).write_bytes(segments[0].read_bytes())
    os.utime(s.raw_dir / segments[0].name, (time.time() - 10,) * 2)
    d.stop(s, save=True)
    wait_for(lambda: s.data["state"] == "done")
    assert [p.name for p in (tmp_path / "archive").iterdir()] == [f"{s.id[:10]} - Schleifen - Bildschirm.mp4"]


def test_stop_right_after_the_last_segment_was_written(tmp_path, segments):
    """The last segment is fresh when the user stops; it must not be left out of the archive."""
    d = delivery.Delivery(tmp_path / "raw").start()
    s = begin(d, tmp_path / "archive", camera=False)
    for f in segments[:2]:
        (s.raw_dir / f.name).write_bytes(f.read_bytes())
        os.utime(s.raw_dir / f.name, (time.time() - 10,) * 2)
    wait_for(lambda: s.data["segments"] and s.data["segments"][0]["processed"])
    last = s.raw_dir / segments[2].name
    last.write_bytes(segments[2].read_bytes())
    wait_for(lambda: len(s.data["segments"]) == 2 and s.data["segments"][1]["processed"])
    os.utime(last)                     # OBS has just closed the last segment when the user stops
    d.stop(s, save=True)
    wait_for(lambda: s.data["state"] == "done")
    assert len(s.data["segments"]) == 3
    assert abs(split.duration(Path(s.data["archive"]["screen"])) - 9) < 0.5


def test_unreachable_server_keeps_everything_and_resumes(tmp_path, server, segments):
    url, data = server
    d = delivery.Delivery(tmp_path / "raw", ServerClient("http://127.0.0.1:9", TOKEN, timeout=2)).start()
    s = begin(d, tmp_path / "archive", camera=False)
    (s.raw_dir / segments[0].name).write_bytes(segments[0].read_bytes())
    os.utime(s.raw_dir / segments[0].name, (time.time() - 10,) * 2)
    d.stop(s, save=True)
    wait_for(lambda: s.data["state"] == "archived")
    time.sleep(3)
    assert d.server_error and s.folder.exists()     # archive written, parts kept for later
    # next start of the app, now with the server reachable
    d2 = delivery.Delivery(tmp_path / "raw", ServerClient(url, TOKEN)).start()
    wait_for(lambda: (data / "sessions" / s.id / "session.json").exists())
    wait_for(lambda: not s.folder.exists())
