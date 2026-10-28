"""Recording sessions that are processed and delivered piece by piece while the lecture runs.

OBS writes a new file every minute into the session's own raw folder. Two background
threads take it from there:

    processor   splits every finished segment into screen, camera and program mix
                (split.py, at low priority while recording), and after the stop joins
                the parts into the local archive
    uploader    sends every processed part to the FLEX3 Light server (server/), and
                after the archive is written completes the session there

Each session lives in a folder below the raw folder with a manifest.json, so work
that a closed window or a crash interrupted continues at the next start. Nothing is
deleted before it is safe: raw segments only after the archive is written and
checked, parts only after the server has confirmed the complete session.
"""
from __future__ import annotations

import ctypes
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import socket
import threading
import time
from pathlib import Path
from typing import Callable

import split
from server_client import ServerClient, ServerError

VERSION = "0.1.0"
SEGMENT_MINUTES = 1         # OBS splits the recording this often
SETTLE = 3.0                # a finished segment must be untouched this long before it is read
RETRY = (5, 15, 30, 60)     # seconds between attempts while the server is unreachable
# File names in a session folder, as the FLEX3 contract has them.
TRACK_FILES = {"mix": "recording.mp4", "screen": "screen.mp4", "camera": "camera.mp4"}


# ------------------------------------------------------------------ helpers

def free_dir(path: Path) -> Path:
    """path, or "path (2)" and so on if it exists; created empty. A name like "Kapitel 2.4"
    keeps its dot."""
    n, p = 2, path
    while p.exists():
        p = path.with_name(f"{path.name} ({n})")
        n += 1
    p.mkdir(parents=True)
    return p


def recycle(path: Path) -> None:
    """Move a file or folder to the recycle bin; a file is deleted if Windows refuses, a folder never."""
    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", ctypes.c_void_p), ("wFunc", ctypes.c_uint), ("pFrom", ctypes.c_wchar_p),
                    ("pTo", ctypes.c_wchar_p), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", ctypes.c_int),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_wchar_p)]
    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
    op = SHFILEOPSTRUCTW(wFunc=FO_DELETE, pFrom=str(path) + "\0",
                         fFlags=FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI)
    if ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) != 0 and path.exists():
        if path.is_dir():
            raise OSError(f"Windows hat {path.name} nicht in den Papierkorb gelegt.")
        path.unlink()


def slug(text: str, limit: int = 40) -> str:
    t = text.lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        t = t.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:limit].strip("-") or "session"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def default_device_id() -> str:
    return "flex3-light-" + slug(os.environ.get("COMPUTERNAME") or socket.gethostname(), 30)


def session_metadata(session_id: str, module: dict, topic: str, started: dt.datetime, settings: dict,
                     user: dict) -> dict:
    """session.json after the FLEX3 contract, without the duration (added at the end).

    The module from the lecturer's profile gives course name and language, and, once
    they are entered there, ILIAS course and Opencast series: then the session arrives
    assigned, otherwise unassigned for the server's queue. Consent is set conservatively
    (recorded, nothing published) and marked as restricted on the device, until Nicolas
    decides how consent without a register is handled.
    """
    ilias, series = module.get("ilias_course_id", ""), module.get("opencast_series_id", "")
    assigned = bool(ilias and series and user.get("user_id"))
    meta = {"session_id": session_id, "status": "assigned" if assigned else "unassigned",
            "course_name": module["name"], "instructor_id": user.get("user_id", ""),
            "instructor_name": user.get("user_name", ""), "recorded_at": started.isoformat(timespec="seconds"),
            "room": "", "language": module.get("language") or settings.get("language", "de"),
            "ilias_course_id": ilias, "opencast_series_id": series,
            "consent": {"recording": True, "publication": False, "voice_clone": False},
            "consent_source": "restricted_on_device",
            "device_id": settings.get("device_id") or default_device_id(), "software_version": VERSION}
    if topic:
        meta["topic"] = topic             # not in the contract; unknown fields are ignored
    return meta


