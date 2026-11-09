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
import shutil
import threading
import time
import webbrowser
import zipfile
from pathlib import Path

import activity
import hardware
import identify
import library
import services
from recordings import Recordings
import monitors                  # sets DPI awareness on import: physical pixels
import obs_remote as remote
import obs_scene as scene
from delivery import Delivery, recycle
from hotkeys import HOTKEY_PAUSE, HOTKEY_PRIVATE, GlobalKeys
from lecturer_profile import Profile, seed_entries
from pipeline import binaries, cut_rules, publish, sounds
from pipeline_runner import PipelineRunner
from preview import PreviewServer
import server_client as server_api
from server_client import ServerClient

APP_DIR = Path(os.environ["APPDATA"]) / "flex3-light"
SETTINGS_FILE = APP_DIR / "settings.json"
PROFILE_FILE = APP_DIR / "profile.json"
PIPELINE_QUEUE = APP_DIR / "pipeline-queue.json"
DEFAULT_ARCHIVE = Path.home() / "Videos" / "Lectures"
DEFAULT_RAW = Path.home() / "Videos" / "flex3-light-raw"   # best outside any cloud-synced folder

# The recorded picture lags the time OBS reports by about 0.55 s (encoding and muxing),
# measured on 2026-10-09: switcher visible 3.73 to 4.83 s, computed 3.02 to 4.61 s.
VIDEO_DELAY = 0.55


# ------------------------------------------------------------------ helpers

semester = library.semester


def clean_name(text: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "", text).strip().rstrip(".")


SERVER_SECRET = "server"     # name of the device token in the Windows Credential Manager


def server_client(settings: dict) -> ServerClient | None:
    """The FLEX3 Light server from the settings, or None: then recordings are only archived locally.

    The address is a setting, the device token lives in the Credential Manager (since
    2026-11-06; a token still in settings.json is moved there on start)."""
    token = services.credentials.get(SERVER_SECRET) if settings.get("server_url") else None
    if settings.get("server_url") and token:
        return ServerClient(settings["server_url"], token, settings.get("upload_kbit"))
    return None


def server_api_split(code: str) -> tuple[str, str]:
    """(key, address) from a connection code "<key>@<address>" or a bare key."""
    key, _, url = (code or "").strip().partition("@")
    return key.strip(), url.strip().rstrip("/")


def _move_token_out_of_settings(settings: dict) -> None:
    if settings.get("server_token"):
        services.credentials.put(SERVER_SECRET, settings.pop("server_token"))
        save_settings(settings)


PROFILE_SETTINGS = ("after_recording", "language")     # with "cut", the lecturer's and not the device's


def _move_preferences_to_profile(settings: dict, profile: Profile) -> None:
    """One-off (2026-11-09): the cut settings, what starts after a recording and the default
    language belong to the lecturer's profile now, which the server keeps for all their devices;
    settings.json keeps only what belongs to this computer."""
    if not any(k in settings for k in (*PROFILE_SETTINGS, "cut")):
        return
    values = {k: settings[k] for k in PROFILE_SETTINGS if k in settings}
    values.update({f"cut.{k}": v for k, v in (settings.get("cut") or {}).items()
                   if k != "auto"})          # the old switch, replaced by after_recording on 2026-11-04
    if values.get("cut.cut_titles") == []:
        del values["cut.cut_titles"]        # never chosen (the window has no field for it): the default applies
    profile.seed_preferences(values)
    for k in (*PROFILE_SETTINGS, "cut"):
        settings.pop(k, None)
    save_settings(settings)


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

