"""The page "Meine Aufnahmen" on the server: what a lecturer may do with their recordings.

Phase 4 of the separation of app and server (plan of 2026-11-06): the review, the approval of
the cut and the publishing run on the server, on the same page the app shows (ui.html at the
repository root, served by the server in its server mode). The actions are the app's
(recordings.py), here on the session folders of whoever asks, with the pipeline of the
server's worker and the settings of their profile. Publishing makes a zip file with the
Opencast names, which the lecturer downloads, until FLEX3 talks to Opencast itself.

The page signs in without a password: the app asks for a one-time ticket with its device
token and opens the page with it; the page trades the ticket for a session of a few hours
(Sessions). Videos are fetched by the <video> element, which sends no header, so their
address carries the session.
"""
from __future__ import annotations

import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Callable

from . import profiles
from .worker import Worker               # puts the repository root on sys.path

import library  # noqa: E402
from pipeline import cut_rules, publish, sounds  # noqa: E402
from recordings import Recordings  # noqa: E402

TICKET_SECONDS = 120
SESSION_SECONDS = 12 * 3600

# what the page may call, by the names ui.html uses with the app (api().<name>(...))
CALLS = ("data", "recordings", "recording", "start_transcription", "redo_transcription", "review_word",
         "redo_cut", "change_cut_item", "add_cut_item", "remove_cut_item", "cover_lock", "lift_lock", "set_program_rule",
         "set_cut_metadata",
         "set_cut_cards", "set_cut_settings", "approve_cut", "rerender", "make_summary", "publish_plan", "publish",
         "delete_recording")


class Sessions:
    """One-time tickets and the page sessions made from them, in memory: a restarted server only
    means the page asks the app for a new ticket."""

    def __init__(self):
        self.lock = threading.Lock()
        self.tickets: dict[str, tuple[dict, float]] = {}
        self.sessions: dict[str, tuple[dict, float]] = {}

    @staticmethod
    def _who(who: dict) -> dict:
        return {k: who.get(k, "") for k in ("user_id", "user_name", "device_id")}

    def ticket(self, who: dict) -> str:
        t = secrets.token_urlsafe(24)
        with self.lock:
            self._expire()
            self.tickets[t] = (self._who(who), time.time() + TICKET_SECONDS)
        return t

    def open(self, ticket: str) -> tuple[str, dict] | None:
        """A session for a valid ticket, which is used up; None otherwise."""
        with self.lock:
            self._expire()
            found = self.tickets.pop(ticket, None)
            if found is None:
                return None
            token = secrets.token_urlsafe(32)
            self.sessions[token] = (found[0], time.time() + SESSION_SECONDS)
            return token, found[0]

    def identity(self, token: str) -> dict | None:
        with self.lock:
            found = self.sessions.get(token)
            return found[0] if found and found[1] > time.time() else None

    def _expire(self) -> None:
        now = time.time()
        for store in (self.tickets, self.sessions):
            for k in [k for k, (_, until) in store.items() if until <= now]:
                del store[k]


class MyRecordings(Recordings):
    """The recordings of one lecturer on the server; made for each request."""

    def __init__(self, data_dir: Path, user_id: str, worker: Worker, store: profiles.Store,
                 lock: Callable[[str], threading.Lock]):
        self.data_dir, self.user_id = Path(data_dir), user_id
        self.worker, self.pipeline, self.store, self.lock = worker, worker.runner, store, lock
        self._library = self._scan()

    # ---------------------------------------------------------- what Recordings needs

    def _scan(self) -> dict[str, Path]:
        sessions = self.data_dir / "sessions"
        found = {}
        for f in sorted(sessions.iterdir()) if sessions.exists() else []:
            if f.is_dir() and not f.name.startswith(".") and (f / "session.json").exists() \
                    and Worker.owner(f) == self.user_id:
                found[f.name] = f
        return found

    def cut_settings(self) -> dict:
        return {**cut_rules.settings_from(self.store.values(self.user_id)),
                "sounds": sorted(sounds.available(self.data_dir / "sounds"))}

    def set_cut_settings(self, values: dict) -> None:
        with self.lock("profile:" + self.user_id):
            modules, prefs = self.store.load(self.user_id)
            current = {k[4:]: v["value"] for k, v in prefs.items() if k.startswith("cut.")}
            prefs, changed = profiles.stamp(prefs, cut_rules.cut_preferences(current, values or {}))
            if changed:
                self.store.save(self.user_id, modules, prefs)

    def _find_module(self, name: str) -> dict | None:
        return self.store.module(self.user_id, name)

    def _add_term(self, module: dict, term: str) -> None:
        with self.lock("profile:" + self.user_id):
            modules, prefs = self.store.load(self.user_id)
            m = modules.get(module["id"])
            if m is None or term in m.get("terms", []):
                return
            changed = {**m, "terms": (m.get("terms", []) + [term])[:profiles.MAX_TERMS],
                       "updated_at": max(time.time(), m["updated_at"] + 0.001)}
            try:
                modules[m["id"]] = profiles.clean(changed)
            except ValueError:
                return                               # e.g. a term longer than the profile allows
            self.store.save(self.user_id, modules, prefs)

    def _discard(self, folder: Path) -> None:
        """Into the server's trash, which an administrator empties; nothing is lost by a wrong click."""
        trash = self.data_dir / "trash"
        trash.mkdir(exist_ok=True)
        shutil.move(str(folder), str(trash / f"{folder.name}-{time.strftime('%Y%m%d-%H%M%S')}"))
        Path(self.package_file(folder.name)).unlink(missing_ok=True)

    # ---------------------------------------------------------- the page

    def data(self) -> dict:
        return {"cut": self.cut_settings(), "after_recording": cut_rules.after_recording(self.store.values(self.user_id))}

    # ---------------------------------------------------------- publishing: a package to download

    def package_file(self, sid: str) -> Path:
        return self.data_dir / "packages" / f"{sid}.zip"

    def package_name(self, sid: str) -> str:
        folder = self._library.get(sid)
        return f"{publish.stem(folder)}.zip" if folder else "recording.zip"

    def publish_plan(self, sid: str) -> dict:
        folder = self._library.get(sid)
        if folder is None:
            return {"folder": "", "files": [], "package": True}
        meta = library.summary(folder)
        return {"folder": "", "files": [packed for _, packed in publish.package_names(folder)], "package": True,
                "term": meta["term"], "module": publish.clean(meta["module"]), "name": self.package_name(sid)}

    def publish(self, sid: str) -> str:
        """Pack the finished files into the zip file the page then downloads; returns an error or ''."""
        folder = self._library.get(sid)
        if folder is None or not folder.exists():
            return "Die Aufnahme ist nicht mehr in der Ablage."
        if library.stations(folder)["cut"] != "done":
            return "Veröffentlichen geht erst, wenn das geschnittene Video fertig ist."
        with self.pipeline.lock:
            if self.pipeline.active == str(folder):
                return "An dieser Aufnahme wird gerade gearbeitet. Versuch es gleich noch einmal."
            try:
                publish.make_package(folder, self.package_file(sid))
            except (OSError, ValueError) as e:
                return str(e) if isinstance(e, (FileNotFoundError, ValueError)) else f"Packen fehlgeschlagen: {e}"
        return ""

    def package(self, sid: str) -> Path | None:
        """The zip file of a recording if it holds what is published now."""
        folder = self._library.get(sid)
        f = self.package_file(sid)
        return f if folder and f.exists() and publish.current(folder) else None