def session_document(s: "Session") -> dict:
    """The complete session.json of a recording: metadata, net duration and the extra tracks."""
    tracks = [{"file": TRACK_FILES["screen"], "kind": "screen"}]
    if s.data["camera"]:
        tracks.append({"file": TRACK_FILES["camera"], "kind": "camera"})
    return {**s.data["meta"], "duration_sec": round(sum(x["duration"] for x in s.data["segments"])),
            "tracks": tracks}


# ------------------------------------------------------------------ session

class Session:
    """One recording: a folder with raw/ (written by OBS), parts/ and manifest.json."""

    def __init__(self, folder: Path, data: dict):
        self.folder, self.data = folder, data
        self.lock = threading.RLock()
        self.alttab_open = False          # Alt+Tab held right now: wait before cutting a segment
        self.private_open = False         # privacy key pressed and not released: wait as well

    @property
    def id(self) -> str:
        return self.data["session_id"]

    @property
    def raw_dir(self) -> Path:
        return self.folder / "raw"

    @property
    def parts_dir(self) -> Path:
        return self.folder / "parts"

    def part(self, index: int, track: str) -> Path:
        return self.parts_dir / f"{index:04d}-{track}.mp4"

    def tracks(self) -> list[str]:
        """Screen, camera if there is one, and the program mix that the session folder always holds."""
        return ["screen"] + (["camera"] if self.data["camera"] else []) + ["mix"]

    def freeze_spans(self) -> list[list[float]]:
        """Spans where the screen track stands still: Alt+Tab (if hidden) and the privacy key."""
        return (self.data["alttab"] if self.data["hide_alttab"] else []) + self.data.get("private", [])

    def save(self) -> None:
        with self.lock:
            tmp = self.folder / "manifest.json.tmp"
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.folder / "manifest.json")

    def add_span(self, a: float, b: float) -> None:
        with self.lock:
            self.data["alttab"].append([a, b])
            self.save()

    def add_private(self, a: float, b: float) -> None:
        """A span the lecturer froze with the privacy key."""
        with self.lock:
            self.data.setdefault("private", []).append([a, b])
            self.save()

    def add_activity(self, start: float, end: float, process: str, title: str) -> None:
        """Which program was in front from start to end (recording time); for the cut agent."""
        with self.lock:
            self.data.setdefault("activity", []).append([round(start, 2), round(end, 2), process, title])
            self.save()


# ------------------------------------------------------------------ delivery

