"""Core of FLEX3 Light, without user interface.

Holds the OBS connection, device selection, settings, global hotkeys and Alt+Tab
detection, and hands every recording to delivery.py, which processes and delivers
it piece by piece while it runs. The UI (recorder.py) polls status() and calls the
commands; OBS requests go through a lock because UI, hotkeys and the status loop
run in different threads.
"""
from __future__ import annotations

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
from delivery import Delivery
from hotkeys import HOTKEY_PAUSE, GlobalKeys
from server_client import ServerClient

APP_DIR = Path(os.environ["APPDATA"]) / "flex3-light"
SETTINGS_FILE = APP_DIR / "settings.json"
DEFAULT_ARCHIVE = Path.home() / "Videos" / "Lectures"
DEFAULT_RAW = Path.home() / "Videos" / "flex3-light-raw"   # best outside any cloud-synced folder

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


def server_client(settings: dict) -> ServerClient | None:
    """The FLEX3 Light server from the settings, or None: then recordings are only archived locally."""
    if settings.get("server_url") and settings.get("server_token"):
        return ServerClient(settings["server_url"], settings["server_token"], settings.get("upload_kbit"))
    return None


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
        self.session = None                       # delivery.Session while recording
        self.offset: float | None = None          # wall clock minus recording time
        self.alttab_start: float | None = None
        self.stop_pressed = 0.0
        self.asking = False                       # stopped by the user, waiting for "save or discard"
        self.on_question = lambda: None           # set by the UI: bring the window back for the question
        self.message, self.message_kind = "", "info"
        self.obs_state = {"paused": False, "time": "00:00:00"}
        self.screen_size = (scene.FIELD_W, scene.FIELD_H)
        # Picks up what an earlier run left unfinished, even before OBS is ready.
        self.delivery = Delivery(self.raw_dir, server_client(self.settings)).start()

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
        if self.session is not None or self.delivery.busy() or not path:
            return
        if kind == "archive_dir":
            self.archive = Path(path)
        elif kind == "raw_dir":
            self.raw_dir = self.delivery.root = Path(path)
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
        if self.delivery.busy():
            return "Die letzte Aufnahme wird noch abgelegt."
        module = clean_name(self.module)
        if not module:
            return "Bitte zuerst ein Modul wählen."
        self.save()
        s = self.delivery.begin(module, clean_name(self.topic), self.choice.get("camera") != scene.NO_CAMERA,
                                self.screen_size, self.options["hide_alttab"],
                                self.archive / semester(dt.date.today()) / module, self.settings)
        try:
            with self.lock:
                # Every session gets its own raw folder, so its one-minute segments are easy to tell apart.
                self.o.request("SetRecordDirectory", {"recordDirectory": s.raw_dir.as_posix()})
                self.o.request("StartRecord")
        except Exception:
            self.delivery.forget(s)
            raise
        self.session = s
        self.offset, self.alttab_start = None, None
        self.message = ""
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
        """End the recording: archive and deliver it, or move it to the recycle bin (and off the server)."""
        if self.session is None:
            return
        with self.lock:
            self.o.request("StopRecord")
            # OBS refuses a new record directory until the output has really stopped.
            deadline = time.time() + 10
            while self.o.request("GetRecordStatus")["outputActive"] and time.time() < deadline:
                time.sleep(0.1)
            self.o.request("SetRecordDirectory", {"recordDirectory": self.raw_dir.as_posix()}, check=False)
        s, self.session, self.asking = self.session, None, False
        self.delivery.stop(s, save)
        self.message = ""

    def _result(self) -> Path | None:
        s = self.delivery.latest()
        screen = s and s is not self.session and s.data["archive"]["screen"]
        return Path(screen) if screen else None

    def open_result(self) -> None:
        f = self._result()
        if f and f.exists():
            subprocess.Popen(["explorer", "/select,", str(f)])
        elif f:
            os.startfile(f.parent)

    # ---------------------------------------------------------- status loop

    @staticmethod
    def _delivery_message(info: dict) -> tuple[str, str]:
        if info["error"]:
            return f"Ablegen fehlgeschlagen, die Rohaufnahme bleibt erhalten: {info['error']}", "error"
        if not info["screen"]:
            return ("Aufnahme verworfen, die Rohdateien liegen im Papierkorb", "info") \
                if info["state"] in ("discarded", "done") else ("", "info")
        text = f"Abgelegt in {info['module']}"
        if info["alttab"]:
            text += f" · {info['alttab']} Fensterwechsel ausgeblendet"
        if not info["deliver"]:
            return text, "ok"
        if info["completed"]:
            return text + " · an den Server übertragen", "ok"
        if info["server_error"]:
            return text + " · Server nicht erreichbar, die Übertragung folgt", "info"
        return text + f" · Übertragung {info['uploaded']} von {info['parts']} Teilen", "info"

    def status(self) -> dict:
        db = self.meter.db if time.time() - self.meter.at < 1 else -90.0
        s = self.delivery.latest()
        info = self.delivery.summary(s) if s is not None and s is not self.session else None
        splitting = bool(info and info["state"] == "stopped" and not info["error"])
        progress = info["processed"] / info["segments"] if splitting and info["segments"] else 0.0
        message, kind = self.message, self.message_kind
        if info and not message:
            message, kind = self._delivery_message(info)
        return {"recording": self.session is not None, "asking": self.asking, "paused": self.obs_state["paused"],
                "time": self.obs_state["time"], "level": round(db, 1), "splitting": splitting,
                "progress": round(progress, 3), "message": message, "message_kind": kind,
                "has_result": bool(info and info["screen"]), "hotkey_errors": list(self.keys.failed)}

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
                self.session.alttab_open = True        # no segment is cut until the span is known
            elif self.alttab_start is not None:
                # shift by the video delay, plus margin for the switcher fading in and out
                a = self.alttab_start + VIDEO_DELAY - 0.25
                b = wall - self.offset + VIDEO_DELAY + 0.5
                self.session.add_span(max(0.0, a), b)
                self.alttab_start = None
                self.session.alttab_open = False
