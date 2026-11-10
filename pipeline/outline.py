"""Stage C of the cut: the outline of a recording, from its overview, by a strong language model.

The model reads only text: the overview of stage A (overview.json, one block per minute with what
was said, loudness, picture changes, slide titles and programs in front) and writes, with one
tool call, three things in seconds of the recording:

- phases that cover the whole recording without gaps: input, demo, exercise, discussion,
  organisation, break, technical, idle;
- topics: the chapters of the long version, each a span of teaching on one subject;
- slide inputs: spans where the lecturer teaches with slides, with start and end, so that each
  can later become a video of its own (stage G).

The program checks what comes back (order, bounds, at least three topics of at least ten seconds)
and gives the model its mistakes to correct; boundaries snap to the start or end of the nearest
sentence. The result is outline.json; the cut agent gets it in its first message, and the
approval shows it. Nothing leaves the house but text (plan of 2026-11-10, "Was an Daten wohin geht").
"""
from __future__ import annotations

import bisect
import datetime as dt
import time
from pathlib import Path
from typing import Callable

from . import files, llm

VERSION = 1
SNAP = 15.0                  # seconds a boundary may move to the start or end of a sentence
SHORTEST_PHASE = 5.0         # shorter phases go into the one before
SHORTEST_TOPIC = 10.0
SHORTEST_INPUT = 90.0       # a slide input is a video of its own later, not a single slide
ROUNDS = 4                   # tries to get a valid outline before taking what is valid of the last

KINDS = {"input": "Input", "demo": "Demo", "exercise": "Übung", "discussion": "Diskussion",
         "organisation": "Organisatorisches", "break": "Pause", "technical": "Technik", "idle": "Leerlauf"}
LANGUAGES = {"de": "German", "en": "English"}

INSTRUCTIONS = """You outline a lecture recording at a university of applied sciences, before it is cut. You get an
overview of the whole recording, one block per minute: what the lecturer said, how much was spoken, the loudness,
picture changes, the titles of slides that appeared (read by text recognition, so with errors; menu bars of programs
are no slides), the programs in front, words of other voices from the room, and spans marked private.

Call set_outline once with:

phases: the course of the session, covering it from 0 without gaps, each with the time it starts. Kinds: input (the
lecturer teaches, usually with slides), demo (shows something in a program, live coding), exercise (students work,
the lecturer mostly silent or helping individually), discussion (questions and answers with the room), organisation
(dates, submissions, exams, groups, things only for the people in the room), break, technical (microphone checks,
trouble with devices or software), idle (arriving, setting up, waiting, the edges of the recording). A phase is a
stretch of several minutes as a rule; do not split for a single sentence. Give each phase a short title that says what
the whole phase is about, not only its beginning, and, where it helps, a note, both in {ui_language}.

topics: the chapters of the long version, in order and without overlap, each a span of teaching on one subject with
start and end: at least three, each at least ten seconds; idle, breaks, technical trouble and organisation get no
topic. A topic can span an input, a demo and an exercise on the same subject. Title in about five words and a
summary in one or two sentences, both in the language of the lecture ({language}); slide titles are good titles.

inputs: every slide input, that is a continuous stretch where the lecturer presents a subject with slides (a
presentation in front, slide titles changing, the lecturer speaking continuously), from the sentence that opens the
subject to the sentence that closes it, before a demo, an exercise or another subject begins. Each input may later
become a short video of its own, so it must make sense alone and is several minutes long as a rule, at least 90
seconds: neighbouring passages on the same subject form one input, not one per slide. Going through a quiz or
questions with the room is discussion, not an input, even with slides and explanations in between. Title and summary
in the language of the lecture. No inputs if there are none.

summary: two or three sentences in {ui_language} for the lecturer on how the session went, as phases and topics.

Times are seconds of the recording (the blocks give both clock and seconds). Boundaries move to the nearest start
or end of a sentence by themselves. If set_outline reports problems, fix them and call it again."""

T = {"type": "number"}
S = {"type": "string"}
TOOL = llm.Tool("set_outline", "The outline of the recording: phases, topics, slide inputs and a summary.", {
    "type": "object", "additionalProperties": False, "required": ["phases", "topics", "inputs", "summary"],
    "properties": {
        "phases": {"type": "array", "items": {"type": "object", "required": ["start", "kind", "title"], "properties": {
            "start": T, "kind": {"type": "string", "enum": list(KINDS)}, "title": S, "note": S}}},
        "topics": {"type": "array", "items": {"type": "object", "required": ["start", "end", "title", "summary"],
                                              "properties": {"start": T, "end": T, "title": S, "summary": S}}},
        "inputs": {"type": "array", "items": {"type": "object", "required": ["start", "end", "title", "summary"],
                                              "properties": {"start": T, "end": T, "title": S, "summary": S}}},
        "summary": S}})


def _clock(t: float) -> str:
    h, rest = divmod(int(max(0.0, t)), 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}"


def load(folder: Path) -> dict:
    return files.read_json(Path(folder) / "outline.json")


