"""Topic videos: stage G of the cut agent's plan (phase 7, built 2026-11-12).

From the approved long version and the outline, every topic piece (outline.json, "pieces", at
least 90 seconds: a talk, a demo of software or a worked example that stands on its own) is
proposed as a video of its own. Clips are a case of it since 2026-11-12: the cut agent proposes
none any more, and the clips of an edit list from before become proposals here, unless a topic
piece covers them. Above the pieces stand the topic blocks of the outline (a talk through one slide
deck or a demo of one program over several pieces): each is proposed as a video of its own too,
and the videos of its pieces and clips name it as their "parent", so there is a hierarchy of a
whole talk and its parts (Nicolas, 2026-11-12). The lecturer approves them in a second,
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
import time
from pathlib import Path
from typing import Callable

from . import binaries, edit, files, llm, outline, publish, timeline
from . import render as rendering      # render() below is the topic videos'
from . import style as styles

FILE = "products.json"
MADE = "topics.json"
MODES = ("off", "propose", "auto")
DEFAULT_MODE = "propose"
SHORTEST = 60.0          # seconds a topic video keeps after the cut at least
SAME_AS_BLOCK = 0.8      # a part that fills this much of its block is not proposed a second time
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
    """Write products.json with one proposal per topic block and topic piece of the outline and per
    clip of an older edit list that no piece covers, a block before the videos of its parts, which
    name it as their parent; with the switch on "auto" they are accepted and approved at once.
    Needs no model: the outline already has spans, titles and summaries in the language of the lecture."""
    folder = Path(folder)
    e = edit.load(folder)
    if not e:
        raise ValueError("Für diese Aufnahme gibt es noch keinen Schnitt.")
    o = outline.load(folder)
    p = _long_plan(folder, e)
    auto = mode(settings) == "auto"
    products, notes = [], []
    pieces = outline.pieces(o) if outline.current(folder) else []
    blocks = [{**x, "kind": "block"} for x in outline.blocks(o)] if outline.current(folder) else []
    if not outline.current(folder):
        notes.append("Ohne Gliederung gibt es keine Themenstücke zum Vorschlagen.")
    clips = [{"start": x["start"], "end": x["end"], "title": x["params"].get("title", ""),
              "summary": x["params"].get("summary", ""), "kind": "clip"}
             for x in e["items"] if x["kind"] == "clip" and x["state"] == "accepted" and x["end"] is not None]
    clips = [c for c in clips if not any(_overlap(c, x) > 0.5 * (c["end"] - c["start"]) for x in pieces)]
    parents: list[tuple[dict, str]] = []                  # the blocks proposed, with their id
    for x in sorted(blocks + pieces + clips, key=lambda x: (x["start"], x["kind"] != "block")):
        a, b = float(x["start"]), float(x["end"])
        left = kept_seconds(p, a, b)
        if left < SHORTEST:
            notes.append(f"„{x.get('title', '')}“ bleibt nach dem Schnitt nur {left:.0f} s lang und wird nicht vorgeschlagen.")
            continue
        pid = f"t{len(products) + 1}"
        home = next(((blk, i) for blk, i in parents if outline.inside(x, blk)), None) if x["kind"] != "block" else None
        parent = home[1] if home else None
        if home and b - a >= SAME_AS_BLOCK * (home[0]["end"] - home[0]["start"]):
            continue                                   # the block's video is this one already
        products.append({"id": pid, "title": (x.get("title") or "")[:TITLE_MAX],
                         "description": (x.get("summary") or "")[:DESCRIPTION_MAX], "spans": [[a, b]],
                         "state": "accepted" if auto else "proposed",
                         "source": {"clip": "clip", "block": "block"}.get(x["kind"], "piece"), "kind": x["kind"],
                         **({"parent": parent} if parent else {})})
        if x["kind"] == "block":
            parents.append((x, pid))
    if outline.current(folder) and not pieces and not blocks:
        notes.append("Die Gliederung hat keine Themenstücke; du kannst ein Kapitel als Themenvideo hinzufügen.")
    data = {"version": 1, "created": _now(), "mode": mode(settings), "approved": _now() if auto and products else None,
            "products": products, "notes": notes}
    files.write_json(folder / FILE, data)
    return data


def _overlap(x: dict, y: dict) -> float:
    return max(0.0, min(x["end"], y["end"]) - max(x["start"], y["start"]))


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
           "feedback": (data.get("feedback") or [])[-3:],
           "due": bool(data.get("approved")) and due(folder), "warnings": made(folder).get("warnings", [])}
    if e:
        p = timeline.plan(e, 30.0)
        for x in data.get("products", []):
            a, b = x["spans"][0]
            out["products"].append({**x, "start": a, "end": b, "kept": kept_seconds(p, a, b)})
    return out


# ---------------------------------------------------------------- the lecturer's feedback in words

REVISE_ROUNDS = 8
FEEDBACK_KEPT = 20             # entries of the feedback log kept in products.json

REVISE_INSTRUCTIONS = """You change the proposed topic videos of a lecture recording as the lecturer asks. A topic
video is a span of the recording cut from the approved long version (it keeps every cut made there); the videos form a
tree of two levels: a block (a whole slide deck) and, indented below it, the videos of its parts. The lecturer sees
them numbered by their id (video 3 is t3) and wrote what they want changed, in their own words.

