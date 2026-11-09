"""The server's "Meine Aufnahmen" for one lecturer, without the server around it.

Since phase 5 (2026-11-09) the review, the cut and the deletion of a recording live on the
server only (my_recordings.MyRecordings on top of recordings.Recordings); tests of those
actions build one here on folders of their own instead of scanning a data folder.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT))
from flex3_light_server import profiles  # noqa: E402
from flex3_light_server.my_recordings import MyRecordings  # noqa: E402
import pipeline_runner  # noqa: E402

USER = "lecturer1"


def shelf(tmp_path: Path, library: dict[str, Path], pipeline=None, user: str = USER) -> MyRecordings:
    r = MyRecordings.__new__(MyRecordings)
    r.data_dir, r.user_id, r.worker = Path(tmp_path) / "server", user, None
    r.store = profiles.Store(r.data_dir / "profiles")
    locks: dict[str, threading.Lock] = {}
    r.lock = lambda name: locks.setdefault(name, threading.Lock())
    r.pipeline = pipeline or pipeline_runner.PipelineRunner(Path(tmp_path) / "q.json", lambda: False, lambda m: [])
    r._library = dict(library)
    return r


def add_module(r: MyRecordings, name: str, terms: list[str]) -> dict:
    """A module in the lecturer's profile on the server, as the app's sync leaves it."""
    modules, prefs = r.store.load(r.user_id)
    m = profiles.clean({"id": f"m{len(modules) + 1}", "updated_at": time.time(), "name": name, "terms": terms})
    modules[m["id"]] = m
    r.store.save(r.user_id, modules, prefs)
    return m