def current(folder: Path) -> bool:
    """Whether the recording has an outline (a failed try does not count)."""
    o = load(folder)
    return bool(o) and not o.get("error")


def material(overview: dict, meta: dict, language: str) -> str:
    """The overview as text for the model: a head, the long pauses and one paragraph per block."""
    programs = ", ".join(f"{p['program']} {round(p['seconds'] / 60)} min" for p in overview.get("programs", []))
    head = [f"Module: {meta.get('course_name', '')}", f"Language of the lecture: {language}",
            f"Duration: {_clock(overview.get('duration', 0))} ({overview.get('duration', 0)} s)",
            "Programs in front: " + (programs or "unknown")]
    long = [f"{_clock(a)}–{_clock(b)} ({round(b - a)} s)" for a, b in overview.get("pauses", []) if b - a >= 30]
    head.append("Pauses of 30 s and more: " + (", ".join(long) if long else "none"))
    out = ["\n".join(head)]
    for b in overview.get("blocks", []):
        facts = [f"speech {round(b['speech'])} s, silence {round(b['silence'])} s"]
        if b.get("level") is not None:
            facts.append(f"level {b['level']} dB")
        if b.get("changes"):
            facts.append(f"picture changes {b['changes']}")
        if b.get("programs"):
            facts.append("programs " + ", ".join(b["programs"]))
        if b.get("room_words"):
            facts.append(f"room voices {b['room_words']} words")
        if b.get("private"):
            facts.append("marked private")
        lines = [f"## {_clock(b['start'])} ({b['start']:.0f}–{b['end']:.0f} s) | " + " | ".join(facts)]
        if b.get("slides"):
            lines.append("slides: " + "; ".join(f"{_clock(s['t'])} „{s['title']}“" for s in b["slides"]))
        lines.append(b.get("text") or "(nothing said)")
        out.append("\n".join(lines))
    return "\n\n".join(out)


def _snap(t: float, marks: list[float]) -> float:
    """t moved to the nearest mark within SNAP seconds, else unchanged."""
    if not marks:
        return t
    i = bisect.bisect_left(marks, t)
    near = min((marks[j] for j in (i - 1, i) if 0 <= j < len(marks)), key=lambda m: abs(m - t))
    return near if abs(near - t) <= SNAP else t