class Core(Recordings):
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
        self.popup_start: float | None = None     # a pop-up of the taskbar is visible on the recorded screen
        self.private_start: float | None = None   # privacy key pressed at this recording time
        self.rec_time = 0.0                       # recording time of the last status, also while paused
        self.tracker: activity.Tracker | None = None
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
        _move_preferences_to_profile(self.settings, self.profile)
        _move_token_out_of_settings(self.settings)
        self.delivery = Delivery(self.raw_dir, server_client(self.settings), self.profile)
        # Transcription and the rest of the pipeline run on filed recordings, never during a recording.
        binaries.configure(hardware.ffmpeg(), typst=hardware.typst())   # the bundled ffmpeg too; decoding on the GPU measured slower (2026-10-29)
        self.pipeline = PipelineRunner(PIPELINE_QUEUE, lambda: self.session is None, self._terms_for,
                                       lambda folder: self.cut_settings(), hardware.ffmpeg_video_args(),
                                       APP_DIR / "sounds").start()
        self.delivery.on_archived = self._archived

        remote.start(profile=scene.NAME, collection=scene.NAME)
        self.o = remote.Obs().__enter__()
        # If the app ended during a recording without stopping it, OBS still records into that
        # session's raw folder: take it over instead of leaving it running with nobody to stop it.
        still_recording = None
        if self.o.request("GetRecordStatus")["outputActive"]:
            folder = Path(self.o.request("GetRecordDirectory")["recordDirectory"])
            if folder.name != "raw" or not (folder.parent / "manifest.json").exists():
                raise remote.ObsError("OBS nimmt gerade auf, aber nicht für FLEX3 Light. "
                                      "Bitte die Aufnahme in OBS beenden und FLEX3 Light neu starten.")
            still_recording = folder
        # Picks up what an earlier run left unfinished.
        self.delivery.start(still_recording)
        self.session = self.delivery.running()
        if self.session is not None:
            self.screen_size = tuple(self.session.data["screen_size"])
            self.tracker = activity.Tracker(self.session.add_activity)
        with self.lock:
            if self.session is None:               # OBS cannot switch profiles while it records
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
        if self.session is None:                   # never change the sources of a running recording
            self.apply_devices()

        # Camera preview for the window; it rests while recording or without a camera.
        self.preview = PreviewServer(scene.CAMERA, lambda: self.session is None
                                     and self.choice.get("camera") != scene.NO_CAMERA, self._media)
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
                "media_base": self.preview.media_base,
                "api_keys": services.status(),
                "module": self.module,
                "devices": {k: [n for n, _ in v] for k, v in self.devices.items()},
                "choice": dict(self.choice), "archive_dir": str(self.archive), "raw_dir": str(self.raw_dir),
                "publish_dir": str(self.publish_dir()),
                "options": dict(self.options), "cut": self.cut_settings(), "after_recording": self.after_recording(),
                "pipeline_place": self.profile.preference("pipeline") or "device",
                "server": self.server_info()}

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
        elif kind == "publish_dir":
            self.settings["publish_dir"] = str(Path(path))
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

    # ---------------------------------------------------------- AI service keys

    def set_api_key(self, service: str, secret: str) -> str:
        """Store a key from the settings in the Credential Manager; returns an error or ''."""
        secret = (secret or "").strip()
        if service not in services.SERVICES:
            return "Unbekannter Dienst."
        if not secret:
            return "Bitte einen Schlüssel einfügen."
        services.credentials.put(service, secret)
        return ""

    def delete_api_key(self, service: str) -> None:
        if service in services.SERVICES:
            services.credentials.delete(service)

    # ---------------------------------------------------------- server connection

    def server_info(self) -> dict:
        """What the settings show about the connection; never the token."""
        url = self.settings.get("server_url", "")
        return {"url": url if self.delivery.client else "", "device_id": self.settings.get("device_id", ""),
                "user_name": self.profile.data.get("user_name", ""), "connection": self.delivery.connection()}

    def connect_server(self, code: str) -> str:
        """Register this computer with a connection code ("<key>@<address>"); returns an error or ''."""
        if self.session is not None:
            return "Während einer Aufnahme lässt sich die Verbindung nicht ändern."
        key, url = server_api_split(code)
        url = url or self.settings.get("server_url", "")
        if not key:
            return "Bitte den Verbindungscode einfügen."
        if not url:
            return "Im Code fehlt die Adresse des Servers (Schlüssel@Adresse)."
        if not url.startswith(("http://", "https://")):
            return "Die Adresse muss mit http:// oder https:// beginnen."
        try:
            answer = server_api.register(url, key, os.environ.get("COMPUTERNAME", "Rechner"))
        except server_api.ServerError as e:
            return str(e.detail) if e.status == 403 else f"Der Server lehnt ab: {e}"
        except OSError as e:
            return f"Der Server ist nicht erreichbar: {getattr(e, 'reason', e)}"
        services.credentials.put(SERVER_SECRET, answer["token"])
        self.settings.update({"server_url": url, "device_id": answer["device_id"]})
        save_settings(self.settings)
        self.delivery.client = server_client(self.settings)
        self.delivery.reachable, self.delivery.server_down_until = None, 0.0
        self.profile.status = "local"
        self.delivery.wake.set()                 # first sync of the profile right away
        return ""

    def disconnect_server(self) -> str:
        """Back to recording locally only; the server keeps what it has."""
        if self.session is not None:
            return "Während einer Aufnahme lässt sich die Verbindung nicht ändern."
        services.credentials.delete(SERVER_SECRET)
        for k in ("server_url", "device_id"):
            self.settings.pop(k, None)
        save_settings(self.settings)
        self.delivery.client = None
        self.profile.status = "local"
        return ""

    def check_api_key(self, service: str) -> dict:
        if service not in services.SERVICES:
            return {"ok": False, "message": "Unbekannter Dienst."}
        ok, message = services.check(service)
        return {"ok": ok, "message": message}

    def save_module(self, entry: dict) -> str:
        """Add or change a module in the profile; returns an error message for the UI or ''."""
        name = clean_name(entry.get("name", ""))
        if not name:
            return "Ein Modul braucht einen Namen."
        other = self.profile.find(name)
        if other and other["id"] != entry.get("id"):
            return "Ein Modul mit diesem Namen gibt es schon."
        fields = {k: entry.get(k, "") for k in ("code", "language", "ilias_course_id", "opencast_series_id")}
        raw = entry.get("terms", "")             # one per line or separated by commas, from the editor
        terms = raw if isinstance(raw, list) else re.split(r"[,;\n]", raw or "")
        fields["terms"] = list(dict.fromkeys(t.strip()[:80] for t in terms if t.strip()))[:300]
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
                                self.archive / semester(dt.date.today()) / entry["name"],
                                {**self.settings, "language": self.profile.preference("language", "de")},
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
        self.tracker = activity.Tracker(s.add_activity)
        self.offset, self.alttab_start, self.private_start, self.popup_start = None, None, None, None
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
        t = self._now()                           # close what is still open at the end of the recording
        self._close_private(t)
        if self.tracker:
            self.tracker.close(t)
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

    def _terms_for(self, meta: dict) -> list[str]:
        """The technical terms of the recording's module, for the transcription prompt."""
        m = self.profile.find(meta.get("course_name", ""))
        return (m or {}).get("terms", [])

    def _now(self) -> float:
        """Current recording time in seconds."""
        return time.time() - self.offset if self.offset is not None else self.rec_time

    def toggle_private(self) -> None:
        """Privacy key (Ctrl+Alt+H or the button in the bar): freeze the screen track from now
        until it is pressed again, while sound and camera go on, as for Alt+Tab."""
        if self.session is None or self.asking:
            return
        if self.private_start is None:
            self.private_start = self._now()
            self.session.private_open = True     # no segment is cut until the span is known
        else:
            self._close_private(self._now())

    def _close_private(self, t: float) -> None:
        if self.private_start is None or self.session is None:
            return
        # shifted by the video delay like Alt+Tab, with half a second of margin on both sides
        self.session.add_private(max(0.0, self.private_start + VIDEO_DELAY - 0.5), t + VIDEO_DELAY + 0.5)
        self.private_start = None
        self.session.private_open = False

    def _result(self) -> Path | None:
        s = self.delivery.latest()
        screen = s and s is not self.session and s.data["archive"]["screen"]
        return Path(screen) if screen else None

    # ---------------------------------------------------------- recordings ("Meine Aufnahmen", recordings.py)

    def _scan(self) -> dict[str, Path]:
        return library.scan(self.archive)

    def _find_module(self, name: str) -> dict | None:
        return self.profile.find(name)

    def _add_term(self, module: dict, term: str) -> None:
        self.profile.update(module["id"], terms=(module.get("terms", []) + [term])[:300])
        self.delivery.wake.set()

    @staticmethod
    def _discard(folder: Path) -> None:
        recycle(folder)

    # ---------------------------------------------------------- after a recording

    AFTER_RECORDING = cut_rules.AFTER_RECORDING

    def after_recording(self) -> str:
        """What starts by itself once a recording is filed: nothing (the default, decided 2026-11-04),
        the transcription, or the transcription and then the cut proposal."""
        return cut_rules.after_recording(self.profile.preferences())

    def pipeline_place(self) -> str:
        """Where filed recordings are worked on: "device" (this computer, the default until the review
        runs on the server) or "server". A preference of the profile, so a lecture is never worked
        on twice and paid twice."""
        connected = getattr(self.delivery, "client", None) is not None
        return "server" if self.profile.preference("pipeline") == "server" and connected else "device"

    def set_pipeline_place(self, value: str) -> None:
        if value in ("device", "server") and self.profile.set_preferences({"pipeline": value}):
            self.delivery.wake.set()

    def set_after_recording(self, value: str) -> None:
        if value in self.AFTER_RECORDING and self.profile.set_preferences({"after_recording": value}):
            self.delivery.wake.set()              # the profile goes to the server at once

    def _archived(self, folder: Path) -> None:
        if self.after_recording() != "nothing" and self.pipeline_place() == "device":
            self.pipeline.enqueue(folder)

    # ---------------------------------------------------------- the cut: settings and approval

    def cut_settings(self) -> dict:
        """The lecturer's settings for the cut, from the profile: propose by itself, pauses, programs to
        cut or keep, cards and sounds; "sounds" lists the sounds already made."""
        return {**cut_rules.settings_from(self.profile.preferences()),
                "sounds": sorted(sounds.available(APP_DIR / "sounds"))}

    def set_cut_settings(self, values: dict) -> None:
        # one preference per field, so a change here does not undo another device's change of a different one
        if self.profile.set_preferences(cut_rules.cut_preferences(self.profile.preferences("cut."), values)):
            self.delivery.wake.set()

    def open_summary(self, sid: str) -> str:
        folder = getattr(self, "_library", {}).get(sid)
        pdf = publish.locate(folder, "summary.pdf") if folder else None
        if pdf is None:
            return "Es gibt noch keine Zusammenfassung."
        os.startfile(pdf)
        return ""

    # ---------------------------------------------------------- publishing

    def publish_dir(self) -> Path:
        """Root of the folders the lecturer uploads from; next to the recordings unless chosen."""
        return Path(self.settings.get("publish_dir") or self.archive / "Veröffentlichen")

    def publish_plan(self, sid: str) -> dict:
        """Where the files of a recording would go, for the window to show before moving them."""
        folder = getattr(self, "_library", {}).get(sid)
        if folder is None or not folder.exists():
            return {"folder": "", "files": []}
        moves = publish.plan(folder, self.publish_dir())
        return {"folder": str(publish.target_dir(folder, self.publish_dir())), "files": [t.name for _, t in moves]}

    def publish(self, sid: str) -> str:
        """Move the finished files of a recording into the publish folder; returns an error or ''."""
        folder = getattr(self, "_library", {}).get(sid)
        if folder is None or not folder.exists():
            return "Die Aufnahme ist nicht mehr in der Ablage."
        if library.stations(folder)["cut"] != "done":
            return "Veröffentlichen geht erst, wenn das geschnittene Video fertig ist."
        with self.pipeline.lock:
            if self.pipeline.active == str(folder):
                return "An dieser Aufnahme wird gerade gearbeitet. Versuch es gleich noch einmal."
            try:
                publish.run(folder, self.publish_dir(), discard=recycle)
            except (OSError, ValueError) as e:
                return str(e) if isinstance(e, (FileExistsError, FileNotFoundError, ValueError)) \
                    else f"Verschieben fehlgeschlagen: {e}"
        return ""

    # ---------------------------------------------------------- "Meine Aufnahmen" on the server (phase 4)

    def server_library(self, browser: bool = False) -> dict:
        """{"url"} of the page "Meine Aufnahmen" on the server with a one-time ticket, for the frame in
        the window, or opened in the default browser (browser=True); {"error"} when that fails."""
        client = self.delivery.client
        if client is None:
            return {"error": "Kein Server verbunden."}
        try:
            url = client.url + client.library_ticket()["path"]
        except (server_api.ServerError, OSError, KeyError):
            return {"error": "Der Server ist gerade nicht erreichbar. Deine Aufnahmen dort erscheinen, sobald er wieder da ist."}
        if browser:
            webbrowser.open(url)
            return {"url": ""}
        return {"url": url}

    def open_server_url(self, url: str) -> str:
        """Open a page of "Meine Aufnahmen" on the server in the browser, e.g. the summary; nothing else."""
        client = self.delivery.client
        if client is None or not url.startswith(client.url + "/library/"):
            return "Diese Adresse öffnet FLEX3 Light nicht."
        webbrowser.open(url)
        return ""

    def fetch_package(self, sid: str) -> str:
        """Take the package of a recording published on the server into the folder for uploads, under
        <term>\\<module> with the names Opencast expects, as publishing on this computer does; then show
        the folder. Files of an earlier package with the same names are replaced. Returns an error or ''."""
        client = self.delivery.client
        if client is None:
            return "Kein Server verbunden."
        try:
            plan = client.library_call("publish_plan", sid)
        except (server_api.ServerError, OSError) as e:
            return f"Der Server ist gerade nicht erreichbar: {e}"
        term, module = str(plan.get("term", "")), publish.clean(str(plan.get("module", "")))
        if not plan.get("files") or not library.TERM.match(term) or not module:
            return "Für diese Aufnahme gibt es kein Paket."
        target = self.publish_dir() / term / module
        target.mkdir(parents=True, exist_ok=True)
        package = target / f".download-{sid}.zip"
        try:
            client.download(f"/library/package/{sid}", package)
            with zipfile.ZipFile(package) as z:
                for info in z.infolist():
                    name = info.filename
                    if not publish.flat_name(name):                # only plain names, never a path
                        continue
                    part = target / (name + ".part")
                    with z.open(info) as src, open(part, "wb") as dst:
                        shutil.copyfileobj(src, dst, 1 << 20)
                    os.replace(part, target / name)
        except (server_api.ServerError, OSError, zipfile.BadZipFile) as e:
            return f"Das Paket ließ sich nicht holen: {e}"
        finally:
            package.unlink(missing_ok=True)
        os.startfile(target)
        return ""

    def open_published(self, sid: str) -> None:
        folder = getattr(self, "_library", {}).get(sid)
        target = publish.published(folder).get("folder") if folder else None
        if target and Path(target).exists():
            os.startfile(target)

    def open_recording(self, sid: str) -> None:
        folder = getattr(self, "_library", {}).get(sid)
        if folder and folder.exists():
            os.startfile(folder)

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
            step = self.pipeline.describe(info.get("session_folder"))
            if step and message:
                message += " · " + step
        return {"recording": self.session is not None, "asking": self.asking, "paused": self.obs_state["paused"],
                "time": self.obs_state["time"], "level": round(db, 1), "splitting": splitting,
                "progress": round(progress, 3), "message": message, "message_kind": kind,
                "has_result": bool(info and info["screen"]), "hotkey_errors": list(self.keys.failed),
                "private": self.private_start is not None,
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
                    self.rec_time = st["outputDuration"] / 1000
                    if self.tracker:                  # which program is in front, for the cut agent
                        if self.offset is None:
                            self.tracker.close(self.rec_time)
                        else:
                            self.tracker.sample(self.rec_time)
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
            elif value == HOTKEY_PRIVATE:
                self.toggle_private()
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
                self._add_switch_span(self.alttab_start, wall - self.offset)
                self.alttab_start = None
            self.session.alttab_open = self.alttab_start is not None or self.popup_start is not None
        elif kind == "popup" and self.session is not None and self.offset is not None:
            # previews over the taskbar show small pictures of all windows: hidden like Alt+Tab,
            # but only when they open on the recorded screen
            edge, wall, point = value
            if edge == "down" and self._on_recorded_screen(point):
                self.popup_start = wall - self.offset
            elif edge == "up" and self.popup_start is not None:
                self._add_switch_span(self.popup_start, wall - self.offset)
                self.popup_start = None
            self.session.alttab_open = self.alttab_start is not None or self.popup_start is not None

    def _add_switch_span(self, start: float, end: float) -> None:
        """Freeze the screen track from start to end (recording time), shifted by the video delay,
        with margin for the switcher or pop-up fading in and out."""
        self.session.add_span(max(0.0, start + VIDEO_DELAY - 0.25), end + VIDEO_DELAY + 0.5)

    def _on_recorded_screen(self, point) -> bool:
        m = monitors.match_obs_name(self.choice.get("screen", ""))
        if not m or not point:
            return True                                  # unsure: rather hide it
        x, y = point
        return m["x"] <= x < m["x"] + m["width"] and m["y"] <= y < m["y"] + m["height"]
