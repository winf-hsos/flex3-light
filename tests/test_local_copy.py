"""Phase 5 (2026-11-09): the app only records. The local copy of a recording goes to the recycle
bin seven days after the server confirmed it, and a server at 127.0.0.1 is started by the app."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import delivery  # noqa: E402
import local_server  # noqa: E402


def filed(archive: Path, name: str) -> Path:
    folder = archive / "2026-27 - WS" / "Digitization and Programming" / name
    folder.mkdir(parents=True)
    (folder / "session.json").write_text(json.dumps({"session_id": name}), encoding="utf-8")
    return folder


def test_a_confirmed_copy_expires_after_seven_days_and_no_other(tmp_path):
    old, new, never = (filed(tmp_path, n) for n in ("2026-11-02 - Old", "2026-11-09 - New", "2026-11-09 - Never"))
    (tmp_path / "Veröffentlichen" / "2026-27 - WS").mkdir(parents=True)          # no session folder
    assert sorted(f.name for f in delivery.unconfirmed(tmp_path)) == sorted([old.name, new.name, never.name])
    now = time.time()
    delivery.mark_confirmed(old, "http://127.0.0.1:8765", at=now - 7 * 86400 - 60)
    delivery.mark_confirmed(new, "http://127.0.0.1:8765", at=now - 3600)
    delivery.mark_confirmed(old, "http://elsewhere", at=now)                      # the first confirmation counts
    assert json.loads((old / delivery.CONFIRMED).read_text(encoding="utf-8"))["server"] == "http://127.0.0.1:8765"
    assert [f.name for f in delivery.unconfirmed(tmp_path)] == [never.name]
    assert [f.name for f in delivery.expired(tmp_path, now=now)] == [old.name]
    assert sorted(f.name for f in delivery.expired(tmp_path, now=now + 7 * 86400)) == sorted([old.name, new.name])
    (new / delivery.CONFIRMED).write_text("kaputt", encoding="utf-8")            # unreadable: never deleted
    assert [f.name for f in delivery.expired(tmp_path, now=now + 30 * 86400)] == [old.name]


def test_a_folder_without_session_is_never_confirmed(tmp_path):
    folder = tmp_path / "a" / "b" / "c"
    folder.mkdir(parents=True)
    delivery.mark_confirmed(folder, "http://127.0.0.1:8765")
    assert not (folder / delivery.CONFIRMED).exists()


def test_the_app_starts_only_a_local_server_that_is_not_running(monkeypatch):
    started = []
    monkeypatch.setattr(local_server.subprocess, "Popen", lambda *a, **k: started.append(a[0]))
    monkeypatch.setattr(local_server, "running", lambda url, timeout=2: url.endswith(":8765"))
    assert local_server.is_local("http://127.0.0.1:8765") and local_server.is_local("http://localhost:9000/")
    assert not local_server.is_local("https://flex3.hs-osnabrueck.de") and not local_server.is_local("")
    assert not local_server.ensure("https://flex3.hs-osnabrueck.de")             # a server elsewhere is left alone
    assert not local_server.ensure("")                                           # not connected
    assert not local_server.ensure("http://127.0.0.1:8765")                      # already running
    assert started == []
    monkeypatch.setattr(local_server, "running", lambda url, timeout=2: False)
    monkeypatch.setattr(local_server.Path, "home", staticmethod(lambda: ROOT / ".pytest_cache" / "home"))
    assert local_server.ensure("http://127.0.0.1:8766")
    args = started[0]
    assert args[1].endswith("dev_server.py") and args[2:] == ["--port", "8766", "--idle-exit", "30"]
    assert Path(args[0]).name.lower() == "python.exe"                            # never pythonw: it logs
    monkeypatch.setattr(local_server.sys, "frozen", True, raising=False)
    assert not local_server.ensure("http://127.0.0.1:8767")                      # the exe carries no server
