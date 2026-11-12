"""Topic videos: stage G of the cut agent's plan (phase 7, built 2026-11-12).

From the approved long version and the outline, every slide input (outline.json, "inputs", at
least 90 seconds) is proposed as a video of its own. The lecturer approves them in a second,
short step; the profile switch "cut.topic_videos" says whether that happens at all ("off"),
waits for the lecturer ("propose", the default) or is done by itself ("auto").

  products.json   the decisions: per video a span in seconds of the recording, title,
                  description and state (proposed, accepted, rejected); "approved" once the
                  lecturer has approved them. Written by the proposal and the window.
  topics.json     what was rendered: per video its files, length, chapters and the signature
                  it was rendered for. Written by render() only.
  topic-01.mp4    the videos, with topic-01.<language>.vtt, topic-01.jpg (preview picture) and
                  topic-01.txt (title, description and chapters, for the upload)

A topic video is cut from the rendered long version, so it carries everything done there: cuts,
mutes, blurs, zooms, pause and chapter cards inside its span. Only its bounds and texts are new,
and it gets a title and closing card of its own in the server's style (style.py). A new render of
the long version (a changed cut, a new style) renders the topic videos again.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from . import binaries, edit, files, outline, publish, timeline
from . import render as rendering      # render() below is the topic videos'
from . import style as styles

FILE = "products.json"
MADE = "topics.json"
MODES = ("off", "propose", "auto")
DEFAULT_MODE = "propose"
SHORTEST = 60.0          # seconds a topic video keeps after the cut at least
STATES = ("proposed", "accepted", "rejected")
TITLE_MAX = 120
DESCRIPTION_MAX = 2000


def mode(settings: dict | None) -> str:
    value = (settings or {}).get("topic_videos")
    return value if value in MODES else DEFAULT_MODE


def load(folder: Path) -> dict:
    return files.read_json(Path(folder) / FILE)


def made(folder: Path) -> dict:
    return files.read_json(Path(folder) / MADE)


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _long_plan(folder: Path, e: dict) -> dict:
    """The plan of the long version as render.run() made it (the same frame rate)."""
    source = folder / "recording.mp4"
    if not source.exists():
        source = folder / "screen.mp4"
    return timeline.plan(e, rendering.probe(source)["fps"])


def kept_seconds(p: dict, a: float, b: float) -> float:
    """How much of [a, b] of the recording remains after the cut."""
    return round(timeline.map_forward(b, p) - timeline.map_forward(a, p), 1)


def propose(folder: Path, settings: dict | None = None) -> dict:
    """Write products.json with one proposal per slide input of the outline; with the switch on
    "auto" they are accepted and approved at once. Needs no model: the outline already has spans,
    titles and summaries in the language of the lecture."""
    folder = Path(folder)
    e = edit.load(folder)
    if not e:
        raise ValueError("Für diese Aufnahme gibt es noch keinen Schnitt.")
    o = outline.load(folder)
    p = _long_plan(folder, e)
    auto = mode(settings) == "auto"
    products, notes = [], []
    if not outline.current(folder):
        notes.append("Ohne Gliederung gibt es keine Folien-Inputs zum Vorschlagen.")
    for x in (o.get("inputs") or []) if outline.current(folder) else []:
        a, b = float(x["start"]), float(x["end"])
        left = kept_seconds(p, a, b)
        if left < SHORTEST:
            notes.append(f"„{x.get('title', '')}“ bleibt nach dem Schnitt nur {left:.0f} s lang und wird nicht vorgeschlagen.")
            continue
        products.append({"id": f"t{len(products) + 1}", "title": (x.get("title") or "")[:TITLE_MAX],
                         "description": (x.get("summary") or "")[:DESCRIPTION_MAX], "spans": [[a, b]],
                         "state": "accepted" if auto else "proposed", "source": "input"})
    if outline.current(folder) and not o.get("inputs"):
        notes.append("Die Gliederung hat keine Folien-Inputs; du kannst ein Kapitel als Themenvideo hinzufügen.")
    data = {"version": 1, "created": _now(), "mode": mode(settings), "approved": _now() if auto and products else None,
            "products": products, "notes": notes}
    files.write_json(folder / FILE, data)
    return data


def _clean(x: dict, changes: dict, duration: float) -> str:
    """Apply the lecturer's changes to one product; returns an error or ''."""
    if "title" in changes:
        title = str(changes["title"] or "").strip()[:TITLE_MAX]
        if not title:
            return "Ein Themenvideo braucht einen Titel."
        x["title"] = title
    if "description" in changes:
        x["description"] = str(changes["description"] or "").strip()[:DESCRIPTION_MAX]
    if "state" in changes:
        if changes["state"] not in STATES:
            return "Unbekannter Zustand."
        x["state"] = changes["state"]
    if "start" in changes or "end" in changes:
        a = float(changes.get("start", x["spans"][0][0]))
        b = float(changes.get("end", x["spans"][0][1]))
        a, b = max(0.0, a), min(duration, b)
        if b - a < 10:
            return "Ein Themenvideo muss mindestens zehn Sekunden lang sein."
        x["spans"] = [[round(a, 2), round(b, 2)]]
    return ""