Use the tools to make exactly the changes asked for, nothing else: merge videos into one, split one at a moment,
move a start or an end, change title or description, select or deselect a video, add a video for a span, put a video
under a block or take it out. Times are seconds of the recording. Where a bound has to sit at a particular sentence,
read the transcript around it first (read_transcript) and put the bound at the start or end of that sentence. Titles
and descriptions stay in the language of the lecture unless the lecturer asks otherwise.

End with finish: a short reply to the lecturer in {ui_language}, saying what you changed, or why you could not do
something. Ask nothing back; if a wish is unclear, do the likely thing and say so in the reply."""

_ID = {"type": "string", "description": "id of a video, e.g. t3"}
_T = {"type": "number", "description": "seconds of the recording"}
_STR = {"type": "string"}
REVISE_TOOLS = [
    llm.Tool("merge_videos", "Merge two or more videos into one that spans them all; it keeps the first id.", {
        "type": "object", "required": ["ids", "title"], "properties": {
            "ids": {"type": "array", "items": _ID}, "title": _STR, "description": _STR}}),
    llm.Tool("split_video", "Split a video in two at a moment; the second part gets a new id.", {
        "type": "object", "required": ["id", "at", "titles"], "properties": {
            "id": _ID, "at": _T, "titles": {"type": "array", "items": _STR, "minItems": 2, "maxItems": 2},
            "descriptions": {"type": "array", "items": _STR, "maxItems": 2}}}),
    llm.Tool("set_bounds", "Move the start and/or the end of a video.", {
        "type": "object", "required": ["id"], "properties": {"id": _ID, "start": _T, "end": _T}}),
    llm.Tool("set_texts", "Change the title and/or the description of a video.", {
        "type": "object", "required": ["id"], "properties": {"id": _ID, "title": _STR, "description": _STR}}),
    llm.Tool("set_selected", "Select a video (it will be made) or deselect it (it will not).", {
        "type": "object", "required": ["id", "selected"], "properties": {"id": _ID, "selected": {"type": "boolean"}}}),
    llm.Tool("add_video", "Add a video for a span of the recording.", {
        "type": "object", "required": ["start", "end", "title"], "properties": {
            "start": _T, "end": _T, "title": _STR, "description": _STR,
            "parent": {"type": "string", "description": "id of the block to put it under, or empty"}}}),
    llm.Tool("set_parent", "Put a video under a block, or take it out (parent empty).", {
        "type": "object", "required": ["id", "parent"], "properties": {"id": _ID, "parent": _STR}}),
    llm.Tool("read_transcript", "The sentences said between two moments, with their times.", {
        "type": "object", "required": ["start", "end"], "properties": {"start": _T, "end": _T}}),
    llm.Tool("finish", "End with a short reply to the lecturer.", {
        "type": "object", "required": ["reply"], "properties": {"reply": _STR}}),
]


def _clock(t: float) -> str:
    return outline._clock(t)


def _order(data: dict) -> None:
    """Blocks before their parts, everything by start."""
    parents = {x.get("parent") for x in data["products"]}
    data["products"].sort(key=lambda x: (x["spans"][0][0], 0 if x["id"] in parents or x.get("kind") == "block" else 1))


def _next_id(data: dict) -> str:
    used = {x["id"] for x in data["products"]} | set(data.get("retired", []))
    return next(f"t{k}" for k in range(1, len(used) + 2) if f"t{k}" not in used)


def listing(data: dict, p: dict) -> str:
    """The videos as the model reads them, as a tree."""
    rows = []
    for x in data.get("products", []):
        a, b = x["spans"][0]
        rows.append(("    " if x.get("parent") else "") + f"{x['id']} ({x.get('kind') or x.get('source', '')}) "
                    f"{_clock(a)}–{_clock(b)} ({a:.0f}–{b:.0f} s), {kept_seconds(p, a, b) / 60:.1f} min after the cut, "
                    f"{'selected' if x['state'] != 'rejected' else 'not selected'}: {x['title']}"
                    + (f" | {x['description']}" if x.get("description") else ""))
    return "\n".join(rows) or "(no videos)"


def _outline_text(o: dict) -> str:
    if not o or o.get("error"):
        return "No outline."
    rows = ["Phases:"] + [f"  {_clock(x['start'])}–{_clock(x['end'])} ({x['start']:.0f}–{x['end']:.0f} s) {x['kind']}: "
                          f"{x['title']}" for x in o.get("phases", [])]
    rows += ["Topics (chapters of the long version):"] + [
        f"  {x['start']:.0f}–{x['end']:.0f} s {x['title']}: {x.get('summary', '')}" for x in o.get("topics", [])]
    rows += ["Slide decks (blocks):"] + [f"  {x['start']:.0f}–{x['end']:.0f} s {x['title']}" for x in outline.blocks(o)]
    rows += ["Topic pieces:"] + [f"  {x['start']:.0f}–{x['end']:.0f} s {x['kind']}: {x['title']}" for x in outline.pieces(o)]
    return "\n".join(rows)


class _Editor:
    """The tools of the feedback round on products.json in memory."""

    def __init__(self, data: dict, e: dict, transcript: dict):
        self.data, self.e, self.transcript = data, e, transcript
        self.changes: list[str] = []

    def _get(self, pid: str) -> dict:
        x = next((x for x in self.data["products"] if x["id"] == pid), None)
        if x is None:
            raise ValueError(f"there is no video {pid}")
        return x

    def _parent_ok(self, x: dict, parent: str) -> str:
        if not parent:
            return ""
        blk = self._get(parent)
        if blk is x or blk.get("parent"):
            raise ValueError(f"{parent} cannot hold other videos (only a video at the top can)")
        if any(y.get("parent") == x["id"] for y in self.data["products"]):
            raise ValueError(f"{x['id']} holds videos of its own and cannot go under another")
        return parent

    def call(self, name: str, args: dict) -> str:
        if name == "read_transcript":
            a, b = float(args["start"]), float(args["end"])
            if b - a > 900:
                raise ValueError("read at most 15 minutes at a time")
            said = [s for s in self.transcript.get("segments", []) if s["end"] > a and s["start"] < b]
            return "\n".join(f"{s['start']:.1f}–{s['end']:.1f} {s['text'].strip()}" for s in said) or "(nothing said)"
        before = json.dumps(self.data["products"], sort_keys=True)
        if name == "merge_videos":
            ids = list(dict.fromkeys(args.get("ids") or []))
            if len(ids) < 2:
                raise ValueError("merge needs two videos or more")
            xs = [self._get(i) for i in ids]
            first = xs[0]
            parents = {x.get("parent") for x in xs}
            a, b = min(x["spans"][0][0] for x in xs), max(x["spans"][0][1] for x in xs)
            first.update(spans=[[a, b]], state="accepted", source="feedback",
                         kind="block" if any(x.get("kind") == "block" for x in xs) else first.get("kind"))
            err = _clean(first, {"title": args.get("title", ""), "description": args.get("description", first.get("description", ""))},
                         self.e["duration"])
            if err:
                raise ValueError(err)
            gone = set(ids[1:])
            self.data["products"] = [x for x in self.data["products"] if x["id"] not in gone]
            self.data.setdefault("retired", []).extend(sorted(gone))
            for y in self.data["products"]:
                if y.get("parent") in gone or (y.get("parent") and y is first):
                    y.pop("parent", None)
                    if y is not first:
                        y["parent"] = first["id"]
            if len(parents) == 1 and None not in parents and not any(y.get("parent") == first["id"] for y in self.data["products"]):
                first["parent"] = parents.pop()
            self.changes.append(f"{', '.join(ids)} zu {first['id']} zusammengelegt")
        elif name == "split_video":
            x = self._get(args["id"])
            a, b = x["spans"][0]
            at = float(args["at"])
            if not a + 10 <= at <= b - 10:
                raise ValueError(f"split {x['id']} inside its span, {a + 10:.0f} to {b - 10:.0f} s")
            titles = args.get("titles") or []
            descs = (args.get("descriptions") or []) + ["", ""]
            if len(titles) != 2 or not all(str(t).strip() for t in titles):
                raise ValueError("split needs two titles")
            y = {**x, "id": _next_id(self.data), "spans": [[round(at, 2), b]], "source": "feedback",
                 "title": str(titles[1]).strip()[:TITLE_MAX], "description": str(descs[1]).strip()[:DESCRIPTION_MAX]}
            x.update(spans=[[a, round(at, 2)]], title=str(titles[0]).strip()[:TITLE_MAX], source="feedback",
                     description=str(descs[0] or x.get("description", "")).strip()[:DESCRIPTION_MAX])
            self.data["products"].append(y)
            for z in self.data["products"]:
                if z.get("parent") == x["id"] and outline.inside({"start": z["spans"][0][0], "end": z["spans"][0][1]},
                                                                  {"start": at, "end": b}):
                    z["parent"] = y["id"]
            self.changes.append(f"{x['id']} bei {_clock(at)} geteilt, der zweite Teil ist {y['id']}")
        elif name == "set_bounds":
            x = self._get(args["id"])
            err = _clean(x, {k: args[k] for k in ("start", "end") if k in args}, self.e["duration"])
            if err:
                raise ValueError(err)
            self.changes.append(f"{x['id']} beginnt bei {_clock(x['spans'][0][0])} und endet bei {_clock(x['spans'][0][1])}")
        elif name == "set_texts":
            x = self._get(args["id"])
            err = _clean(x, {k: args[k] for k in ("title", "description") if k in args}, self.e["duration"])
            if err:
                raise ValueError(err)
            self.changes.append(f"Texte von {x['id']} geändert")
        elif name == "set_selected":
            x = self._get(args["id"])
            x["state"] = "accepted" if args.get("selected") else "rejected"
            self.changes.append(f"{x['id']} {'angewählt' if args.get('selected') else 'abgewählt'}")
        elif name == "add_video":
            x = {"id": _next_id(self.data), "title": "", "description": "", "spans": [[0.0, 0.0]], "state": "accepted",
                 "source": "feedback", "kind": "piece"}
            err = _clean(x, {"title": args.get("title", ""), "description": args.get("description", ""),
                             "start": args["start"], "end": args["end"]}, self.e["duration"])
            if err:
                raise ValueError(err)
            parent = self._parent_ok(x, str(args.get("parent") or ""))
            if parent:
                x["parent"] = parent
            self.data["products"].append(x)
            self.changes.append(f"{x['id']} hinzugefügt")
        elif name == "set_parent":
            x = self._get(args["id"])
            parent = self._parent_ok(x, str(args.get("parent") or ""))
            x.pop("parent", None)
            if parent:
                x["parent"] = parent
            self.changes.append(f"{x['id']} " + (f"unter {parent}" if parent else "steht jetzt für sich"))
        else:
            raise ValueError(f"unknown tool {name}")
        _order(self.data)
        return "done" if json.dumps(self.data["products"], sort_keys=True) != before else "nothing changed"


def revise(folder: Path, text: str, key, config: dict | None = None, settings: dict | None = None,
           client=None, progress: Callable[[str], None] | None = None) -> dict:
    """Apply the lecturer's feedback in words to the proposed topic videos with a language model;
    every change asks for a new approval. Returns the entry of the feedback log."""
    folder = Path(folder)
    say = progress or (lambda t: None)
    data, e = load(folder), edit.load(folder)
    if not data or not e:
        raise ValueError("Es gibt noch keine Themenvideos.")
    text = str(text or "").strip()[:4000]
    if not text:
        raise ValueError("Die Rückmeldung ist leer.")
    p = timeline.plan(e, 30.0)
    work = json.loads(json.dumps(data))
    editor = _Editor(work, e, files.read_json(folder / "transcript.json"))
    config = config or llm.stage_config("topics")
    if client is None:
        keys = key if callable(key) else (lambda service: key if service == "openai" else None)
        client = llm.make(config, keys)
    ui = (settings or {}).get("ui_language", "de")
    instructions = REVISE_INSTRUCTIONS.replace("{ui_language}", outline.LANGUAGES.get(ui, ui))
    first = (f"Outline of the recording:\n{_outline_text(outline.load(folder))}\n\n"
             f"The topic videos now (a tree; indented: the parts of the block above):\n{listing(work, p)}\n\n"
             f"What the lecturer wants:\n{text}")
    messages = [llm.Message("user", [llm.Text(first)])]
    reply, usage, started = "", llm.Usage(), time.monotonic()
    for rnd in range(REVISE_ROUNDS):
        say("Deine Rückmeldung wird umgesetzt …")
        answer = client.complete(instructions, messages, REVISE_TOOLS)
        usage.add(answer.usage)
        if not answer.calls:
            reply = answer.text.strip()
            break
        results, done = [], False
        for c in answer.calls:
            if c.name == "finish":
                reply, done = str(c.arguments.get("reply") or "").strip(), True
                results.append(llm.ToolResult(c.id, "ok"))
                continue
            try:
                out = editor.call(c.name, c.arguments)
                if c.name != "read_transcript":
                    out += "\n\nThe videos now:\n" + listing(work, p)
            except (ValueError, KeyError, TypeError) as err:
                out = f"error: {err}"
            results.append(llm.ToolResult(c.id, out))
        if done:
            break
        messages += [answer.message(), llm.Message("user", results)]
    entry = {"at": _now(), "text": text, "reply": reply or "Ich habe die Rückmeldung nicht ganz umsetzen können.",
             "changes": editor.changes, "model": getattr(client, "model", config.get("model", "")),
             "seconds": round(time.monotonic() - started, 1), "usage": usage.as_dict()}
    if editor.changes:
        work["approved"] = None
    work["feedback"] = (work.get("feedback") or [])[-(FEEDBACK_KEPT - 1):] + [entry]
    files.write_json(folder / FILE, work)
    return entry
