"""The edit list of a recording, edit.json: proposals for the cut, never the cut itself.

Rules, the cut agent and the lecturer all write here. Every item has a kind, a span in
seconds of the recording, a reason in the lecture's language, its source, a confidence
and a state; only accepted items are rendered, and nothing is rendered before the
lecturer approved the list (docs/pipeline.md, step 3).

The cut agent decides every item, so that the lecturer gets a finished proposal and only
changes what he wants to (decision of 2026-10-29). It keeps a rule's reason and puts its
own view beside it ("assessment"), a proposal of its own may name the items it replaces
("replaces"), and every removal carries a category, so that summary() can tell in a few
lines what is cut and why.
"""
from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path

from . import files

KINDS = {
    "cut": "Schnitt",                     # remove the span
    "pause": "Pause",                     # a long pause: card "Pause entfernt", shortened or cut (params.mode)
    "mute": "Ton aus",                    # keep the picture, silence the sound (voices from the room)
    "freeze": "Bild anhalten",            # keep the sound, hold the last picture before the span
    "blur": "Verpixeln",                  # params.region: [x, y, w, h] as fractions of the frame
    "zoom": "Zoom",                       # params.region on the screen track, to make something readable
    "layout": "Layout",                   # params.camera: "big", "small" or "hidden"
    "chapter": "Kapitel",                 # start only; params.title, params.summary
    "clip": "Clip",                       # a span that stands on its own as a short video; params.title
    "lower_third": "Bauchbinde",          # params.text
    "thumbnail": "Vorschaubild",          # start only; params.track
}
SPANLESS = {"chapter", "thumbnail"}       # these only have a start
SOURCES = ("rule", "agent", "lecturer")
VERDICTS = ("agree", "disagree")
CATEGORIES = {                            # why something is taken out, for the summary
    "dead_time": "Leerlauf",              # silence at the edges, setting up, waiting
    "pause": "Lange Pausen",              # exercises, breaks
    "technical": "Technik",               # microphone checks, trouble with devices or software
    "off_record": "Nicht für die Aufzeichnung",
    "organisation": "Organisatorisches",  # things that only concerned the people in the room
    "privacy": "Datenschutz",             # mail, chats, names, voices from the room
    "other": "Sonstiges",
}
STATES = ("proposed", "accepted", "rejected")
SHORTEST_KEPT = 1.0                       # seconds; a remnant between two removals shorter than this is a problem
SHORTEST_CHAPTER = 10.0                   # YouTube's rule


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def new(duration: float) -> dict:
    return {"version": 1, "created": _now(), "duration": round(duration, 3), "title": "", "description": "",
            "items": [], "notes": ""}


def load(folder: Path) -> dict | None:
    data = files.read_json(Path(folder) / "edit.json", {})
    return data or None


def save(folder: Path, edit: dict) -> None:
    edit["changed"] = _now()
    files.write_json(Path(folder) / "edit.json", edit)


def item(kind: str, start: float, end: float | None, reason: str, source: str, confidence: float = 0.5,
         state: str = "proposed", category: str | None = None, **params) -> dict:
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}, one of {', '.join(KINDS)}")
    if category is not None and category not in CATEGORIES:
        raise ValueError(f"unknown category {category!r}, one of {', '.join(CATEGORIES)}")
    if source not in SOURCES or state not in STATES:
        raise ValueError("unknown source or state")
    if kind in SPANLESS:
        end = None
    elif end is None or end <= start:
        raise ValueError(f"{kind} needs an end after its start")
    return {"id": uuid.uuid4().hex[:8], "kind": kind, "start": round(float(start), 3),
            "end": None if end is None else round(float(end), 3), "reason": reason.strip(), "source": source,
            "confidence": round(min(1.0, max(0.0, float(confidence))), 2), "state": state,
            **({"category": category} if category else {}),
            "params": {k: v for k, v in params.items() if v is not None}}


def add(edit: dict, entry: dict) -> dict:
    edit["items"].append(entry)
    edit["items"].sort(key=lambda x: (x["start"], x["kind"]))
    return entry


def find(edit: dict, item_id: str) -> dict | None:
    return next((x for x in edit["items"] if x["id"] == item_id), None)


REMOVING = {"cut", "pause"}               # kinds that take time out of the result (a pause may keep a card)


