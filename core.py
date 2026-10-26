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

import identify
import monitors                  # sets DPI awareness on import: physical pixels
import obs_remote as remote
import obs_scene as scene
from delivery import Delivery
from hotkeys import HOTKEY_PAUSE, GlobalKeys
from lecturer_profile import Profile, seed_entries
from preview import PreviewServer
from server_client import ServerClient

APP_DIR = Path(os.environ["APPDATA"]) / "flex3-light"
SETTINGS_FILE = APP_DIR / "settings.json"
PROFILE_FILE = APP_DIR / "profile.json"
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
        self.on_capture = lambda exclude: None    # set by the UI: hide its window from capture while recording
        self.message, self.message_kind = "", "info"
        self.obs_state = {"paused": False, "time": "00:00:00"}
        self.screen_size = (scene.FIELD_W, scene.FIELD_H)
        # The modules live in the lecturer's profile; on the very first start it takes over what exists.
        self.profile = Profile(PROFILE_FILE)
        if self.profile.is_new:
            self.profile.seed(seed_entries(self.archive, self.term, self.settings.get("module_file")))
        # Picks up what an earlier run left unfinished, even before OBS is ready.
        self.delivery = Delivery(self.raw_dir, server_client(self.settings), self.profile).start()

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

        # Camera preview for the window; it rests while recording or without a camera.
        self.preview = PreviewServer(scene.CAMERA, lambda: self.session is None
                                     and self.choice.get("camera") != scene.NO_CAMERA)
        self.keys = GlobalKeys(self.events)
        self.keys.start()
        self.meter = LevelMeter()
        self.meter.start()
        threading.Thread(target=self._loop, daemon=True).start()

    # ---------------------------------------------------------- selection

    def data(self) -> dict:
        return {"term": self.term,
                "modules": [m["name"] for m in self.profile.modules()],
                "profile": self.profile.modules(), "user": self.profile.user(), "preview_url": self.preview.url,
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

    def identify_screen(self) -> str:
        """Frame the area of the chosen screen that gets recorded for a moment; returns an error or ''."""
        name = self.choice.get("screen", "")
        m = monitors.match_obs_name(name)
        if not m:
            return "Dieser Bildschirm lässt sich nicht zuordnen."
        g = m["margins"] if self.options["hide_taskbar"] else {"top": 0, "bottom": 0, "left": 0, "right": 0}
        number = next((i + 1 for i, (n, _) in enumerate(self.devices["screen"]) if n == name), 1)
        detail = " · ".join(filter(None, [name.split(":")[0].strip(), f"Bildschirm {number}",
                                          "ohne Taskleiste" if self.options["hide_taskbar"] else ""]))
        identify.show(m["x"] + g["left"], m["y"] + g["top"], m["width"] - g["left"] - g["right"],
                      m["height"] - g["top"] - g["bottom"], "Dieser Bildschirm wird aufgenommen", detail)
        return ""

    def save_module(self, entry: dict) -> str:
        """Add or change a module in the profile; returns an error message for the UI or ''."""
        name = clean_name(entry.get("name", ""))
        if not name:
            return "Ein Modul braucht einen Namen."
        other = self.profile.find(name)
        if other and other["id"] != entry.get("id"):
            return "Ein Modul mit diesem Namen gibt es schon."
        fields = {k: entry.get(k, "") for k in ("code", "language", "ilias_course_id", "opencast_series_id")}
        fields["code"] = re.sub(r"[^a-z0-9-]+", "-", fields["code"].lower()).strip("-")[:40]
        fields["language"] = fields["language"] if re.fullmatch(r"[a-z]{2}", fields["language"] or "") else "de"
        if entry.get("id"):
            self.profile.update(entry["id"], name=name, **fields)
        else:
            self.profile.add(name, **fields)
        self.delivery.wake.set()                  # send it at once if the server is there
        return ""

    def delete_module(self, eid: str) -> None:
        self.profile.delete(eid)
        self.delivery.wake.set()

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
        entry = self.profile.find(module) or self.profile.add(module)    # a module typed in joins the profile
        self.delivery.wake.set()
        s = self.delivery.begin(entry, clean_name(self.topic), self.choice.get("camera") != scene.NO_CAMERA,
                                self.screen_size, self.options["hide_alttab"],
                                self.archive / semester(dt.date.today()) / entry["name"], self.settings,
                                self.profile.user())
        self.on_capture(True)                     # before the first frame, also when started by hotkey
        try:
            with self.lock:
                # Every session gets its own raw folder, so its one-minute segments are easy to tell apart.
                self.o.request("SetRecordDirectory", {"recordDirectory": s.raw_dir.as_posix()})
                self.o.request("StartRecord")
        except Exception:
            self.on_capture(False)
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
                "has_result": bool(info and info["screen"]), "hotkey_errors": list(self.keys.failed),
                "connection": self.delivery.connection(), "profile_status": self.profile.status,
                "profile_pending": self.profile.dirty() and self.delivery.connection() != "local",
                "profile_stamp": f"{self.profile.data['revision']}-{self.profile.data['synced_at']}"}

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
