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

import activity
import hardware
import identify
import library
import services
import monitors                  # sets DPI awareness on import: physical pixels
import obs_remote as remote
import obs_scene as scene
from delivery import Delivery, recycle
from hotkeys import HOTKEY_PAUSE, HOTKEY_PRIVATE, GlobalKeys
from lecturer_profile import Profile, seed_entries
from pipeline import binaries, cut_rules, edit, review
from pipeline_runner import PipelineRunner
from preview import PreviewServer
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
        self.delivery = Delivery(self.raw_dir, server_client(self.settings), self.profile)
        # Transcription and the rest of the pipeline run on filed recordings, never during a recording.
        binaries.configure(hardware.ffmpeg())   # the bundled ffmpeg too; decoding on the GPU measured slower (2026-10-29)
        self.pipeline = PipelineRunner(PIPELINE_QUEUE, lambda: self.session is None, self._terms_for,
                                       lambda: self.cut_settings(), hardware.ffmpeg_video_args()).start()
        self.delivery.on_archived = self.pipeline.enqueue

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
                "options": dict(self.options), "cut": self.cut_settings()}

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
        self.tracker = activity.Tracker(s.add_activity)
        self.offset, self.alttab_start, self.private_start = None, None, None
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

    # ---------------------------------------------------------- recordings ("Meine Aufnahmen")

    def recordings(self) -> list[dict]:
        """All session folders in the archive, newest first, with their way through the pipeline."""
        self._library = library.scan(self.archive)
        items = [library.summary(f) for f in self._library.values()]
        return sorted(items, key=lambda r: r["recorded_at"], reverse=True)

    def recording(self, sid: str) -> dict | None:
        folder = getattr(self, "_library", {}).get(sid)
        if folder is None:
            return None
        return {**library.detail(folder), "status": self.pipeline.describe(folder)}

    # ---------------------------------------------------------- the cut: settings and approval

    def cut_settings(self) -> dict:
        """The lecturer's settings for the cut: propose by itself, pauses, programs to cut or keep."""
        return {"auto": True, **cut_rules.policy(self.settings.get("cut"))}

    def set_cut_settings(self, values: dict) -> None:
        current = dict(self.settings.get("cut") or {})
        for k in ("auto", "pause_mode", "pause_min", "cut_programs", "keep_programs", "cut_titles", "kinds_off"):
            if k in values:
                current[k] = values[k]
        if current.get("pause_mode") not in ("card", "shorten", "cut"):
            current["pause_mode"] = "card"
        current["pause_min"] = min(600.0, max(10.0, float(current.get("pause_min", 30))))
        for k in ("cut_programs", "keep_programs", "cut_titles"):
            current[k] = sorted({str(x).strip().lower() for x in current.get(k, []) if str(x).strip()})
        current["kinds_off"] = sorted({k for k in current.get("kinds_off", []) if k in edit.KINDS})
        self.settings["cut"] = current
        self.save()

    def _edit_of(self, sid: str):
        """(folder, edit list) of a recording the lecturer may change now, or an error message."""
        folder = getattr(self, "_library", {}).get(sid)
        if folder is None or not folder.exists():
            return None, "Die Aufnahme ist nicht mehr in der Ablage."
        if self.pipeline.active == str(folder):
            return None, "Der Schnitt-Agent oder die Transkription arbeitet gerade an dieser Aufnahme."
        e = edit.load(folder)
        if e is None:
            return None, "Für diese Aufnahme gibt es noch keinen Schnitt-Vorschlag."
        return (folder, e), ""

    def _save_edit(self, folder: Path, e: dict) -> None:
        e.pop("approved", None)                    # every change asks for a new approval
        edit.save(folder, e)

    def _words(self, folder: Path) -> list[dict]:
        return json.loads((folder / "transcript.json").read_text(encoding="utf-8")).get("words", []) \
            if (folder / "transcript.json").exists() else []

    def change_cut_item(self, sid: str, item_id: str, changes: dict) -> str:
        """Accept, reject, move the bounds or change the text of one item; returns an error or ''."""
        found, error = self._edit_of(sid)
        if error:
            return error
        folder, e = found
        x = edit.find(e, item_id)
        if x is None:
            return "Diesen Vorschlag gibt es nicht mehr."
        words = self._words(folder)
        for k in ("start", "end"):
            if changes.get(k) is not None and not (k == "end" and x["end"] is None):
                t = min(e["duration"], max(0.0, float(changes[k])))
                x[k] = edit.snap(t, words) if x["kind"] not in edit.SPANLESS else round(t, 3)
        if x["end"] is not None and x["end"] <= x["start"]:
            return "Das Ende muss nach dem Anfang liegen."
        if changes.get("state") in edit.STATES:
            x["state"] = changes["state"]
        for k, v in (changes.get("params") or {}).items():
            x["params"][k] = v
        x["by_lecturer"] = True
        e["items"].sort(key=lambda i: (i["start"], i["kind"]))
        self._save_edit(folder, e)
        return ""

    def add_cut_item(self, sid: str, kind: str, start: float, end: float | None, params: dict | None = None) -> str:
        found, error = self._edit_of(sid)
        if error:
            return error
        folder, e = found
        words = self._words(folder)
        try:
            if kind not in edit.SPANLESS:
                start, end = edit.snap(float(start), words), edit.snap(float(end), words)
            x = edit.item(kind, float(start), end, "Von dir gesetzt", "lecturer", 1.0, "accepted", **(params or {}))
        except (TypeError, ValueError) as ex:
            return str(ex)
        edit.add(e, x)
        self._save_edit(folder, e)
        return ""

    def remove_cut_item(self, sid: str, item_id: str) -> str:
        found, error = self._edit_of(sid)
        if error:
            return error
        folder, e = found
        x = edit.find(e, item_id)
        if x is None or x["source"] != "lecturer":
            return "Nur selbst gesetzte Einträge lassen sich entfernen; Vorschläge lehnst du ab."
        e["items"].remove(x)
        self._save_edit(folder, e)
        return ""

    def set_cut_metadata(self, sid: str, title: str, description: str) -> str:
        found, error = self._edit_of(sid)
        if error:
            return error
        folder, e = found
        e["title"], e["description"] = title.strip(), description.strip()
        self._save_edit(folder, e)
        return ""

    def set_program_rule(self, sid: str, program: str, choice: str) -> str:
        """choice "cut" or "keep" remembers the program for all recordings, "" forgets it; the program's
        spans in this recording are proposed again accordingly."""
        program = program.strip().lower()
        policy = self.cut_settings()
        cut = set(policy["cut_programs"]) - {program}
        keep = set(policy["keep_programs"]) - {program}
        (cut if choice == "cut" else keep if choice == "keep" else set()).add(program)
        self.set_cut_settings({"cut_programs": sorted(cut), "keep_programs": sorted(keep)})
        found, error = self._edit_of(sid)
        if error:
            return "" if "noch keinen" in error else error
        folder, e = found
        e["items"] = [x for x in e["items"] if not (x["params"].get("rule") == "program" and not x.get("by_lecturer"))]
        log = json.loads((folder / "activity.json").read_text(encoding="utf-8")) if (folder / "activity.json").exists() else {}
        for x in cut_rules.programs(log.get("activity", []), self.cut_settings()):
            edit.add(e, x)
        self._save_edit(folder, e)
        return ""

    def approve_cut(self, sid: str, approved: bool = True) -> str:
        found, error = self._edit_of(sid)
        if error:
            return error
        folder, e = found
        if approved:
            open_items = [x for x in e["items"] if x["state"] == "proposed"]
            if open_items:
                return f"Noch {len(open_items)} Vorschläge offen: annehmen oder ablehnen, dann freigeben."
            e["approved"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
            edit.save(folder, e)
            self.pipeline.failed.discard(str(folder))  # a failed render is tried again
            self.pipeline.enqueue(folder)            # render it
        else:
            self._save_edit(folder, e)
        return ""

    def redo_cut(self, sid: str) -> str:
        folder = getattr(self, "_library", {}).get(sid)
        if folder is None or not folder.exists():
            return "Die Aufnahme ist nicht mehr in der Ablage."
        if library.stations(folder)["transcript"] != "done":
            return "Der Schnitt braucht zuerst die Transkription."
        if not self.pipeline.redo_cut(folder):
            return "An dieser Aufnahme wird gerade gearbeitet."
        return ""

    def redo_transcription(self, sid: str) -> str:
        """Queue a recording's transcription again, with the module's current terms; returns an error or ''."""
        folder = getattr(self, "_library", {}).get(sid)
        if folder is None or not folder.exists():
            return "Die Aufnahme ist nicht mehr in der Ablage."
        if not self.pipeline.redo(folder):
            return "Die Aufnahme wird gerade transkribiert."
        return ""

    def review_word(self, sid: str, index: int, text: str, as_term: bool = False) -> dict:
        """Correct or confirm an unsure word of a transcript, optionally adding it to the module's terms.

        Returns {"ok", "message"} for the window.
        """
        folder = getattr(self, "_library", {}).get(sid)
        if folder is None or not folder.exists():
            return {"ok": False, "message": "Die Aufnahme ist nicht mehr in der Ablage."}
        if library.stations(folder)["transcript"] != "done":
            return {"ok": False, "message": "Die Transkription läuft gerade, danach geht es wieder."}
        try:
            old = json.loads((folder / "transcript.json").read_text(encoding="utf-8"))["words"][int(index)]["word"]
            word = (review.confirm(folder, int(index)) if text.strip() == old.strip()
                    else review.correct(folder, int(index), text))
        except (ValueError, IndexError, OSError) as e:
            return {"ok": False, "message": str(e) or "Das Wort ließ sich nicht ändern."}
        if not as_term:
            return {"ok": True, "message": ""}
        name = json.loads((folder / "session.json").read_text(encoding="utf-8")).get("course_name", "")
        module, new = self.profile.find(name), review.term(word["word"])
        if module is None:
            return {"ok": True, "message": f"Korrigiert, aber das Modul „{name}“ fehlt in deinen Modulen."}
        if new and new not in module.get("terms", []):
            self.profile.update(module["id"], terms=(module.get("terms", []) + [new])[:300])
            self.delivery.wake.set()
        return {"ok": True, "message": f"„{new}“ steht jetzt in den Fachbegriffen von {module['name']}."}

    def delete_recording(self, sid: str) -> str:
        """Move a recording's session folder with all its files to the recycle bin; returns an error or ''."""
        folder = getattr(self, "_library", {}).get(sid)
        if folder is None or not folder.exists():
            return "Die Aufnahme ist nicht mehr in der Ablage."
        if not self.pipeline.forget(folder):
            return "Die Aufnahme wird gerade transkribiert. Lösch sie, wenn das fertig ist."
        try:
            recycle(folder)
        except OSError as e:
            return f"Löschen fehlgeschlagen: {e}"
        self._library.pop(sid, None)
        return ""

    def open_recording(self, sid: str) -> None:
        folder = getattr(self, "_library", {}).get(sid)
        if folder and folder.exists():
            os.startfile(folder)

    def _media(self, sid: str, name: str) -> Path | None:
        """For the preview server: a video or subtitle file of a listed recording, nothing else."""
        folder = getattr(self, "_library", {}).get(sid)
        return folder / name if folder and library.MEDIA.match(name) else None

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
                self.session.alttab_open = True        # no segment is cut until the span is known
            elif self.alttab_start is not None:
                # shift by the video delay, plus margin for the switcher fading in and out
                a = self.alttab_start + VIDEO_DELAY - 0.25
                b = wall - self.offset + VIDEO_DELAY + 0.5
                self.session.add_span(max(0.0, a), b)
                self.alttab_start = None
                self.session.alttab_open = False
