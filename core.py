"""Core of FLEX3 Light, without user interface.

Holds the OBS connection, device selection, settings, global hotkeys, Alt+Tab
detection and the split after stopping. The UI (recorder.py) polls status()
and calls the commands; OBS requests go through a lock because UI, hotkeys
and the status loop run in different threads.
"""
from __future__ import annotations

import ctypes
import datetime as dt
import json
import math
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

import monitors                  # sets DPI awareness on import: physical pixels
import obs_remote as remote
import obs_scene as scene
import split
from hotkeys import HOTKEY_PAUSE, GlobalKeys

APP_DIR = Path(os.environ["APPDATA"]) / "flex3-light"
SETTINGS_FILE = APP_DIR / "settings.json"
DEFAULT_ARCHIVE = Path.home() / "Videos" / "Lectures"
DEFAULT_RAW = Path.home() / "Videos" / "flex3-light-raw"   # best outside any cloud-synced folder

# File names go into a German archive, so the suffixes stay German.
SCREEN_SUFFIX, CAMERA_SUFFIX = "Bildschirm", "Kamera"

# The recorded picture lags the time OBS reports by about 0.55 s (encoding and muxing),
# measured on 2026-10-09: switcher visible 3.73 to 4.83 s, computed 3.02 to 4.61 s.
VIDEO_DELAY = 0.55


# ------------------------------------------------------------------ helpers

def semester(d: dt.date) -> str:
    """Semester folder name: winter term 1 September to end of February, summer term from 1 March."""
    if d.month >= 9:
        return f"{d.year}-{(d.year + 1) % 100:02d} - WS"
    if d.month <= 2:
        return f"{d.year - 1}-{d.year % 100:02d} - WS"
    return f"{d.year} - SS"


def module_choices(archive: Path, term: str, module_file: str | None) -> list[str]:
    """Folders that already exist for the term first, then modules from an optional Markdown list."""
    existing = sorted(p.name for p in (archive / term).glob("*") if p.is_dir()) if (archive / term).exists() else []
    names: list[str] = []
    if module_file and Path(module_file).exists():
        names = [m.group(1).strip() for m in re.finditer(r"^## (.+)$", Path(module_file).read_text(encoding="utf-8"), re.M)]
        names = [n for n in names if n != "Wie ein Modul beschrieben wird"]
    return existing + [n for n in names if n not in existing]


def clean_name(text: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "", text).strip().rstrip(".")


def free_path(path: Path) -> Path:
    n, p = 2, path
    while p.exists():
        p = path.with_name(f"{path.stem} ({n}){path.suffix}")
        n += 1
    return p


def recycle(path: Path) -> None:
    """Move a file to the recycle bin (falls back to deleting it if Windows refuses)."""
    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", ctypes.c_void_p), ("wFunc", ctypes.c_uint), ("pFrom", ctypes.c_wchar_p),
                    ("pTo", ctypes.c_wchar_p), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", ctypes.c_int),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_wchar_p)]
    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
    op = SHFILEOPSTRUCTW(wFunc=FO_DELETE, pFrom=str(path) + "\0",
                         fFlags=FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI)
    if ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) != 0 and path.exists():
        path.unlink()


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return _migrate_old_settings()


def _migrate_old_settings() -> dict:
    """One-off: take over the settings of the lecture recorder this app was forked from.

    The raw folder is not taken over, so the two apps never write into the same one.
    """
    old = Path(os.environ["APPDATA"]) / "lecture-recorder" / "settings.json"
    try:
        s = json.loads(old.read_text(encoding="utf-8"))
    except Exception:
        return {}
    s.pop("raw_dir", None)
    save_settings(s)
    return s


def save_settings(s: dict) -> None:
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


class LevelMeter(threading.Thread):
    """Peak level of the microphone in dBFS, from OBS volume meter events."""

    def __init__(self):
        super().__init__(daemon=True)
        self.db = -90.0
        self.at = 0.0

    def run(self) -> None:
        while True:
            try:
                with remote.Obs(events=remote.Obs.VOLUME_METERS) as o:
                    while True:
                        e = o.event(timeout=5)
                        if not e or e.get("eventType") != "InputVolumeMeters":
                            continue
                        for i in e["eventData"]["inputs"]:
                            if i["inputName"] == scene.MICROPHONE and i["inputLevelsMul"]:
                                peak = max(ch[1] for ch in i["inputLevelsMul"])
                                self.db = 20 * math.log10(peak) if peak > 0 else -90.0
                                self.at = time.time()
            except Exception:
                time.sleep(2)


# ------------------------------------------------------------------ core

