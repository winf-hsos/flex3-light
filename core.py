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
import webbrowser
from pathlib import Path

import activity
import hardware
import identify
import library
import services
import monitors                  # sets DPI awareness on import: physical pixels
import obs_remote as remote
import obs_scene as scene
import delivery as delivery_mod
import device_watch
import local_server
from delivery import Delivery, recycle
from hotkeys import HOTKEY_PAUSE, HOTKEY_PRIVATE, GlobalKeys
from lecturer_profile import Profile, seed_entries
from pipeline import cut_rules
from preview import PreviewServer
import server_client as server_api
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

semester = library.semester


def clean_name(text: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "", text).strip().rstrip(".")


SERVER_SECRET = "server"     # name of the device token in the Windows Credential Manager
# Two connections since 2026-11-25: the server on the workstation ("main") and a test server on this
# computer ("test"), to try a change before it goes there. Which one a connection code fills follows
# from its address. Each keeps its address and device in settings["servers"] and its token under its
# own name in the Credential Manager; the one in use is mirrored in server_url and device_id.
SERVERS = {"main": SERVER_SECRET, "test": "server-test"}
SERVER_LABELS = {"main": "Server", "test": "Testserver (lokal)"}


def server_kind(url: str) -> str:
    return "test" if local_server.is_local(url) else "main"


def server_client(settings: dict) -> ServerClient | None:
    """The FLEX3 Light server from the settings, or None: then recordings are only archived locally.

    The address is a setting, the device token lives in the Credential Manager (since
    2026-11-06; a token still in settings.json is moved there on start)."""
    url = settings.get("server_url")
    token = services.credentials.get(SERVERS[server_kind(url)]) if url else None
    if url and token:
        return ServerClient(url, token, settings.get("upload_kbit"))
    return None


def server_api_split(code: str) -> tuple[str, str]:
    """(key, address) from a connection code "<key>@<address>" or a bare key."""
    key, _, url = (code or "").strip().partition("@")
    return key.strip(), url.strip().rstrip("/")


def _move_token_out_of_settings(settings: dict) -> None:
    if settings.get("server_token"):
        services.credentials.put(SERVER_SECRET, settings.pop("server_token"))
        save_settings(settings)


def _move_connection_into_servers(settings: dict) -> None:
    """One-off (2026-11-25): the one connection of before becomes one of the two. A connection to
    this computer is the test server, and its token moves to the test server's name."""
    url = settings.get("server_url")
    if not url or "servers" in settings:
        return
    kind = server_kind(url)
    settings["servers"] = {kind: {"url": url, "device_id": settings.get("device_id", "")}}
    if SERVERS[kind] != SERVER_SECRET:
        token = services.credentials.get(SERVER_SECRET)
        if token:
            services.credentials.put(SERVERS[kind], token)
            services.credentials.delete(SERVER_SECRET)
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


# ------------------------------------------------------------------ devices

# Without a saved choice: the likely screen, camera and microphone, by name.
DEVICE_GUESS = {"screen": lambda n: "Haupt" in n or "Primary" in n, "camera": lambda n: "C920" in n,
                "microphone": lambda n: "DJI" in n or "C920" in n}