def change(folder: Path, pid: str, changes: dict) -> str:
    """Change one product (title, description, state, start, end); every change asks for a new approval."""
    folder = Path(folder)
    data, e = load(folder), edit.load(folder)
    x = next((x for x in data.get("products", []) if x["id"] == pid), None)
    if x is None or not e:
        return "Dieses Themenvideo gibt es nicht mehr."
    error = _clean(x, changes or {}, e["duration"])
    if error:
        return error
    data["approved"] = None
    files.write_json(folder / FILE, data)
    return ""


def add(folder: Path, start: float, end: float, title: str, description: str = "") -> str:
    """A topic video the lecturer adds, e.g. from a chapter; accepted, as they chose it."""
    folder = Path(folder)
    e = edit.load(folder)
    if not e:
        return "Für diese Aufnahme gibt es noch keinen Schnitt."
    data = load(folder) or {"version": 1, "created": _now(), "mode": "", "products": [], "notes": []}
    used = {x["id"] for x in data["products"]}
    pid = next(f"t{k}" for k in range(1, len(used) + 2) if f"t{k}" not in used)
    x = {"id": pid, "title": "", "description": "", "spans": [[0.0, 0.0]], "state": "accepted", "source": "lecturer"}
    error = _clean(x, {"title": title, "description": description, "start": start, "end": end}, e["duration"])
    if error:
        return error
    data["products"] = sorted([*data["products"], x], key=lambda x: x["spans"][0][0])
    data["approved"] = None
    files.write_json(folder / FILE, data)
    return ""


def approve(folder: Path, approved: bool = True) -> str:
    """The second approval: every proposal still open is accepted with it."""
    folder = Path(folder)
    data = load(folder)
    if not data:
        return "Es gibt noch keine Themenvideos."
    if approved:
        for x in data["products"]:
            if x["state"] == "proposed":
                x["state"] = "accepted"
        data["approved"] = _now()
    else:
        data["approved"] = None
    files.write_json(folder / FILE, data)
    return ""


def accepted(data: dict) -> list[dict]:
    return [x for x in data.get("products", []) if x["state"] == "accepted"] if data.get("approved") else []


def signature(folder: Path, x: dict) -> str:
    """Changes whenever the video of product x would change: a new render of the long version
    (cut, cards or style) or its own span and texts."""
    long = files.read_json(Path(folder) / "render.json")
    key = [long.get("rendered"), long.get("signature"), long.get("style"),
           {k: x.get(k) for k in ("id", "title", "description", "spans")}]
    return hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]


def due(folder: Path) -> bool:
    """Whether render() has something to do: an approved video not rendered for what it is now, a
    file of it gone, or a video rendered that is no longer wanted."""
    folder = Path(folder)
    wanted = {x["id"]: signature(folder, x) for x in accepted(load(folder))}
    have = {v["id"]: v for v in made(folder).get("videos", [])}
    if set(wanted) != set(have):
        return True
    return any(have[k].get("signature") != sig or not all(publish.locate(folder, n) for n in have[k].get("files", []))
               for k, sig in wanted.items())


def finished(folder: Path) -> list[dict]:
    """The rendered topic videos that belong to the present long version and are approved now."""
    folder = Path(folder)
    wanted = {x["id"]: signature(folder, x) for x in accepted(load(folder))}
    return [v for v in made(folder).get("videos", []) if wanted.get(v["id"]) == v.get("signature")]


def _window(p: dict, ins: list[dict], a: float, b: float) -> tuple[float, float]:
    """Span [a, b] of the recording in the rendered long version: after a card at its start (the
    topic video has its own title card), before a card at its end (the next chapter's)."""
    start = timeline.shift(timeline.map_forward(a, p), ins)
    end = timeline.shift(timeline.map_forward(b, p) - 1e-3, ins) + 1e-3
    return round(start, 3), round(end, 3)


def _own_inserts(e: dict, x: dict, length: float, s: dict) -> list[dict]:
    """Title and closing card of a topic video, intro and outro of the style; the switches and
    texts of the long version's cards, with the topic's title on the title card."""
    cfg = e.get("cards") or {}
    title = cfg.get("title") or {}
    own = {"items": [], "cards": {"title": {**title, "title": x["title"]}, "closing": cfg.get("closing") or {},
                                  "chapters": "none", "music": cfg.get("music", True)}}
    return timeline.inserts(own, {"duration": length}, s)


def _description(x: dict, chapters: list[dict]) -> str:
    rows = [x["title"], "", x.get("description", "")]
    if len(chapters) > 1:
        rows += ["", timeline.youtube_chapters(chapters)]
    return "\n".join(rows).strip() + "\n"