class Delivery:
    def __init__(self, root: Path, client: ServerClient | None = None, profile=None):
        self.root = Path(root)
        self.client = client
        self.profile = profile          # lecturer_profile.Profile, synced by the uploader thread
        self.reachable: bool | None = None
        self.last_contact = 0.0
        self.sessions: list[Session] = []
        self.guard = threading.RLock()
        self.wake = threading.Event()
        self.server_down_until = 0.0
        self.server_error = ""
        self._retry = 0
        self.last_exception = ""
        self.on_archived: Callable[[Path], None] | None = None   # the pipeline picks it up

    def start(self, still_recording: Path | None = None) -> "Delivery":
        """Pick up unfinished sessions from earlier runs and start both threads.

        still_recording: the raw folder OBS is still recording into, if the app ended
        without stopping it; that session stays a running recording for the core to take over.
        """
        for manifest in sorted(self.root.glob("*/manifest.json")):
            s = Session(manifest.parent, json.loads(manifest.read_text(encoding="utf-8")))
            running = still_recording is not None and s.raw_dir.resolve() == Path(still_recording).resolve()
            if running and s.data["state"] in ("recording", "stopped"):
                s.data["state"] = "recording"          # OBS still records it: taken over, not stopped
            elif s.data["state"] == "recording":       # the app ended during a recording: keep it
                s.data["state"] = "stopped"
            s.data["error"] = None                   # try again what failed last time
            s.save()
            self.sessions.append(s)
        threading.Thread(target=self._loop, args=(self._process,), daemon=True).start()
        threading.Thread(target=self._loop, args=(self._upload,), daemon=True).start()
        return self

    # ---------------------------------------------------------- commands from the core

    def begin(self, module: dict, topic: str, camera: bool, screen_size, hide_alttab: bool,
              archive_folder: Path, settings: dict, user: dict | None = None) -> Session:
        """Open a session for a module from the profile (at least {"name": ...})."""
        started = dt.datetime.now().astimezone()
        base = f"{started:%Y-%m-%d}_{slug(module.get('code') or module['name'])}_{started:%H%M}"
        sid, n = base, 2
        while (self.root / sid).exists():
            sid, n = f"{base}-{n}", n + 1
        folder = self.root / sid
        (folder / "raw").mkdir(parents=True)
        stem = f"{started:%Y-%m-%d} - {topic or module['name']}"
        data = {"session_id": sid, "state": "recording", "module": module["name"], "topic": topic,
                "camera": camera, "screen_size": list(screen_size), "hide_alttab": hide_alttab,
                "deliver": self.client is not None, "alttab": [], "segments": [],
                "archive": {"folder": str(archive_folder), "stem": stem, "screen": None, "camera": None},
                "server": {"opened": False, "completed": False}, "error": None,
                "meta": session_metadata(sid, module, topic, started, settings, user or {})}
        s = Session(folder, data)
        s.save()
        with self.guard:
            self.sessions.append(s)
        return s

    def running(self) -> Session | None:
        """The session that is still recording, after start(still_recording=...)."""
        with self.guard:
            return next((s for s in self.sessions if s.data["state"] == "recording"), None)

    def stop(self, s: Session, save: bool) -> None:
        with s.lock:
            s.data["state"] = "stopped" if save else "discarded"
            s.alttab_open = s.private_open = False
            s.save()
        self.wake.set()

    def forget(self, s: Session) -> None:
        """Remove a session that never started recording."""
        with self.guard:
            self.sessions.remove(s)
        shutil.rmtree(s.folder, ignore_errors=True)

    def latest(self) -> Session | None:
        with self.guard:
            return self.sessions[-1] if self.sessions else None

    def busy(self) -> bool:
        """True while a stopped session is still being processed into the archive."""
        with self.guard:
            return any(s.data["state"] == "stopped" and not s.data["error"] for s in self.sessions)

    def summary(self, s: Session) -> dict:
        with s.lock:
            segs = s.data["segments"]
            tracks = s.tracks()
            total = len(segs) * len(tracks)
            uploaded = sum(len(x.get("uploaded", [])) for x in segs)
            return {"state": s.data["state"], "segments": len(segs), "processed": sum(x["processed"] for x in segs),
                    "deliver": s.data["deliver"], "uploaded": uploaded, "parts": total,
                    "completed": s.data["server"]["completed"], "error": s.data["error"],
                    "server_error": self.server_error if s.data["deliver"] else "",
                    "screen": s.data["archive"]["screen"], "module": s.data["module"],
                    "session_folder": s.data["archive"].get("session_folder"),
                    "alttab": len(s.data["alttab"]) if s.data["hide_alttab"] else 0}

    # ---------------------------------------------------------- threads

    def _loop(self, step) -> None:
        while True:
            try:
                worked = step()
            except Exception as e:            # keep the thread alive; kept for debugging
                self.last_exception = repr(e)
                worked = False
            if not worked:
                self.wake.wait(2)
                self.wake.clear()

    def _active(self) -> list[Session]:
        with self.guard:
            return list(self.sessions)

    def _finish(self, s: Session) -> None:
        with self.guard:
            if s in self.sessions and s is not self.sessions[-1]:
                self.sessions.remove(s)
        with s.lock:
            s.data["state"] = "done"
            s.save()
        shutil.rmtree(s.parts_dir, ignore_errors=True)
        shutil.rmtree(s.raw_dir, ignore_errors=True)
        (s.folder / "manifest.json").unlink(missing_ok=True)
        try:
            s.folder.rmdir()
        except OSError:
            pass

    # ---------------------------------------------------------- processor

    def _process(self) -> bool:
        for s in self._active():
            state = s.data["state"]
            if s.data["error"] or state in ("archived", "done"):
                continue
            if state == "discarded":
                files = list(s.raw_dir.glob("*.mkv"))
                if not files and not s.parts_dir.exists():
                    continue                  # cleaned up, the uploader still deletes it on the server
                time.sleep(1.5)               # let OBS close the last file
                for f in files:
                    recycle(f)
                shutil.rmtree(s.parts_dir, ignore_errors=True)
                if not s.data["server"]["opened"]:
                    self._finish(s)
                return True
            try:
                if self._next_segment(s):
                    return True
                if state == "stopped" and self._archive(s):
                    return True
            except Exception as e:
                with s.lock:
                    s.data["error"] = str(e)
                    s.save()
        return False

    def _next_segment(self, s: Session) -> bool:
        """Register and process the next finished segment; False if there is none yet."""
        with s.lock:
            segs = s.data["segments"]
            pending = next((i for i, x in enumerate(segs) if not x["processed"]), None)
        if pending is None:
            files = sorted(s.raw_dir.glob("*.mkv"))
            known = {x["raw"] for x in segs}
            new = [f for f in files if f.name not in known]
            if not new:
                return False
            f = new[0]
            last = f == files[-1]
            if (last and s.data["state"] == "recording") or time.time() - f.stat().st_mtime < SETTLE:
                return False                       # OBS is still writing it
            if s.alttab_open or s.private_open:
                return False                       # its freeze span is not known yet
            start = sum(x["duration"] for x in segs)
            with s.lock:
                segs.append({"raw": f.name, "start": start, "duration": split.duration(f), "processed": False,
                             "sha256": {}, "uploaded": []})
                s.save()
            pending = len(segs) - 1
        seg = segs[pending]
        a0, d = seg["start"], seg["duration"]
        spans = [(max(0.0, a - a0), min(d, b - a0)) for a, b in s.freeze_spans() if b > a0 and a < a0 + d]
        tracks = s.tracks()
        s.parts_dir.mkdir(exist_ok=True)
        split.split_recording(s.raw_dir / seg["raw"], s.part(pending, "screen"),
                              s.part(pending, "camera") if "camera" in tracks else None,
                              freeze=spans, screen_size=tuple(s.data["screen_size"]),
                              mix_out=s.part(pending, "mix") if "mix" in tracks else None,
                              low_priority=s.data["state"] == "recording")
        with s.lock:
            seg["sha256"] = {t: sha256(s.part(pending, t)) for t in tracks}
            seg["processed"] = True
            s.save()
        self.wake.set()                            # the uploader has something new
        return True

    def _archive(self, s: Session) -> bool:
        """After the stop: write the session folder in the archive, then drop the raw segments.

        One folder per recording (<archive>\\<term>\\<module>\\<date> - <topic>), with the file
        names of the FLEX3 contract, so the pipeline and later the server work on the same
        kind of folder: recording.mp4 (program mix), screen.mp4, camera.mp4, session.json, and
        activity.json for the cut agent.
        """
        segs = s.data["segments"]
        if any(not x["processed"] for x in segs):
            return False
        if {f.name for f in s.raw_dir.glob("*.mkv")} - {x["raw"] for x in segs}:
            return False                           # the last segment is still settling
        if not segs:
            raise RuntimeError("OBS hat keine Datei geschrieben")
        a = s.data["archive"]
        folder = free_dir(Path(a["folder"]) / a["stem"])
        for track in s.tracks():
            split.join([s.part(i, track) for i in range(len(segs))], folder / TRACK_FILES[track])
        (folder / "session.json").write_text(json.dumps(session_document(s), ensure_ascii=False, indent=2),
                                             encoding="utf-8")
        if s.data.get("activity"):
            (folder / "activity.json").write_text(json.dumps(
                {"note": "Program in front during the recording: [start, end, process, window title], "
                         "seconds of recording time. Local only, for the cut agent.",
                 "activity": s.data["activity"], "private": s.data.get("private", [])},
                ensure_ascii=False, indent=1), encoding="utf-8")
        with s.lock:
            a["session_folder"] = str(folder)
            a["screen"] = str(folder / TRACK_FILES["screen"])
            a["camera"] = str(folder / TRACK_FILES["camera"]) if s.data["camera"] else None
            s.data["state"] = "archived"
            s.save()
        shutil.rmtree(s.raw_dir, ignore_errors=True)
        if self.on_archived:
            self.on_archived(folder)
        if not s.data["deliver"]:
            self._finish(s)
        self.wake.set()
        return True

    # ---------------------------------------------------------- uploader

    def connection(self) -> str:
        """"local" without a server, else "online", "offline" or "connecting" before the first contact."""
        if not self.client:
            return "local"
        return {True: "online", False: "offline", None: "connecting"}[self.reachable]

    def _online(self) -> None:
        self.server_error, self._retry, self.reachable = "", 0, True
        self.last_contact = time.time()

    def _offline(self, e: Exception) -> None:
        self._retry += 1
        self.server_down_until = time.time() + RETRY[min(self._retry, len(RETRY)) - 1]
        self.server_error, self.reachable = str(e), False

    def _upload(self) -> bool:
        if not self.client or time.time() < self.server_down_until:
            return False
        p = self.profile
        if p is not None and p.status != "no-profile" and p.due():
            try:
                p.sync(self.client)
                p.status = "online"
                self._online()
                return True
            except ServerError as e:
                if e.status != 403:
                    self._offline(e)
                    return False
                p.status = "no-profile"            # a device token without a lecturer: no profile on the server
            except OSError as e:
                self._offline(e)
                return False
        for s in self._active():
            if not s.data["deliver"] or s.data["state"] == "done":
                continue
            try:
                if self._upload_step(s):
                    self._online()
                    return True
            except (OSError, ServerError) as e:
                self._offline(e)
                return False
        if self.reachable is not True or time.time() - self.last_contact > 60:
            try:                                   # nothing to send: still keep the status in the window right
                self.client.health()
                self._online()
            except (OSError, ServerError) as e:
                self._offline(e)
        return False

    def _upload_step(self, s: Session) -> bool:
        sv = s.data["server"]
        if s.data["state"] == "discarded":
            if sv["opened"]:
                self.client.delete(s.id)
            self._finish(s)
            return True
        if not sv["opened"]:
            self.client.open_session(s.id)
            with s.lock:
                sv["opened"] = True
                s.save()
            return True
        for i, seg in enumerate(s.data["segments"]):
            if not seg["processed"]:
                break
            for track, digest in seg["sha256"].items():
                if track not in seg["uploaded"]:
                    self.client.put_part(s.id, track, i, s.part(i, track), digest)
                    with s.lock:
                        seg["uploaded"].append(track)
                        s.save()
                    return True
        if s.data["state"] != "archived" or sv["completed"]:
            return False
        segs = s.data["segments"]
        if any(set(x["sha256"]) - set(x["uploaded"]) for x in segs):
            return False
        n = len(segs)
        meta = session_document(s)
        state = self.client.complete(s.id, meta, {t: n for t in s.tracks()})
        while state == "assembling":
            time.sleep(2)
            st = self.client.status(s.id)
            state = st["state"]
            if st.get("error"):
                raise ServerError(500, st["error"])
        if state != "complete":
            raise ServerError(500, f"unexpected state {state}")
        with s.lock:
            sv["completed"] = True
            s.save()
        self._finish(s)
        return True
