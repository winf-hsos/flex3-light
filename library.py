"""The recordings in the archive, for the page "Meine Aufnahmen".

A recording is a session folder <archive>\\<term>\\<module>\\<date> - <topic>\\ with a
session.json; older recordings filed as two loose videos are not listed. For each
recording the page shows its way through the pipeline as five stations, in order:
recorded, transcript, cut, content, publish. The last three are not built yet.
"""
from __future__ import annotations

import re
from pathlib import Path

from pipeline import files, review

STATIONS = ("recorded", "transcript", "cut", "content", "publish")
MEDIA = re.compile(r"^(recording|screen|camera)\.mp4$|^subtitles\.[a-z]{2}\.vtt$")


def _read(path: Path) -> dict:
    return files.read_json(path)


def stations(folder: Path) -> dict[str, str]:
    """State per station: done, running, waiting, failed, queued or soon."""
    steps = _read(folder / "pipeline.json").get("steps", {})
    t, s = steps.get("transcribe", {}).get("state"), steps.get("subtitles", {}).get("state")
    if s == "done":
        transcript = "done"
    elif t in ("running", "waiting", "failed"):
        transcript = t
    elif t == "done" or s == "running":
        transcript = "running"
    else:
        transcript = "queued"
    return {"recorded": "done", "transcript": transcript, "cut": "soon", "content": "soon", "publish": "soon"}


def scan(archive: Path) -> dict[str, Path]:
    """session_id -> folder for every session folder in the archive."""
    found = {}
    for meta_file in Path(archive).glob("*/*/*/session.json"):
        sid = _read(meta_file).get("session_id")
        if sid:
            found[sid] = meta_file.parent
    return found


def summary(folder: Path) -> dict:
    meta = _read(folder / "session.json")
    topic = folder.name.split(" - ", 1)[1] if " - " in folder.name else folder.name
    return {"id": meta.get("session_id", ""), "title": topic, "module": meta.get("course_name", folder.parent.name),
            "term": folder.parent.parent.name, "recorded_at": meta.get("recorded_at", ""),
            "duration": meta.get("duration_sec", 0), "language": meta.get("language", "de"),
            "camera": any(t.get("kind") == "camera" for t in meta.get("tracks", [])),
            "stations": stations(folder)}


def detail(folder: Path) -> dict:
    """What the detail view needs beyond the summary: the transcript by sentence, and the media."""
    transcript = _read(folder / "transcript.json")
    segments = [{"start": s["start"], "end": s["end"], "text": s["text"]} for s in transcript.get("segments", [])]
    media = sorted(p.name for p in folder.iterdir() if MEDIA.match(p.name)) if folder.exists() else []
    return {**summary(folder), "segments": segments, "media": media,
            "model": transcript.get("model", ""), "terms": transcript.get("terms", []),
            "uncertain": review.uncertain(transcript) if transcript else [],
            "rated": any("confidence" in w for w in transcript.get("words", [])),
            "message": _read(folder / "pipeline.json").get("steps", {}).get("transcribe", {}).get("message", "")}