def render(folder: Path, video_args: list[str] | None = None, progress: Callable[[str], None] | None = None,
           sound_dir: Path | None = None, style_dir: Path | None = None) -> dict:
    """Render every approved topic video that is not rendered for what it is now, remove the ones
    no longer wanted, and write topics.json."""
    folder = Path(folder)
    say = progress or (lambda text: None)
    e = edit.load(folder)
    long = files.read_json(folder / "render.json")
    if not (e and e.get("approved") and long.get("signature") == rendering.signature(e)):
        raise ValueError("Die Langfassung ist nicht fertig gerendert.")
    video_args = video_args or rendering.X264
    s = rendering.measured(styles.load(style_dir))
    transcript = files.read_json(folder / "transcript.json")
    language = transcript.get("language") or files.read_json(folder / "session.json").get("language") or "de"
    p = _long_plan(folder, e)
    ins = timeline.inserts(e, p, s)
    info = rendering.probe(folder / "edited.mp4")
    frame = {"width": info["width"], "height": info["height"], "fps": rendering.probe(
        folder / ("recording.mp4" if (folder / "recording.mp4").exists() else "screen.mp4"))["fps"]}
    long_chapters = timeline.chapters(e, p, ins)
    wanted = accepted(load(folder))
    before = {v["id"]: v for v in made(folder).get("videos", [])}
    videos, warnings = [], []
    work = Path(tempfile.mkdtemp(prefix="flex3-topics-"))
    try:
        for k, x in enumerate(wanted, 1):
            name = f"topic-{k:02d}"
            sig = signature(folder, x)
            old = before.get(x["id"])
            if old and old.get("signature") == sig and old.get("name") == name and \
                    all(publish.locate(folder, n) for n in old.get("files", [])):
                videos.append(old)                                   # nothing changed
                continue
            a, b = _window(p, ins, *x["spans"][0])
            length = round(b - a, 3)
            if length < 10:
                warnings.append(f"„{x['title']}“ ist nach dem Schnitt kürzer als zehn Sekunden und wurde ausgelassen.")
                continue
            say(f"Themenvideo {k} von {len(wanted)} wird gerendert …")
            own = _own_inserts(e, x, length, s)
            part = folder / f"{name}.mp4.part"
            total = rendering.compose(folder / "edited.mp4", own, {**frame, "duration": length}, part, work, sound_dir,
                                   video_args, lambda text: say(f"Themenvideo {k}: {text}"), warnings, s, offset=a)
            part.replace(folder / f"{name}.mp4")
            written = [f"{name}.mp4"]
            if transcript:
                timed = [{**w, "start": timeline.shift(w["start"], own), "end": timeline.shift(w["end"], own)}
                         for w in timeline.words(transcript, p, a, b, ins)]
                (folder / f"{name}.{language}.vtt").write_text(timeline.vtt(timed), encoding="utf-8")
                written.append(f"{name}.{language}.vtt")
            chs = [{"start": 0.0, "title": x["title"]}]
            for c in long_chapters:
                if a + 1.0 <= c["start"] < b - 1.0:
                    chs.append({"start": timeline.shift(c["start"] - a, own), "title": c["title"]})
            t = timeline.shift(min(15.0, length / 3), own)
            subprocess.run([binaries.ffmpeg(), "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(folder / f"{name}.mp4"),
                            "-frames:v", "1", "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,"
                            "pad=1280:720:(ow-iw)/2:(oh-ih)/2", "-q:v", "3", str(folder / f"{name}.jpg")],
                           stdin=subprocess.DEVNULL, capture_output=True, creationflags=binaries.flags())
            if (folder / f"{name}.jpg").exists():
                written.append(f"{name}.jpg")
            (folder / f"{name}.txt").write_text(_description(x, chs), encoding="utf-8")
            written.append(f"{name}.txt")
            videos.append({"id": x["id"], "name": name, "file": f"{name}.mp4", "title": x["title"],
                           "description": x.get("description", ""), "duration": total, "files": written,
                           "chapters": chs, "start": a, "end": b, "source": x["spans"][0], "signature": sig})
        for old in folder.glob("topic-*.*"):                         # files of videos no longer wanted
            if old.name.split(".")[0] not in {v["name"] for v in videos} or old.name.endswith(".part"):
                old.unlink(missing_ok=True)
        report = {"rendered": _now(), "long": long.get("rendered"), "style_name": s["name"], "videos": videos,
                  "warnings": warnings}
        files.write_json(folder / MADE, report)
        return report
    finally:
        shutil.rmtree(work, ignore_errors=True)
        for part in folder.glob("topic-*.part"):
            part.unlink(missing_ok=True)


def view(folder: Path, settings: dict | None = None) -> dict:
    """What the window shows of the topic videos: the proposals with their length after the cut,
    and what is rendered of them."""
    folder = Path(folder)
    data, e = load(folder), edit.load(folder)
    out = {"mode": mode(settings), "proposed": bool(data), "approved": data.get("approved"),
           "notes": data.get("notes", []), "products": [], "videos": finished(folder),
           "due": bool(data.get("approved")) and due(folder), "warnings": made(folder).get("warnings", [])}
    if e:
        p = timeline.plan(e, 30.0)
        for x in data.get("products", []):
            a, b = x["spans"][0]
            out["products"].append({**x, "start": a, "end": b, "kept": kept_seconds(p, a, b)})
    return out