class Core:
    def __init__(self):
        self.lock = threading.RLock()
        self.events: queue.Queue = queue.Queue()
        self.settings = load_settings()
        self.archive = Path(self.settings.get("archive_dir") or DEFAULT_ARCHIVE)
        self.raw_dir = Path(self.settings.get("raw_dir") or DEFAULT_RAW)
        self.term = semester(dt.date.today())
        self.module = self.settings.get("module", "")
        self.topic = ""
        self.options = {"hide_taskbar": self.settings.get("hide_taskbar", True),
                        "hide_alttab": self.settings.get("hide_alttab", True)}
        self.session: dict | None = None
        self.offset: float | None = None          # wall clock minus recording time
        self.alttab_start: float | None = None
        self.stop_pressed = 0.0
        self.asking = False                       # stopped by the user, waiting for "save or discard"
        self.on_question = lambda: None           # set by the UI: bring the window back for the question
        self.splitting = False
        self.progress = 0.0
        self.message, self.message_kind = "", "info"
        self.last_file: Path | None = None
        self.obs_state = {"paused": False, "time": "00:00:00"}
        self.screen_size = (scene.FIELD_W, scene.FIELD_H)

        remote.start(profile=scene.NAME, collection=scene.NAME)
        self.o = remote.Obs().__enter__()
        with self.lock:
            scene.ensure_profile_and_scene(self.o, self.raw_dir)
            self.devices = scene.devices(self.o)
        self.devices["camera"] = self.devices["camera"] + [(scene.NO_CAMERA, scene.NO_CAMERA)]
        guess = {"screen": lambda n: "Haupt" in n or "Primary" in n, "camera": lambda n: "C920" in n,
                 "microphone": lambda n: "DJI" in n or "C920" in n}
        self.choice = {}
        for kind, items in self.devices.items():
            names = [n for n, _ in items]
            saved = self.settings.get(kind)
            self.choice[kind] = saved if saved in names else next(
                (n for n in names if guess[kind](n)), names[0] if names else "")
        self.apply_devices()

        self.keys = GlobalKeys(self.events)
        self.keys.start()
        self.meter = LevelMeter()
        self.meter.start()
        threading.Thread(target=self._loop, daemon=True).start()

    # ---------------------------------------------------------- selection

    def data(self) -> dict:
        return {"term": self.term,
                "modules": module_choices(self.archive, self.term, self.settings.get("module_file")),
                "module": self.module,
                "devices": {k: [n for n, _ in v] for k, v in self.devices.items()},
                "choice": dict(self.choice), "archive_dir": str(self.archive), "raw_dir": str(self.raw_dir),
                "options": dict(self.options)}

    def _device_id(self, kind: str) -> str | None:
        return next((i for n, i in self.devices[kind] if n == self.choice.get(kind)), None)

    def apply_devices(self) -> None:
        with self.lock:
            scene.apply(self.o, screen=self._device_id("screen"), camera=self._device_id("camera"),
                        microphone=self._device_id("microphone"))
            m = monitors.match_obs_name(self.choice.get("screen", ""))
            if m:
                margins = m["margins"] if self.options["hide_taskbar"] else None
                self.screen_size = scene.crop_screen(self.o, m["width"], m["height"], margins)
            else:
                self.screen_size = (scene.FIELD_W, scene.FIELD_H)
        self.save()

    def select(self, kind: str, name: str) -> None:
        if self.session is None and name in [n for n, _ in self.devices[kind]]:
            self.choice[kind] = name
            self.apply_devices()

    def set_option(self, name: str, value: bool) -> None:
        if self.session is None and name in self.options:
            self.options[name] = bool(value)
            self.apply_devices() if name == "hide_taskbar" else self.save()

    def set_text(self, module: str, topic: str) -> None:
        if self.session is None:
            self.module, self.topic = module, topic
            self.save()

    def set_folder(self, kind: str, path: str) -> None:
        if self.session is not None or self.splitting or not path:
            return
        if kind == "archive_dir":
            self.archive = Path(path)
        elif kind == "raw_dir":
            self.raw_dir = Path(path)
            with self.lock:
                scene.ensure_profile_and_scene(self.o, self.raw_dir)
            self.apply_devices()
        self.save()

    def save(self) -> None:
        self.settings.update({"module": self.module, **self.choice, "archive_dir": str(self.archive),
                              "raw_dir": str(self.raw_dir), **self.options})
        save_settings(self.settings)

    # ---------------------------------------------------------- commands

    def start(self) -> str:
        """Start recording; returns an error message for the UI or ''."""
        if self.session is not None:
            return ""
        if self.splitting:
            return "Die letzte Aufnahme wird noch geteilt."
        module = clean_name(self.module)
        if not module:
            return "Bitte zuerst ein Modul wählen."
        self.save()
        with self.lock:
            self.o.request("StartRecord")
        self.session = {"module": module, "topic": clean_name(self.topic), "date": dt.date.today(),
                        "camera": self.choice.get("camera") != scene.NO_CAMERA, "alttab": [],
                        "screen_size": self.screen_size}
        self.offset, self.alttab_start = None, None
        self.message, self.last_file = "", None
        return ""

    def pause(self) -> None:
        if self.session is None or self.asking:
            return
        with self.lock:
            if self.o.request("GetRecordStatus")["outputPaused"]:
                self.o.request("ResumeRecord")
            else:
                self.o.request("PauseRecord")

    def stop(self) -> None:
        """Pause the recording and ask whether to save it; finish() or resume() answers."""
        if self.session is None or self.asking:
            return
        with self.lock:
            if not self.o.request("GetRecordStatus")["outputPaused"]:
                self.o.request("PauseRecord")
        self.asking = True
        try:
            self.on_question()
        except Exception:
            pass

    def resume(self) -> None:
        """Answer "continue recording" to the save question."""
        if self.session is None or not self.asking:
            return
        with self.lock:
            self.o.request("ResumeRecord")
        self.asking = False

    def finish(self, save: bool) -> None:
        """End the recording: split and file it, or move the raw file to the recycle bin."""
        if self.session is None:
            return
        with self.lock:
            raw = Path(self.o.request("StopRecord")["outputPath"])
        s, self.session, self.asking = self.session, None, False
        if not save:
            threading.Thread(target=self._discard, args=(raw,), daemon=False).start()
            return
        folder = self.archive / semester(s["date"]) / s["module"]
        stem = f"{s['date']:%Y-%m-%d} - {s['topic'] or s['module']}"
        screen_out = free_path(folder / f"{stem} - {SCREEN_SUFFIX}.mp4")
        camera_out = free_path(folder / f"{stem} - {CAMERA_SUFFIX}.mp4") if s["camera"] else None
        spans = s["alttab"] if self.options["hide_alttab"] else []
        self.splitting, self.progress = True, 0.0
        threading.Thread(target=self._split, args=(raw, screen_out, camera_out, spans, s["screen_size"]),
                         daemon=False).start()

    def _split(self, raw: Path, screen_out: Path, camera_out: Path | None, spans: list, size) -> None:
        try:
            time.sleep(1.5)   # let OBS close the file
            split.split_recording(raw, screen_out, camera_out, lambda x: setattr(self, "progress", x),
                                  freeze=spans, screen_size=size)
            raw.unlink()
            self.last_file = screen_out
            extra = f" · {len(spans)} Fensterwechsel ausgeblendet" if spans else ""
            self.message, self.message_kind = f"Abgelegt in {screen_out.parent.name}{extra}", "ok"
        except Exception as e:
            self.last_file = raw
            self.message, self.message_kind = f"Teilen fehlgeschlagen, die Rohaufnahme bleibt erhalten: {e}", "error"
        finally:
            self.splitting = False

    def _discard(self, raw: Path) -> None:
        time.sleep(1.5)   # let OBS close the file
        try:
            recycle(raw)
            self.message, self.message_kind = "Aufnahme verworfen, die Rohdatei liegt im Papierkorb", "info"
        except Exception as e:
            self.message, self.message_kind = f"Verwerfen fehlgeschlagen, die Rohdatei liegt noch in {raw.parent}: {e}", "error"

    def open_result(self) -> None:
        if self.last_file and self.last_file.exists():
            subprocess.Popen(["explorer", "/select,", str(self.last_file)])
        elif self.last_file:
            os.startfile(self.last_file.parent)

    # ---------------------------------------------------------- status loop

    def status(self) -> dict:
        db = self.meter.db if time.time() - self.meter.at < 1 else -90.0
        return {"recording": self.session is not None, "asking": self.asking, "paused": self.obs_state["paused"],
                "time": self.obs_state["time"], "level": round(db, 1), "splitting": self.splitting,
                "progress": round(self.progress, 3), "message": self.message, "message_kind": self.message_kind,
                "has_result": bool(self.last_file), "hotkey_errors": list(self.keys.failed)}

    def _loop(self) -> None:
        while True:
            try:
                if self.session is not None:
                    with self.lock:
                        st = self.o.request("GetRecordStatus")
                    self.offset = None if st["outputPaused"] else time.time() - st["outputDuration"] / 1000
                    self.obs_state = {"paused": st["outputPaused"], "time": st["outputTimecode"].split(".")[0]}
                while not self.events.empty():
                    self._handle(*self.events.get())
            except Exception as e:
                self.message, self.message_kind = f"Verbindung zu OBS gestört: {e}", "error"
                try:
                    with self.lock:
                        self.o = remote.Obs().__enter__()
                except Exception:
                    pass
            time.sleep(0.15)

    def _handle(self, kind: str, value) -> None:
        if kind == "hotkey":
            if value == HOTKEY_PAUSE:
                self.pause()
            elif self.session is None:
                error = self.start()
                if error:
                    self.message, self.message_kind = error, "error"
            elif self.asking:
                pass
            elif time.time() - self.stop_pressed > 3:      # stop needs a second press within 3 s
                self.stop_pressed = time.time()
                self.message, self.message_kind = "Zum Beenden Strg+Alt+R noch einmal drücken", "info"
            else:
                self.stop()
        elif kind == "alttab" and self.session is not None and self.offset is not None:
            edge, wall = value
            if edge == "down":
                self.alttab_start = wall - self.offset
            elif self.alttab_start is not None:
                # shift by the video delay, plus margin for the switcher fading in and out
                a = self.alttab_start + VIDEO_DELAY - 0.25
                b = wall - self.offset + VIDEO_DELAY + 0.5
                self.session["alttab"].append((max(0.0, a), b))
                self.alttab_start = None
