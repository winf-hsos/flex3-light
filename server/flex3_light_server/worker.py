"""The pipeline on the server: the same steps as in the app, on the server's session folders.

One pipeline, two places to run it (docs/pipeline.md): the worker drives the app's
PipelineRunner and the steps in pipeline/ at the repository root, one recording after the
other. What a recording gets comes from the profile of whoever recorded it: the module's
technical terms for the transcription, the cut settings, and whether anything starts by
itself. A finished upload joins the queue when the lecturer's "after_recording" is not
"nothing" (since phase 5 the server is the only place the pipeline runs, so the earlier
preference "pipeline" no longer counts); everything else is started on request (act()). Keys are the project's, given to the
server (FLEX3_LIGHT_SERVER_SECRETS), never the lecturer's.

Since the decision of 2026-11-06 the activity log arrives without window titles: the app
puts the word of the lecturer's title list a title contained in its place, so the rules
and the agent work on it unchanged.
"""
from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]      # the repository, with pipeline/ and pipeline_runner.py
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pipeline_runner  # noqa: E402
from pipeline import binaries, cut_rules, edit, files  # noqa: E402

from .profiles import Store  # noqa: E402

logger = logging.getLogger(__name__)
ACTIONS = ("transcribe", "retranscribe", "cut", "approve", "render", "summary")


class Worker:
    def __init__(self, data_dir: Path, profiles: Store, keys: Callable[[str], str | None] | None = None):
        self.sessions = Path(data_dir) / "sessions"
        self.profiles = profiles
        ffmpeg, typst = shutil.which("ffmpeg"), shutil.which("typst")
        binaries.configure(ffmpeg=ffmpeg, typst=typst)
        self.runner = pipeline_runner.PipelineRunner(
            Path(data_dir) / "pipeline-queue.json", lambda: True, self._terms_for, self._cut_settings,
            sound_dir=Path(data_dir) / "sounds", keys=keys or (lambda service: None))

    def start(self) -> "Worker":
        self.runner.video_args = binaries.video_args()      # NVENC with an NVIDIA card, x264 otherwise
        self.runner.start()
        return self

    # ---------------------------------------------------------- the lecturer behind a folder

    @staticmethod
    def owner(folder: Path) -> str:
        return files.read_json(Path(folder) / "session.json").get("instructor_id", "")

    def _preferences(self, folder: Path) -> dict:
        user = self.owner(folder)
        return self.profiles.values(user) if user else {}

    def _terms_for(self, meta: dict) -> list[str]:
        user = meta.get("instructor_id", "")
        module = self.profiles.module(user, meta.get("course_name", "")) if user else None
        return (module or {}).get("terms", [])

    def _cut_settings(self, folder: Path) -> dict:
        return cut_rules.settings_from(self._preferences(folder))

    # ---------------------------------------------------------- events and requests

    def arrived(self, folder: Path) -> None:
        """A session folder is complete: start what the lecturer chose to start by itself."""
        prefs = self._preferences(folder)
        if cut_rules.after_recording(prefs) != "nothing":
            self.runner.enqueue(folder)
            logger.info("Pipeline queued for %s", folder.name)

    def state(self, folder: Path) -> dict:
        """Where a recording stands: the steps of pipeline.json and one line about it."""
        data = pipeline_runner.read_state(folder)
        return {"steps": data["steps"], "status": self.runner.describe(folder),
                "queued": self.runner.queued(folder), "active": self.runner.active == str(folder),
                "failed": str(folder) in self.runner.failed}

    def act(self, folder: Path, action: str) -> str:
        """Start a step by request; returns what stands in the way or ''."""
        r = self.runner
        steps = pipeline_runner.read_state(folder)["steps"]
        transcribed = steps.get("subtitles", {}).get("state") == "done"
        if action == "transcribe":
            r.failed.discard(str(folder))
            r.enqueue(folder)
            return ""
        if action == "retranscribe":
            return "" if r.redo(folder) else "An dieser Aufnahme wird gerade gearbeitet."
        if action == "cut":
            if not transcribed:
                return "Der Schnitt braucht zuerst die Transkription."
            return "" if r.redo_cut(folder) else "An dieser Aufnahme wird gerade gearbeitet."
        if action == "approve":
            if r.active == str(folder):
                return "An dieser Aufnahme wird gerade gearbeitet."
            error = edit.approve(folder, self._cut_settings(folder))
            if not error:
                r.failed.discard(str(folder))
                r.enqueue(folder)                 # render it
            return error
        e = edit.load(folder)
        if action == "render":
            if not (e and e.get("approved")):
                return "Der Schnitt ist noch nicht freigegeben."
            return "" if r.redo_render(folder) else "An dieser Aufnahme wird gerade gearbeitet."
        if action == "summary":
            if not (folder / "render.json").exists():
                return "Die Zusammenfassung braucht zuerst das geschnittene Video."
            return "" if r.redo_summary(folder) else "An dieser Aufnahme wird gerade gearbeitet."
        raise ValueError(action)
