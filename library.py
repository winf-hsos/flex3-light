"""The recordings in the archive, for the page "Meine Aufnahmen".

A recording is a session folder <archive>\\<term>\\<module>\\<date> - <topic>\\ with a
session.json; older recordings filed as two loose videos are not listed. For each
recording the page shows its way through the pipeline as five stations, in order:
recorded, transcript, cut, content, publish. The last two are not built yet; the cut
station shows the edit list of the cut agent until the lecturer approved it.
"""
from __future__ import annotations

import re
from pathlib import Path

from pipeline import cut_rules, edit, files, render, review

STATIONS = ("recorded", "transcript", "cut", "content", "publish")
MEDIA = re.compile(r"^(recording|screen|camera|edited|clip-\d{2})\.mp4$|^(subtitles|edited|clip-\d{2})\.[a-z]{2}\.vtt$"
                   r"|^thumbnail\.jpg$")


def _read(path: Path) -> dict:
    return files.read_json(path)


def stations(folder: Path, queued: bool | None = None) -> dict[str, str]:
    """State per station: done, running, waiting, failed, queued, review (waits for the lecturer),
    open (can be started) or soon (not built yet). queued says whether the recording waits in the
    pipeline's queue; False makes an untouched transcript "open", None leaves it "queued"."""
    steps = _read(folder / "pipeline.json").get("steps", {})
    t, s = steps.get("transcribe", {}).get("state"), steps.get("subtitles", {}).get("state")
    if s == "done":
        transcript = "done"
    elif t in ("running", "waiting", "failed"):
        transcript = t
    elif t == "done" or s == "running":
        transcript = "running"
    else:
        transcript = "open" if queued is False else "queued"
    edit_list = _read(folder / "edit.json")
    c = steps.get("cut", {}).get("state")
    r = steps.get("render", {}).get("state")
    if c == "running" or r == "running":  # the agent saves while it works; rendering follows the approval
        cut = "running"
    elif r == "failed" and edit_list.get("approved"):
        cut = "failed"
    elif edit_list and edit_list.get("approved"):   # approved: done once the video of this very list is there
        made = _read(folder / "render.json").get("signature")
        cut = "done" if made == render.signature(edit_list) else "queued"
    elif edit_list:
        cut = "review"
    elif c in ("waiting", "failed"):
        cut = c
    else:
        cut = "open" if transcript == "done" else "soon"
    return {"recorded": "done", "transcript": transcript, "cut": cut, "content": "soon", "publish": "soon"}


def scan(archive: Path) -> dict[str, Path]:
    """session_id -> folder for every session folder in the archive."""
    found = {}
    for meta_file in Path(archive).glob("*/*/*/session.json"):
        sid = _read(meta_file).get("session_id")
        if sid:
            found[sid] = meta_file.parent
    return found


def summary(folder: Path, queued: bool | None = None) -> dict:
    meta = _read(folder / "session.json")
    topic = folder.name.split(" - ", 1)[1] if " - " in folder.name else folder.name
    return {"id": meta.get("session_id", ""), "title": topic, "module": meta.get("course_name", folder.parent.name),
            "term": folder.parent.parent.name, "recorded_at": meta.get("recorded_at", ""),
            "duration": meta.get("duration_sec", 0), "language": meta.get("language", "de"),
            "camera": any(t.get("kind") == "camera" for t in meta.get("tracks", [])),
            "stations": stations(folder, queued)}


def detail(folder: Path, queued: bool | None = None) -> dict:
    """What the detail view needs beyond the summary: the transcript by sentence, and the media."""
    transcript = _read(folder / "transcript.json")
    segments = [{"start": s["start"], "end": s["end"], "text": s["text"]} for s in transcript.get("segments", [])]
    media = sorted(p.name for p in folder.iterdir() if MEDIA.match(p.name)) if folder.exists() else []
    return {**summary(folder, queued), "segments": segments, "media": media,
            "model": transcript.get("model", ""), "terms": transcript.get("terms", []),
            "uncertain": review.uncertain(transcript) if transcript else [],
            "rated": any("confidence" in w for w in transcript.get("words", [])),
            "cut": cut_view(folder),
            "message": _read(folder / "pipeline.json").get("steps", {}).get("transcribe", {}).get("message", "")}


def cut_view(folder: Path) -> dict:
    """What the approval of the cut needs: the edit list, its problems, what remains, and the programs."""
    log = _read(folder / "activity.json")
    view = {"programs": cut_rules.program_summary(log.get("activity", [])), "edit": None}
    e = _read(folder / "edit.json")
    if e:
        kept = edit.kept(e, ("accepted",))
        view["edit"] = {k: e.get(k) for k in ("title", "description", "approved", "agent", "duration")}
        view["edit"]["items"] = e.get("items", [])
        view["edit"]["problems"] = edit.problems(e)
        view["edit"]["kept_seconds"] = round(sum(b - a for a, b in kept), 1)
        view["edit"]["summary"] = edit.summary(e)
        view["edit"]["cards"] = e.get("cards") or edit.default_cards(e, _read(folder / "session.json"))
        made = _read(folder / "render.json")
        if made:
            view["render"] = {**made, "current": made.get("signature") == render.signature(e),
                              "clips": _read(folder / "clips.json").get("clips", []),
                              "chapters": _read(folder / "chapters.json").get("chapters", [])}
    return view
