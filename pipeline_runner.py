"""Runs the pipeline on filed recordings in the background, after the lecture.

A recording joins the queue as soon as its session folder is written. The queue is
worked off only while nothing is being recorded (the guiding principle "little load
during the lecture"), survives a restart in pipeline-queue.json, and keeps the state
of every step in pipeline.json inside the session folder, so a step that is done is
never repeated. The transcription goes to AssemblyAI and, should that fail, to OpenAI's
whisper-1 until its end; without a key for either it waits instead of failing.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
from pathlib import Path
from typing import Callable

import services
from pipeline import files, subtitles, transcribe

STEPS = ("transcribe", "subtitles")
WAIT = 30                    # seconds between checks while waiting for a key or for the recording to end
WHISPER_END = dt.date(2027, 2, 26)   # OpenAI shuts whisper-1 down


def read_state(folder: Path) -> dict:
    return files.read_json(Path(folder) / "pipeline.json", {"steps": {}})


class PipelineRunner:
    def __init__(self, queue_file: Path, can_run: Callable[[], bool], terms_for: Callable[[dict], list[str]]):
        self.queue_file = Path(queue_file)
        self.can_run, self.terms_for = can_run, terms_for
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
            for step in STEPS:
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

    def describe(self, folder: Path | str | None) -> str:
        """One line for the window about where a recording stands in the pipeline."""
        if not folder:
            return ""
        steps = read_state(folder)["steps"]
        t, s = steps.get("transcribe", {}), steps.get("subtitles", {})
        if s.get("state") == "done":
            return "Transkription und Untertitel fertig"
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
        except Exception as e:
            step = "transcribe" if steps.get("transcribe", {}).get("state") != "done" else "subtitles"
            name = "Transkription" if step == "transcribe" else "Untertitel"
            self._set(folder, step, "failed", f"{name} fehlgeschlagen: {e}"[:300])
            self.failed.add(str(folder))
            return True
        self._drop(folder)                                     # all steps done
        return True

    def _transcribers(self) -> list:
        """AssemblyAI first, whisper-1 as the fallback while it still runs; those with a key."""
        found = []
        key = services.key("assemblyai")
        base = key and services.assemblyai_base(key)
        if base:
            found.append(transcribe.AssemblyAI(key, base))
        key = services.key("openai")
        if key and dt.date.today() < WHISPER_END:
            found.append(transcribe.OpenAIWhisper(key))
        return found

    def _drop(self, folder: Path) -> None:
        with self.lock:
            if str(folder) in self.queue:
                self.queue.remove(str(folder))
                self._save()