def _spans(raw: list, name: str, shortest: float, duration: float, starts: list[float], ends: list[float],
           errors: list[str]) -> list[dict]:
    out, given_end = [], 0.0                      # overlaps count in the model's own times, before snapping
    for i, x in enumerate(sorted(raw or [], key=lambda x: float(x.get("start", 0)))):
        try:
            a, b = float(x["start"]), float(x["end"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"{name} {i + 1}: start and end must be numbers")
            continue
        title = str(x.get("title") or "").strip()
        if not title:
            errors.append(f"{name} {i + 1} at {_clock(a)}: title missing")
        a, b = max(0.0, min(a, duration)), max(0.0, min(b, duration))
        if out and a < given_end - 0.5:
            errors.append(f"{name} {i + 1} ({title}, starts {_clock(a)}) overlaps the one before "
                          f"({out[-1]['title']}, ends {_clock(given_end)})")
            continue
        given = b
        a, b = _snap(a, starts), _snap(b, ends)
        if out and a < out[-1]["end"]:              # touching spans snapped past each other
            a = out[-1]["end"]
        if b - a < shortest:
            errors.append(f"{name} {i + 1} ({title or '?'}, {_clock(a)}–{_clock(b)}): shorter than {shortest:.0f} s")
            continue
        given_end = given
        out.append({"start": round(a, 2), "end": round(b, 2), "title": title,
                    "summary": str(x.get("summary") or "").strip()})
    return out


def check(raw: dict, duration: float, segments: list[dict]) -> tuple[dict, list[str]]:
    """The outline the model proposed, made consistent; and what the model has to fix."""
    errors: list[str] = []
    starts = sorted(s["start"] for s in segments)
    ends = sorted(s["end"] for s in segments)
    phases = []
    for x in sorted(raw.get("phases") or [], key=lambda x: float(x.get("start", 0))):
        kind = x.get("kind")
        if kind not in KINDS:
            errors.append(f"phase at {x.get('start')}: unknown kind {kind!r}")
            continue
        a = 0.0 if not phases else _snap(max(0.0, min(float(x.get("start", 0)), duration)), starts)
        if phases and kind == phases[-1]["kind"]:
            continue                                     # the same kind again continues the phase
        if phases and a - phases[-1]["start"] < SHORTEST_PHASE:
            phases[-1].update(kind=kind, title=str(x.get("title") or "").strip(), note=str(x.get("note") or "").strip())
            continue
        phases.append({"start": round(a, 2), "kind": kind, "title": str(x.get("title") or "").strip(),
                       "note": str(x.get("note") or "").strip()})
    if not phases:
        errors.append("phases: none; they must cover the recording from 0")
    for p, q in zip(phases, phases[1:] + [None]):
        p["end"] = q["start"] if q else round(duration, 2)
    topics = _spans(raw.get("topics"), "topic", SHORTEST_TOPIC, duration, starts, ends, errors)
    if len(topics) < 3 and duration >= 600:
        errors.append(f"topics: {len(topics)}, at least three are needed")
    inputs = _spans(raw.get("inputs"), "input", SHORTEST_INPUT, duration, starts, ends, errors)
    return {"phases": phases, "topics": topics, "inputs": inputs,
            "summary": str(raw.get("summary") or "").strip()}, errors


def _slides(overview: dict, a: float, b: float) -> list[dict]:
    return [{"t": s["t"], "title": s["title"]} for s in overview.get("slides", []) if a <= s["t"] < b]


def run(folder: Path, key, settings: dict | None = None, progress: Callable[[str], None] | None = None,
        config: dict | None = None, client=None) -> dict:
    """Write outline.json for a session folder that has overview.json; returns it. A model that fails
    leaves outline.json with "error", so the cut goes on without it and the next proposal tries again."""
    folder = Path(folder)
    say = progress or (lambda text: None)
    overview = files.read_json(folder / "overview.json")
    if not overview:
        raise RuntimeError("Keine Übersicht (overview.json); die Sichtung muss zuerst laufen.")
    transcript = files.read_json(folder / "transcript.json")
    meta = files.read_json(folder / "session.json")
    language = transcript.get("language") or meta.get("language") or "de"
    ui = (settings or {}).get("ui_language", "de")
    duration = float(overview.get("duration") or transcript.get("duration") or 0)
    segments = transcript.get("segments", [])
    config = config or llm.stage_config("outline")
    started, rounds, usage, corrections = time.monotonic(), 0, llm.Usage(), []
    agent = {"model": config.get("model", ""), "adapter": config.get("adapter", "")}
    try:
        if client is None:
            keys = key if callable(key) else (lambda service: key if service == "openai" else None)
            client = llm.make(config, keys)
        instructions = (INSTRUCTIONS.replace("{ui_language}", LANGUAGES.get(ui, ui))
                        .replace("{language}", LANGUAGES.get(language, language)))
        messages = [llm.Message("user", [llm.Text(material(overview, meta, language))])]
        result, errors = None, ["no outline yet"]
        while rounds < ROUNDS:
            rounds += 1
            say("Gliederung: das Sprachmodell liest die Übersicht …" if rounds == 1
                else f"Gliederung: Korrektur {rounds - 1} …")
            reply = client.complete(instructions, messages, [TOOL])
            usage.add(reply.usage)
            calls = [c for c in reply.calls if c.name == "set_outline"]
            if not calls:
                messages += [reply.message(), llm.Message("user", [llm.Text("Call set_outline with the outline.")])]
                continue
            result, errors = check(calls[-1].arguments, duration, segments)
            if not errors:
                break
            corrections.append(errors)
            messages += [reply.message(), llm.Message("user", [
                llm.ToolResult(c.id, "Problems, fix them and call set_outline again:\n- " + "\n- ".join(errors)
                               if c is calls[-1] else "ignored, only the last call counts") for c in reply.calls])]
        if result is None:
            raise llm.ModelError("Das Sprachmodell hat keine Gliederung geliefert.")
    except llm.ModelError as e:
        out = {"version": VERSION, "error": str(e)[:500], "created": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
               "agent": {**agent, "rounds": rounds, "seconds": round(time.monotonic() - started, 1), "usage": usage.as_dict()}}
        files.write_json(folder / "outline.json", out)
        return out
    for x in result["inputs"]:
        x["slides"] = _slides(overview, x["start"], x["end"])
    out = {"version": VERSION, "created": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
           "duration": round(duration, 1), "language": language, **result,
           "problems": errors,                             # what was still wrong after the last try, left out
           "agent": {**agent, "model": getattr(client, "model", agent["model"]), "rounds": rounds,
                     "corrections": corrections, "seconds": round(time.monotonic() - started, 1),
                     "usage": usage.as_dict()}}
    files.write_json(folder / "outline.json", out)
    return out


def lines(folder: Path) -> str:
    """The outline for the cut agent's first message; empty without one."""
    o = load(folder)
    if not o or o.get("error"):
        return ""
    rows = ["Outline of the recording (stage C, from the overview; use its topics as chapters unless you see reason "
            "not to, and its phases to find idle time, breaks, organisation and technical trouble):", "Phases:"]
    rows += [f"  {_clock(p['start'])}–{_clock(p['end'])} {p['start']:.0f}-{p['end']:.0f} {p['kind']}: {p['title']}"
             for p in o.get("phases", [])]
    rows.append("Topics:")
    rows += [f"  {t['start']:.0f}-{t['end']:.0f} {t['title']}: {t['summary']}" for t in o.get("topics", [])]
    if o.get("inputs"):
        rows.append("Slide inputs (candidates for short videos):")
        rows += [f"  {x['start']:.0f}-{x['end']:.0f} {x['title']}" for x in o["inputs"]]
    return "\n".join(rows)