def pick_device(names: list[str], wanted: str | None, current: str | None, guess) -> str:
    """The device to use: the lecturer's choice if it is there, else the one in use, else a likely
    one, else the first. A chosen camera that was unplugged comes back by itself when plugged in."""
    if wanted in names:
        return wanted
    if current in names:
        return current
    return next((n for n in names if guess(n)), names[0] if names else "")


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
        self.devices_stamp = 0                    # counts changes of the device lists, for the window
        self.device_print = device_watch.fingerprint()
        self.devices_due: list[float] = []        # when to ask OBS for the device lists again
        # The modules live in the lecturer's profile; on the very first start it takes over what exists.
        self.profile = Profile(PROFILE_FILE)
        if self.profile.is_new:
            self.profile.seed(seed_entries(self.archive, self.term, self.settings.get("module_file")))
        _move_preferences_to_profile(self.settings, self.profile)
        _move_token_out_of_settings(self.settings)
        _move_connection_into_servers(self.settings)
        self.delivery = Delivery(self.raw_dir, server_client(self.settings), self.profile)
        self.about = {"url": "", "name": "", "version": "", "at": 0.0}   # what the server in use says of itself
        self._asking_about = False
        # Since phase 5 (2026-11-09) the app only records: transcription, cut, rendering and the rest
        # run on the server, and a local server is started here if the connection points to one.
        local_server.ensure(self.settings.get("server_url", ""))
        threading.Thread(target=self._tidy, daemon=True, name="tidy").start()

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
            self.devices = self._read_devices(self.o)
        self.choice, self.wanted = {}, {}
        for kind, items in self.devices.items():
            self.choice[kind] = pick_device([n for n, _ in items], self.settings.get(kind), None, DEVICE_GUESS[kind])
            # what the lecturer chose stays wanted while it is unplugged, so it comes back with the device
            self.wanted[kind] = self.settings.get(kind) or self.choice[kind]
        if self.session is None:                   # never change the sources of a running recording
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
                "api_keys": services.status(),
                "module": self.module,
                "devices": {k: [n for n, _ in v] for k, v in self.devices.items()},
                "choice": dict(self.choice), "archive_dir": str(self.archive), "raw_dir": str(self.raw_dir),
                "options": dict(self.options), "cut": self.cut_settings(), "after_recording": self.after_recording(),
                "server": self.server_info()}

    @staticmethod
    def _read_devices(o: remote.Obs) -> dict[str, list[tuple[str, str]]]:
        devices = scene.devices(o)
        devices["camera"] = devices["camera"] + [(scene.NO_CAMERA, scene.NO_CAMERA)]
        return devices

    def watch_devices(self) -> None:
        """While idle: notice a camera, microphone or screen plugged in or out. Windows reports it at
        once and cheaply; OBS is asked only then (it takes 1 to 2 s), and once more a few seconds
        later, because an audio device can reach OBS a moment after its camera."""
        now = time.time()
        fp = device_watch.fingerprint()
        if fp != self.device_print:
            self.device_print = fp
            self.devices_due = [now + 1, now + 5]
        if self.devices_due and now >= self.devices_due[0]:
            self.devices_due.pop(0)
            try:
                self.refresh_devices()
            except Exception:                     # OBS busy or reconnecting: try once more shortly
                if not self.devices_due:
                    self.devices_due.append(now + 5)

    def refresh_devices(self) -> bool:
        """Read the device lists from OBS again; never during a recording. Returns whether they changed."""
        if self.session is not None:
            return False
        with remote.Obs() as o:                   # own connection: the slow query does not hold the lock
            new = self._read_devices(o)
        with self.lock:
            if self.session is not None or new == self.devices:
                return False
            before = {k: self._device_id(k) for k in self.devices}
            self.devices = new
            for kind, items in new.items():
                self.choice[kind] = pick_device([n for n, _ in items], self.wanted.get(kind),
                                                self.choice.get(kind), DEVICE_GUESS[kind])
            if any(self._device_id(k) != before.get(k) for k in new):
                self.apply_devices()
            self.devices_stamp += 1
        return True

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
            self.choice[kind] = self.wanted[kind] = name
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

    # ---------------------------------------------------------- server connection

    def server_info(self) -> dict:
        """What the settings show about the connections; never a token."""
        url = self.settings.get("server_url", "")
        stored = self.settings.get("servers", {})
        return {"url": url if self.delivery.client else "", "device_id": self.settings.get("device_id", ""),
                "active": server_kind(url) if url and self.delivery.client else "",
                "servers": [{"kind": k, "label": SERVER_LABELS[k], "url": stored[k]["url"]}
                            for k in SERVERS if k in stored],
                "user_name": self.profile.data.get("user_name", ""), "connection": self.delivery.connection(),
                **self.server_about()}

    def server_about(self) -> dict:
        """Name and version of the server in use, for the window: {"name", "version"}, empty until known.
        Asked in the background when the connection is new and every ten minutes while it is online, so
        an update on the workstation shows within that time; a server from before has no name."""
        client, a = self.delivery.client, self.about
        if client is None:
            return {"name": "", "version": ""}
        wait = 600 if a["version"] else 60
        if self.delivery.reachable and not self._asking_about and (a["url"] != client.url or time.time() - a["at"] > wait):
            self._asking_about = True

            def ask() -> None:
                try:
                    answer = client.about()
                    self.about = {"url": client.url, "name": str(answer.get("name") or "")[:60],
                                  "version": str(answer.get("version") or "")[:40], "at": time.time()}
                except (OSError, server_api.ServerError, ValueError, AttributeError):
                    if self.about["url"] != client.url:
                        self.about = {"url": client.url, "name": "", "version": "", "at": time.time()}
                    else:
                        self.about["at"] = time.time()
                finally:
                    self._asking_about = False
            threading.Thread(target=ask, daemon=True, name="server-about").start()
        return {"name": a["name"], "version": a["version"]} if a["url"] == client.url else {"name": "", "version": ""}

    def refresh_profile(self) -> None:
        """Take over changes made elsewhere at once, above all the modules kept in the browser since
        2026-11-25; the window asks for it whenever it comes to the front."""
        if self.delivery.client is not None and self.session is None:
            self.profile.stale = True
            self.delivery.wake.set()

    def _changing_server(self, url: str) -> str:
        """Why the connection cannot change to url now, or ''."""
        if self.session is not None:
            return "Während einer Aufnahme lässt sich die Verbindung nicht ändern."
        if url != self.settings.get("server_url") and self.delivery.sending():
            return "Eine Aufnahme ist erst zum Teil beim Server. Wechseln geht, sobald sie ganz angekommen ist."
        return ""

    def _use_server(self, kind: str) -> None:
        c = self.settings["servers"][kind]
        self.settings.update({"server_url": c["url"], "device_id": c["device_id"]})
        save_settings(self.settings)
        self.delivery.client = server_client(self.settings)
        self.delivery.reachable, self.delivery.server_down_until = None, 0.0
        self.profile.status = "local"
        self.delivery.wake.set()                 # first sync of the profile right away
        local_server.ensure(c["url"])

    def connect_server(self, code: str) -> str:
        """Register this computer with a connection code ("<key>@<address>") and use that server;
        returns an error or ''. An address on this computer is the test server, any other the server."""
        key, url = server_api_split(code)
        url = url or self.settings.get("server_url", "")
        if not key:
            return "Bitte den Verbindungscode einfügen."
        if not url:
            return "Im Code fehlt die Adresse des Servers (Schlüssel@Adresse)."
        if not url.startswith(("http://", "https://")):
            return "Die Adresse muss mit http:// oder https:// beginnen."
        if error := self._changing_server(url):
            return error
        try:
            answer = server_api.register(url, key, os.environ.get("COMPUTERNAME", "Rechner"))
        except server_api.ServerError as e:
            return str(e.detail) if e.status == 403 else f"Der Server lehnt ab: {e}"
        except OSError as e:
            return f"Der Server ist nicht erreichbar: {getattr(e, 'reason', e)}"
        kind = server_kind(url)
        services.credentials.put(SERVERS[kind], answer["token"])
        self.settings.setdefault("servers", {})[kind] = {"url": url, "device_id": answer["device_id"]}
        self._use_server(kind)
        return ""

    def use_server(self, kind: str) -> str:
        """Use the other of the two connections; both stay registered. Returns an error or ''."""
        c = self.settings.get("servers", {}).get(kind)
        if c is None or not services.credentials.get(SERVERS[kind]):
            return "Mit diesem Server ist der Rechner nicht verbunden."
        if error := self._changing_server(c["url"]):
            return error
        self._use_server(kind)
        return ""

    def await_server(self, timeout: float = 45) -> str:
        """After a change of connection, wait until the server in use answers and the profile came
        from it, so the window shows that server's modules and recordings and not the last one's.
        The test server on this computer may need several seconds to start. Returns '' or why the
        server is not there yet; the app then keeps trying in the background as always."""
        client = self.delivery.client
        if client is None:
            return ""
        end = time.monotonic() + timeout
        while True:
            try:
                client.health()
                break
            except (OSError, server_api.ServerError):
                if time.monotonic() >= end:
                    return "Der Server antwortet noch nicht. FLEX3 Light versucht es im Hintergrund weiter."
                time.sleep(0.5)
        self.delivery.server_down_until = 0.0
        self.delivery.wake.set()                  # the profile from this server right away
        while (self.profile.status == "local" and self.delivery.reachable is not False
               and self.delivery.client is client and time.monotonic() < end):
            time.sleep(0.2)
        return ""

    def disconnect_server(self) -> str:
        """Forget the connection in use and record locally only; the server keeps what it has, and the
        other connection stays to be chosen again."""
        if self.session is not None:
            return "Während einer Aufnahme lässt sich die Verbindung nicht ändern."
        url = self.settings.get("server_url", "")
        if url:
            kind = server_kind(url)
            services.credentials.delete(SERVERS[kind])
            self.settings.get("servers", {}).pop(kind, None)
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
        self.settings.update({"module": self.module, **{k: v for k, v in self.wanted.items() if v},
                              "archive_dir": str(self.archive),
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

    # ---------------------------------------------------------- the local copy (phase 5)

    def _tidy(self) -> None:
        """Once an hour: note which filed recordings the server has confirmed, and move the local
        copy of those confirmed more than KEEP_DAYS days ago to the recycle bin (decision of
        2026-11-09). A recording the server never confirmed stays."""
        while True:
            try:
                client = self.delivery.client
                if client is not None:
                    for folder in delivery_mod.unconfirmed(self.archive):
                        sid = json.loads((folder / "session.json").read_text(encoding="utf-8")).get("session_id", "")
                        try:
                            if sid and client.status(sid).get("state") == "complete":
                                delivery_mod.mark_confirmed(folder, client.url)
                        except (server_api.ServerError, OSError):
                            break                          # offline: next hour
                for folder in delivery_mod.expired(self.archive):
                    recycle(folder)
            except Exception:                              # never stop the app over tidying up
                pass
            time.sleep(3600)

    # ---------------------------------------------------------- after a recording

    AFTER_RECORDING = cut_rules.AFTER_RECORDING

    def after_recording(self) -> str:
        """What starts by itself once a recording is filed: nothing (the default, decided 2026-11-04),
        the transcription, or the transcription and then the cut proposal."""
        return cut_rules.after_recording(self.profile.preferences())

    def set_after_recording(self, value: str) -> None:
        if value in self.AFTER_RECORDING and self.profile.set_preferences({"after_recording": value}):
            self.delivery.wake.set()              # the profile goes to the server at once

    # ---------------------------------------------------------- the cut: settings and approval

    def cut_settings(self) -> dict:
        """The lecturer's settings for the cut, from the profile: propose by itself, pauses, programs to
        cut or keep, cards and sounds; the server uses them for the lecturer's recordings."""
        return cut_rules.settings_from(self.profile.preferences())

    def set_cut_settings(self, values: dict) -> None:
        # one preference per field, so a change here does not undo another device's change of a different one
        if self.profile.set_preferences(cut_rules.cut_preferences(self.profile.preferences("cut."), values)):
            self.delivery.wake.set()

    # ---------------------------------------------------------- "Meine Aufnahmen" on the server (phase 4)
    # Since 2026-11-25 the app only records (Nicolas): everything after the recording, the recordings
    # and the settings of cut and videos, is the server's page in the browser.

    def open_on_server(self, page: str = "library") -> str:
        """Open the server's page in the default browser, signed in as this lecturer with a one-time
        ticket: "library" (Meine Aufnahmen), "modules" or "settings" (cut, videos, their look). Returns an
        error or ''."""
        client = self.delivery.client
        if client is None:
            return "Kein Server verbunden."
        try:
            url = client.url + client.library_ticket()["path"]
        except (server_api.ServerError, OSError, KeyError):
            return "Der Server ist gerade nicht erreichbar. Versuch es gleich noch einmal."
        if page in ("settings", "modules"):
            url += "&page=" + page
        webbrowser.open(url)
        return ""

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
                "private": self.private_start is not None,
                "connection": self.delivery.connection(), "profile_status": self.profile.status,
                "profile_pending": self.profile.dirty() and self.delivery.connection() != "local",
                "profile_stamp": f"{self.profile.data['revision']}-{self.profile.data['synced_at']}",
                "devices_stamp": self.devices_stamp, "server": self.server_about(),
                "server_kind": server_kind(self.settings.get("server_url", "")) if self.delivery.client else ""}

    def _loop(self) -> None:
        next_device_check = 0.0
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
                if self.session is None and time.time() >= next_device_check:
                    next_device_check = time.time() + 2
                    self.watch_devices()
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