def removed(edit: dict, states=("accepted",)) -> list[tuple[float, float]]:
    """Merged spans taken out of the recording by items in the given states."""
    spans = sorted((x["start"], x["end"]) for x in edit["items"] if x["kind"] in REMOVING and x["state"] in states)
    merged: list[list[float]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def kept(edit: dict, states=("accepted",)) -> list[tuple[float, float]]:
    """The spans of the recording that remain, in order."""
    out, t = [], 0.0
    for a, b in removed(edit, states):
        if a > t:
            out.append((t, a))
        t = max(t, b)
    if t < edit["duration"]:
        out.append((t, edit["duration"]))
    return out


def problems(edit: dict, states=("proposed", "accepted")) -> list[str]:
    """What would go wrong if these items were rendered as they are."""
    found = []
    duration = edit["duration"]
    for x in edit["items"]:
        if x["state"] not in states:
            continue
        end = x["end"] if x["end"] is not None else x["start"]
        if x["start"] < 0 or end > duration + 0.01:
            found.append(f"{x['id']} ({x['kind']}) liegt außerhalb der Aufnahme.")
    for a, b in kept(edit, states):
        if 0 < b - a < SHORTEST_KEPT:
            found.append(f"Zwischen {a:.1f} s und {b:.1f} s bleibt nur ein Rest von {b - a:.1f} s stehen.")
    chapters = sorted(x["start"] for x in edit["items"] if x["kind"] == "chapter" and x["state"] in states)
    if chapters:
        if chapters[0] > 0.5:
            found.append("Das erste Kapitel beginnt nicht bei 0:00 (YouTube verlangt das).")
        if len(chapters) < 3:
            found.append("YouTube zeigt Kapitel erst ab drei Kapiteln.")
        for a, b in zip(chapters, chapters[1:] + [duration]):
            if b - a < SHORTEST_CHAPTER:
                found.append(f"Das Kapitel bei {a:.1f} s ist kürzer als {SHORTEST_CHAPTER:.0f} s.")
    return found


MARGIN = 0.12                             # seconds of pause kept next to a word at a boundary


def snap(t: float, words: list[dict]) -> float:
    """Move a boundary out of a word to the nearer of its edges, a little into the pause beside it,
    so that no word is clipped. A boundary in a pause stays where it is."""
    for i, w in enumerate(words):
        if w["start"] < t < w["end"]:
            before = w["start"] - (words[i - 1]["end"] if i else 0.0)
            after = (words[i + 1]["start"] - w["end"]) if i + 1 < len(words) else MARGIN * 2
            left = w["start"] - min(MARGIN, max(0.0, before) / 2)
            right = w["end"] + min(MARGIN, max(0.0, after) / 2)
            return round(left if t - w["start"] <= w["end"] - t else right, 3)
    return round(t, 3)


MARKING = {"mute", "freeze", "blur"}      # kinds that hide something without taking time out


def summary(edit: dict) -> dict:
    """What the accepted list does, in the same shape every time: the length before and after,
    what is cut by category (count, time, the agent's sentence why), what is hidden, the clips,
    the chapters, and where the agent asks for a look."""
    accepted = [x for x in edit["items"] if x["state"] == "accepted"]
    told = edit.get("summary") or {}
    texts = {c.get("category"): c.get("text", "") for c in told.get("cuts", [])}
    classes: dict[str, dict] = {}
    for x in accepted:
        if x["kind"] in REMOVING or x["kind"] in MARKING:
            c = x.get("category") or ("pause" if x["kind"] == "pause" else "other")
            k = classes.setdefault(c, {"category": c, "label": CATEGORIES.get(c, c), "count": 0, "seconds": 0.0,
                                       "hidden": 0, "text": texts.get(c, "")})
            if x["kind"] in REMOVING:
                k["count"] += 1
                k["seconds"] += x["end"] - x["start"]
            else:
                k["hidden"] += 1
    for k in classes.values():
        k["seconds"] = round(k["seconds"], 1)
    by_id = {x["id"]: x for x in edit["items"]}
    return {
        "duration": edit["duration"],
        "kept": round(sum(b - a for a, b in kept(edit)), 1),
        "cuts": sorted(classes.values(), key=lambda k: (-k["seconds"], -k["hidden"])),
        "clips": [x["params"].get("title", "") for x in accepted if x["kind"] == "clip"],
        "chapters": [x["params"].get("title", "") for x in accepted if x["kind"] == "chapter"],
        "zooms": sum(1 for x in accepted if x["kind"] == "zoom"),
        "check": [{"id": c["id"], "why": c.get("why", "")} for c in told.get("check", []) if c.get("id") in by_id],
        "notes": [n for n in told.get("notes", []) if n][:3],
    }
