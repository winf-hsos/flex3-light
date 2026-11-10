"""Runs the pipeline on filed recordings in the background, after the lecture.

A recording joins the queue as soon as its session folder is written. The queue is
worked off only while nothing is being recorded (the guiding principle "little load
during the lecture"), survives a restart in pipeline-queue.json, and keeps the state
of every step in pipeline.json inside the session folder, so a step that is done is
never repeated. The transcription goes to AssemblyAI and, should that fail, to OpenAI's
whisper-1 until its end; without a key for either it waits instead of failing. After the
subtitles the cut agent proposes the edit list (pipeline/cut_agent.py), unless the
lecturer switched that off; the list then waits for the lecturer's approval. Once it is
approved, the videos are rendered (pipeline/render.py), and again whenever an approved
list differs from the one rendered last. Each new render is followed by the one-page
summary in German and English (pipeline/summary.py), which needs the OpenAI key.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
from pathlib import Path
from typing import Callable

try:
    import services          # the app's keys in the Windows Credential Manager
except (ImportError, OSError, AttributeError):   # not on Windows: the server passes its own keys
    services = None
from pipeline import cut_agent, edit, files, llm, outline, render, screening, sounds, subtitles, summary, transcribe

STEPS = ("transcribe", "subtitles", "cut", "render", "summary")
TRANSCRIPT_STEPS = ("transcribe", "subtitles")   # what a new transcription redoes; the edit list stays
WAIT = 30                    # seconds between checks while waiting for a key or for the recording to end
WHISPER_END = dt.date(2027, 2, 26)   # OpenAI shuts whisper-1 down


def read_state(folder: Path) -> dict:
    return files.read_json(Path(folder) / "pipeline.json", {"steps": {}})


class PipelineRunner:
    def __init__(self, queue_file: Path, can_run: Callable[[], bool], terms_for: Callable[[dict], list[str]],
                 cut_settings: Callable[[Path], dict] = lambda folder: {"auto": False},
                 video_args: list[str] | None = None, sound_dir: Path | None = None,
                 keys: Callable[[str], str | None] | None = None, models: Callable[[], dict] | None = None):
        """cut_settings: the settings for the cut of the recording in a folder, with "auto" (propose
        by itself) and the rule policy of pipeline/cut_rules.py; in the app those of its one lecturer,
        on the server those of whoever recorded it. keys: the key of a service ("openai",
        "assemblyai", "elevenlabs") or None; the app's Credential Manager unless given. models: the
        server's choice of model per stage ({"cut": {"adapter", "model", "effort"}, ...}, see
        pipeline/llm.py), read anew for every step so a change needs no restart."""
        self.queue_file = Path(queue_file)
        self.can_run, self.terms_for, self.cut_settings = can_run, terms_for, cut_settings
        self.key = keys or (lambda service: services.key(service))
        self.video_args, self.sound_dir = video_args, sound_dir
        self.models = models or (lambda: {})
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.failed: set[str] = set()          # not retried before the next start of the app
        self.active: str | None = None         # the folder being worked on right now
        try:
            self.queue: list[str] = json.loads(self.queue_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            self.queue = []

    def start(self) -> "PipelineRunner":
        threading.Thread(target=self._loop, daemon=True, name="pipeline").start()
        return self

    def enqueue(self, folder: Path) -> None:
        with self.lock:
            if str(folder) not in self.queue:
                self.queue.append(str(folder))
                self._save()
        self.wake.set()

    def queued(self, folder: Path | str) -> bool:
        with self.lock:
            return str(folder) in self.queue

    def redo(self, folder: Path) -> bool:
        """Transcribe a recording again, e.g. after the module's terms changed, and write new subtitles.

        The old transcript stays in place until the new one replaces it. False while the
        recording is being worked on.
        """
        folder = Path(folder)
        with self.lock:
            if self.active == str(folder):
                return False
            data = read_state(folder)
            for step in TRANSCRIPT_STEPS:
                data["steps"].pop(step, None)
            self._write_state(folder, data)
            self.failed.discard(str(folder))
        self.enqueue(folder)
        return True

    def forget(self, folder: Path) -> bool:
        """Take a recording out of the queue before it is deleted; False while it is being worked on."""
        with self.lock:
            if self.active == str(folder):
                return False
            self.failed.discard(str(folder))
            self._drop(Path(folder))
        return True

    def redo_cut(self, folder: Path) -> bool:
        """Let the agent propose the cut again; the old edit list is kept as edit.previous.json."""
        folder = Path(folder)
        with self.lock:
            if self.active == str(folder):
                return False
            if (folder / "edit.json").exists():
                files.retry(lambda: os.replace(folder / "edit.json", folder / "edit.previous.json"))
            data = read_state(folder)
            data["steps"].pop("cut", None)
            data["cut_requested"] = True                  # also when the lecturer switched the agent off
            self._write_state(folder, data)
            self.failed.discard(str(folder))
        self.enqueue(folder)
        return True

    def redo_summary(self, folder: Path) -> bool:
        """Write the summary again, e.g. after the prompt changed; False while the recording is worked on."""
        folder = Path(folder)
        with self.lock:
            if self.active == str(folder):
                return False
            data = read_state(folder)
            data["steps"].pop("summary", None)
            data["summary_requested"] = True
            self._write_state(folder, data)
            self.failed.discard(str(folder))
        self.enqueue(folder)
        return True

    def redo_render(self, folder: Path) -> bool:
        """Render the approved edit list again, e.g. after its videos were deleted; no new proposal
        and no AI involved. False while the recording is being worked on."""
        folder = Path(folder)
        with self.lock:
            if self.active == str(folder):
                return False
            data = read_state(folder)
            data["steps"].pop("render", None)
            data["render_requested"] = True
            self._write_state(folder, data)
            self.failed.discard(str(folder))
        self.enqueue(folder)
        return True

    def describe(self, folder: Path | str | None) -> str:
        """One line for the window about where a recording stands in the pipeline."""
        if not folder:
            return ""
        steps = read_state(folder)["steps"]
        t, s, c = steps.get("transcribe", {}), steps.get("subtitles", {}), steps.get("cut", {})
        r = steps.get("render", {})
        if r.get("state") in ("running", "failed"):
            return r.get("message", "")
        if c.get("state") in ("running", "waiting", "failed"):
            return c.get("message", "")
        if c.get("state") == "done":
            e = edit.load(Path(folder))
            if e and e.get("approved"):
                done = files.read_json(Path(folder) / "render.json").get("signature") == render.signature(e)
                if done and render.missing(Path(folder)):
                    return "Dateien des geschnittenen Videos fehlen"
                return "Geschnittenes Video fertig" if done else "Schnitt freigegeben, das Video wird gleich gerendert"
            return "Schnitt vorgeschlagen, wartet auf deine Freigabe"
        if s.get("state") == "done":
            with self.lock:
                queued = str(folder) in self.queue
            return "Schnitt-Vorschlag folgt" if queued else "Transkription und Untertitel fertig"
        if t.get("state") in ("running", "waiting", "failed"):
            return t.get("message", "")
        with self.lock:
            return "Transkription folgt" if str(folder) in self.queue else ""

    # ---------------------------------------------------------- work

    def _save(self) -> None:
        self.queue_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.queue_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.queue, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.queue_file)

    def _set(self, folder: Path, step: str, state: str, message: str = "", **extra) -> None:
        data = read_state(folder)
        data["steps"][step] = {"state": state, "message": message,
                               "at": dt.datetime.now().astimezone().isoformat(timespec="seconds"), **extra}
        self._write_state(folder, data)

    @staticmethod
    def _write_state(folder: Path, data: dict) -> None:
        files.write_json(folder / "pipeline.json", data)

    def _loop(self) -> None:
        while True:
            worked = False
            try:
                with self.lock:
                    todo = [f for f in self.queue if f not in self.failed]
                    if todo and self.can_run():
                        self.active = todo[0]
                if self.active:
                    worked = self._process(Path(self.active))
            except Exception:
                logging.getLogger("flex3-light").exception("pipeline")
            finally:
                self.active = None
            if not worked:
                self.wake.wait(WAIT)
                self.wake.clear()

    def _process(self, folder: Path) -> bool:
        """Run the next open step of one recording; False if it has to wait."""
        if not (folder / "session.json").exists():          # moved or deleted meanwhile
            self._drop(folder)
            return True
        steps = read_state(folder)["steps"]
        try:
            if steps.get("transcribe", {}).get("state") != "done":
                transcribers = self._transcribers()
                if not transcribers:
                    self._set(folder, "transcribe", "waiting", "Transkription wartet auf einen Schlüssel für "
                              "AssemblyAI oder OpenAI (Einstellungen, KI-Dienste)")
                    return False
                meta = json.loads((folder / "session.json").read_text(encoding="utf-8"))
                for i, t in enumerate(transcribers):
                    self._set(folder, "transcribe", "running", "Transkription läuft …")
                    try:
                        transcribe.run(folder, t, self.terms_for(meta), progress=lambda k, n: self._set(
                            folder, "transcribe", "running", "Transkription läuft" + (
                                f", Stück {min(k + 1, n)} von {n}" if n > 1 else " …")))
                    except Exception:
                        if i == len(transcribers) - 1:
                            raise
                        logging.getLogger("flex3-light").warning("%s failed, falling back", t.name, exc_info=True)
                        continue
                    self._set(folder, "transcribe", "done", model=t.name)
                    return True
            if steps.get("subtitles", {}).get("state") != "done":
                subtitles.run(folder)
                self._set(folder, "subtitles", "done")
                return True
            settings = self.cut_settings(folder)
            wanted = settings.get("auto", True) or read_state(folder).get("cut_requested")
            if wanted and steps.get("cut", {}).get("state") != "done" and not (folder / "edit.json").exists():
                key = self.key("openai")
                if not key:
                    self._set(folder, "cut", "waiting", "Schnitt-Vorschlag wartet auf den OpenAI-Schlüssel "
                              "(Einstellungen, KI-Dienste)")
                    return False
                listener = None
                aai = self.key("assemblyai")
                base = aai and transcribe.assemblyai_base(aai)
                if base:
                    listener = transcribe.AssemblyAI(aai, base)
                if not (folder / "privacy.json").exists():   # stages A and B, once; a new proposal reuses them
                    self._set(folder, "cut", "running", "Sichtung der Aufnahme …")
                    screening.run(folder, listener, settings, progress=lambda text: self._set(
                        folder, "cut", "running", text))
                if not outline.current(folder):                # stage C, once; a failed try is repeated next time
                    self._set(folder, "cut", "running", "Gliederung der Aufnahme …")
                    outline.run(folder, self.key, settings, progress=lambda text: self._set(
                        folder, "cut", "running", text), config=llm.stage_config("outline", self.models()))
                self._set(folder, "cut", "running", "Der Schnitt-Agent sieht sich die Aufnahme an …")
                result = cut_agent.run(folder, self.key, listener, settings, progress=lambda text: self._set(
                    folder, "cut", "running", text), config=llm.stage_config("cut", self.models()))
                self._set(folder, "cut", "done", model=(result.get("agent") or {}).get("model", ""))
                return True
            e = edit.load(folder)
            if e and e.get("approved") and (read_state(folder).get("render_requested") or
                                            files.read_json(folder / "render.json").get("signature") != render.signature(e)):
                self._set(folder, "render", "running", "Das Video wird gerendert …")
                self._make_sounds(folder)
                report = render.run(folder, self.video_args, progress=lambda text: self._set(
                    folder, "render", "running", text), sound_dir=self.sound_dir)
                self._set(folder, "render", "done", signature=report["signature"])
                data = read_state(folder)
                if data.pop("render_requested", None):
                    self._write_state(folder, data)
                return True
            if (folder / "render.json").exists() and \
                    (not summary.current(folder) or read_state(folder).get("summary_requested")):
                key = self.key("openai")
                if not key:
                    self._set(folder, "summary", "waiting", "Zusammenfassung wartet auf den OpenAI-Schlüssel "
                              "(Einstellungen, KI-Dienste)")
                    return False
                self._set(folder, "summary", "running", "Die Zusammenfassung entsteht …")
                model = llm.stage_config("summary", self.models())     # the summary needs the Responses API
                result = summary.run(folder, llm.OpenAIResponses(key, model["model"], model.get("effort", "medium")),
                                     progress=lambda text: self._set(folder, "summary", "running", text))
                data = read_state(folder)
                data.pop("summary_requested", None)
                data["steps"]["summary"] = {"state": "done", "message": "", "model": result.get("model", ""),
                                            "at": result["created"]}
                self._write_state(folder, data)
                return True
        except Exception as e:
            now = read_state(folder)["steps"]                  # the step that was running, else the first not done
            step = next((k for k in STEPS if now.get(k, {}).get("state") == "running"), None) or \
                next((k for k in STEPS if now.get(k, {}).get("state") != "done"), STEPS[-1])
            name = {"transcribe": "Transkription", "subtitles": "Untertitel", "cut": "Schnitt-Vorschlag",
                    "render": "Rendern", "summary": "Zusammenfassung"}[step]
            self._set(folder, step, "failed", f"{name} fehlgeschlagen: {e}"[:300])
            self.failed.add(str(folder))
            return True
        self._drop(folder)                                     # all steps done
        return True

    def _make_sounds(self, folder: Path) -> None:
        """The sounds of the cards are made once with ElevenLabs; without its key the cards stay silent."""
        if not self.sound_dir or len(sounds.available(self.sound_dir)) == len(sounds.LIBRARY):
            return
        key = self.key("elevenlabs")
        if not key:
            return
        self._set(folder, "render", "running", "Die Klänge für Tafeln entstehen (einmalig) …")
        try:
            sounds.make(self.sound_dir, key)
        except Exception:
            logging.getLogger("flex3-light").warning("sounds", exc_info=True)

    def _transcribers(self) -> list:
        """AssemblyAI first, whisper-1 as the fallback while it still runs; those with a key."""
        found = []
        key = self.key("assemblyai")
        base = key and transcribe.assemblyai_base(key)
        if base:
            found.append(transcribe.AssemblyAI(key, base))
        key = self.key("openai")
        if key and dt.date.today() < WHISPER_END:
            found.append(transcribe.OpenAIWhisper(key))
        return found

    def _drop(self, folder: Path) -> None:
        with self.lock:
            if str(folder) in self.queue:
                self.queue.remove(str(folder))
                self._save()
