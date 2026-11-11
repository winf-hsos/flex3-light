"""Version 2 of the cut agent: the fine cut in sections, each with a fresh context (stage D of the
plan of 2026-11-10), and a closing round for title, clips and summary.

Version 1 (cut_agent.CutAgent) is one long conversation over the whole recording: every round carries
the whole transcript and every picture seen so far. Version 2 splits the recording along its outline
(stage C, outline.json) into sections of about a quarter of an hour and gives each section a
conversation of its own, with the transcript of that section only, the items already in the list
there and the locks there; several sections are cut at the same time (WORKERS). The agent of a section can only look,
propose and decide inside its section (and a margin around it); a section that is only idle time,
a break, technical trouble or organisation goes to a small model, teaching to the strong one. The
chapters come from the topics of the outline. A closing round sees the whole list, but no transcript:
it writes title and description, picks clips and the thumbnail, fixes what check_edit reports and
writes the summary. Then everything ends as in version 1 (decide_rest, cards, edit.agent.json).

Which version runs is configuration of the server (models.json, "cut_version") or the wish of the
lecturer for one proposal (pipeline.json, "cut_version"); version 1 stays the default until the
lecturer prefers version 2 on the test recordings.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from . import llm, locks, outline
from .cut_agent import DOING, CutAgent, _tool, tools_for
from .cut_tools import Workbench, as_json, summary_line

LIGHT_KINDS = {"idle", "break", "technical", "organisation"}     # phases a small model can decide
TARGET = 900.0               # seconds a section aims at
LONGEST = 1500.0             # a section is not longer than this
OWN = 180.0                  # a stretch without teaching of at least this length gets a section of its own
MARGIN = 90.0                # seconds around its section an agent may look at
WORKERS = 4                  # sections cut at the same time
WORDS_SHOWN = 800            # words get_words returns at most; the transcript by sentence is in the first message
VERSION = 2

S = {"type": "string"}
SECTION_DONE = _tool("section_done", "End your section with a note for the next section and the items worth a look.",
                     {"note": {"type": "string", "description": "what the next section should know: what is going on "
                                                                "at your end, a span you left open at the border"},
                      "check": {"type": "array", "items": {"type": "object", "properties": {"id": S, "why": S},
                                                           "required": ["id", "why"]}}},
                     ["note", "check"])
SECTION_DONE["strict"] = False

COMMON = """You edit lecture recordings at a university of applied sciences: a list of proposals the lecturer approves
before anything is rendered. Goal: a long version that someone who was not there understands and likes to watch to
the end, that loses nothing of the teaching and keeps nothing private.

Standards: no clipped word; no transition that refers to something cut away ("as you just saw"); two pieces in a row
sound like one thought. Explanations, examples, reasons and summaries stay complete; demos are shortened, not removed
(goal, the steps that carry it, result); false starts and repetitions stay where a cut would be bumpier than the
original. Arriving, technical trouble, waiting, silent work and breaks are out or replaced by a pause card. No voice
from the room is audible unless the lecturer repeats the question; no readable name, mail address, key, chat or
mailbox in the picture; for a voice in the middle of the lecturer's speech, a mute is better than a cut. The
recording is watched first by students of this term who missed the session: what they need to take part stays (the
plan of the session, what to do for an assignment or a challenge, how to register for it); what only concerned the
people in the room at that moment goes (who is here, a cable, waiting for latecomers, a vote being opened, calling
or checking single teams or people, handing devices out or back, a course evaluation, arrangements for the next
dates).

Give every cut, pause, mute, freeze and blur a category: dead_time, pause, technical, off_record, organisation,
privacy or other. Never place a blur or a zoom by eye: find the text with locate_text, take its region, and check it
with check_region; where the content moves during the span, check start, middle and end. Privacy locks are hard: the
video is not rendered while one stays visible or audible; hide each with mute (voices), blur (text in the picture) or
a cut, and if you are sure one is a false alarm, leave it and say so in a note; only the lecturer lifts a lock.
Candidates are weaker hints: look and decide.

Write reasons and notes in {ui_language}, titles in the language of the lecture. Times are seconds of the recording;
boundaries of spans move out of words by themselves."""

SECTION = COMMON + """

You work on one section of the recording only; other sections are handled in conversations of their own. The outline
of the whole recording tells you where you are. Decide every rule proposal in your section with assess_rule, propose
what your section needs, and leave nothing open: what you propose is accepted unless you set state "rejected" to show
an alternative you weighed. The chapters come from the outline; change one only if its start or title is wrong. Do
not write title, description, clips or the summary; a closing round does that. Work from coarse to fine: read the
transcript, look at the picture where it matters (contact sheets for long spans), listen where voices from the room
may be. A span that runs over your end belongs to you if it starts in your section. When you are done, call
check_edit, fix what concerns your section, and end with section_done.

Work in few rounds, because every round carries all that came before: ask for everything a step needs at once
(several tool calls in one round: the frames, words and spans you want to check), and propose and decide several
items in one round. The transcript of your section is above; use get_words only for short spans around a boundary."""

CLOSING = COMMON + """

The fine cut is done, section by section. You see the whole edit list, the outline and the notes of the sections.
Now:
- set_metadata: a title of one line of about eight words and a description for the video platforms, in the language
  of the lecture; the title appears on the title card.
- clips: propose a clip (params title, summary) for every passage that stands on its own as a short video, above all
  the slide inputs of the outline that stay largely intact; a clip spans what remains of the passage.
- a thumbnail at a moment with a clear, telling picture (look at it first).
- borders between sections: two cuts that touch or a remnant too short to keep between them, merge or adjust them.
- call check_edit and fix what it reports.
- set_summary, always in the same shape: for every category cut, one sentence on what kind of passages these are and
  why they go (classes, not single cuts); "check": at most five items where a decision was close (from the notes of
  the sections or your own); "notes": at most three short remarks that matter to the lecturer.
- finish with a short summary in {ui_language}.

Work in few rounds, because every round carries all that came before: make all proposals and changes of a kind in
one round, as several tool calls at once."""

SECTION_ONLY = {"set_metadata", "set_summary", "finish"}
CLOSING_TOOLS = {"get_words", "search_transcript", "get_frame", "get_contact_sheet", "list_items", "propose",
                 "change_item", "withdraw_item", "assess_rule", "check_edit", "set_metadata", "set_summary", "finish"}


def _clock(t: float) -> str:
    return outline._clock(t)


def _split(a: float, b: float, cuts: list[float]) -> list[tuple[float, float]]:
    """A stretch longer than LONGEST split near TARGET, at the given points if one fits."""
    out, cur = [], a
    while b - cur > LONGEST:
        goal = cur + TARGET
        fits = [t for t in cuts if cur + 0.6 * TARGET <= t <= min(cur + LONGEST, b - OWN)]
        cut = min(fits, key=lambda t: abs(t - goal)) if fits else goal
        out.append((cur, cut))
        cur = cut
    out.append((cur, b))
    return out


def plan_sections(plan: dict, duration: float, segments: list[dict]) -> list[dict]:
    """The sections of the fine cut: the phases of the outline, long ones split at topics or sentences,
    short ones joined; a stretch without teaching of some minutes stays a section of its own (light)."""
    topics = [t["start"] for t in plan.get("topics", [])]
    sentences = [s["start"] for s in segments]
    phases = [p for p in plan.get("phases", []) if p["end"] > p["start"]] or \
        [{"start": 0.0, "end": duration, "kind": "input", "title": ""}]
    pieces = []
    for p in phases:
        for a, b in _split(p["start"], p["end"], [t for t in topics if p["start"] < t < p["end"]] or sentences):
            pieces.append({"start": a, "end": b, "kind": p["kind"], "title": p.get("title", "")})
    sections: list[dict] = []
    cur = None
    for p in pieces:
        light = p["kind"] in LIGHT_KINDS
        if cur is not None:
            length = cur["end"] - cur["start"]
            switch = light != cur["light"] and length >= OWN and p["end"] - p["start"] >= OWN
            if length < TARGET and not switch and p["end"] - cur["start"] <= LONGEST:
                cur["end"] = p["end"]
                cur["light"] = cur["light"] and light
                cur["phases"].append(p)
                continue
            sections.append(cur)
        cur = {"start": p["start"], "end": p["end"], "light": light, "phases": [p]}
    if cur is not None:
        sections.append(cur)
    if len(sections) > 1 and sections[-1]["end"] - sections[-1]["start"] < OWN:
        last = sections.pop()                       # a short rest goes to the section before
        sections[-1]["end"] = last["end"]
        sections[-1]["light"] = sections[-1]["light"] and last["light"]
        sections[-1]["phases"] += last["phases"]
    for i, s in enumerate(sections):
        s["no"] = i + 1
    return sections


def words_text(words: list[dict]) -> str:
    """Words as "time word", a speaker label where it changes, "?" after an unsure word: a fraction of the
    tokens of the JSON version 1 gets."""
    out, speaker = [], None
    for w in words[:WORDS_SHOWN]:
        if w.get("speaker") is not None and w["speaker"] != speaker:
            speaker = w["speaker"]
            out.append(f"[{speaker}]")
        conf = w.get("conf")
        out.append(f"{w['t']:.2f} {w['word']}" + ("?" if conf is not None and conf < 0.6 else ""))
    more = f" … and {len(words) - WORDS_SHOWN} more words; ask for a shorter span" if len(words) > WORDS_SHOWN else ""
    return (" ".join(out) + more) or "no words in this span"


def heard_text(words: list[dict]) -> str:
    """What listen heard, by turn of speaker: [B 812.4-815.0] darf ich etwas fragen."""
    turns: list[dict] = []
    for w in words:
        if turns and turns[-1]["speaker"] == w.get("speaker"):
            turns[-1]["end"] = w["t"]
            turns[-1]["words"].append(w["word"])
        else:
            turns.append({"speaker": w.get("speaker"), "start": w["t"], "end": w["t"], "words": [w["word"]]})
    return "\n".join(f"[{t['speaker']} {t['start']:.1f}-{t['end']:.1f}] {' '.join(t['words'])}" for t in turns) \
        or "nothing heard"


def _compact(bench: Workbench, name: str, args: dict) -> str | None:
    """The output of the word tools in few tokens; None for every other tool."""
    if name == "get_words":
        return words_text(bench.get_words(float(args["start"]), float(args["end"])))
    if name == "listen":
        return heard_text(bench.listen(float(args["start"]), float(args["end"]))["words"])
    return None


def _window_items(bench: Workbench, a: float, b: float) -> list[dict]:
    return [x for x in bench.edit["items"] if a - MARGIN <= x["start"] <= b + MARGIN]


class SectionAgent(CutAgent):
    """The agent of one section: the tools of version 1, but only inside its section and a margin. Several
    run at once on the same workbench; the lock lets one tool run at a time, the models think in parallel."""

    def __init__(self, bench: Workbench, client, sec: dict, lock: threading.RLock,
                 progress: Callable[[str], None] | None = None):
        super().__init__(bench, client, progress)
        self.sec, self.lock = sec, lock
        self.window = (sec["start"], sec["end"])
        self.note, self.checks = "", []

    def _log(self, **entry) -> None:
        with self.lock:
            super()._log(**entry)

    def _inside(self, t: float) -> bool:
        a, b = self.window
        return a - MARGIN <= t <= b + MARGIN

    def _call(self, name: str, args: dict):
        self._guard(name, args)
        if name == "listen":                          # minutes at AssemblyAI: the other sections go on meanwhile
            with self.lock:
                self.bench._audio()
            return _compact(self.bench, name, args), None
        with self.lock:
            a, b = self.window
            if name == "list_items":
                return "\n".join(summary_line(x) for x in _window_items(self.bench, a, b)) or "empty", None
            if name == "section_done":
                self.note = str(args.get("note") or "").strip()
                self.checks = [c for c in args.get("check") or [] if isinstance(c, dict) and c.get("id")]
                return "done", None
            text = _compact(self.bench, name, args)
            return (text, None) if text is not None else super()._call(name, args)

    def _guard(self, name: str, args: dict) -> None:
        """Refuse what lies outside the section."""
        with self.lock:
            a, b = self.window
            for k in ("t", "start", "end"):
                v = args.get(k)
                if isinstance(v, (int, float)) and not self._inside(float(v)):
                    raise ValueError(f"{k} = {v} is outside your section {a:.0f}-{b:.0f} s (with {MARGIN:.0f} s "
                                     "around it); the neighbouring section decides there")
            if name == "propose" and isinstance(args.get("start"), (int, float)) and not a - 30 <= args["start"] <= b:
                raise ValueError(f"a proposal of your section starts between {a:.0f} and {b:.0f} s")
            if name in ("change_item", "withdraw_item", "assess_rule"):
                x = next((x for x in self.bench.edit["items"] if x["id"] == args.get("id")), None)
                if x is not None and not self._inside(x["start"]):
                    raise ValueError(f"{x['id']} is not in your section; the section around {x['start']:.0f} s decides it")


class StagedCut(CutAgent):
    """The cut in sections with fresh contexts, several at once, and a closing round."""

    def __init__(self, bench: Workbench, clients: Callable[[str], object],
                 progress: Callable[[str], None] | None = None, workers: int | None = None):
        """clients: a new client for a stage ("section", "section_light", "closing"); a client keeps the state
        of its conversation, so every section gets its own."""
        super().__init__(bench, clients("closing"), progress)
        self.clients, self.workers = clients, workers or WORKERS
        self.lock = threading.RLock()
        self.notes: list[dict] = []
        self.checks: list[dict] = []
        self.sections: list[dict] = []
        self.done = 0

    def doing(self, tool: str) -> str:
        return f"Abschluss: {DOING.get(tool, 'arbeitet')}"

    def _call(self, name: str, args: dict):
        text = _compact(self.bench, name, args)
        return (text, None) if text is not None else super()._call(name, args)

    # ---------------------------------------------------------- what the conversations get
    def _chapters(self, plan: dict) -> None:
        """The topics of the outline as chapters, the first at 0:00."""
        if "chapter" in self.bench.kinds_off or any(x["kind"] == "chapter" for x in self.bench.edit["items"]):
            return
        for i, t in enumerate(plan.get("topics", [])):
            self.bench.propose("chapter", 0.0 if i == 0 else t["start"], None, "Thema der Gliederung", 0.8,
                               title=t["title"], summary=t.get("summary", ""))

    def _section_message(self, sec: dict, n: int) -> str:
        a, b = sec["start"], sec["end"]
        b_ = self.bench
        parts = ["Recording: " + as_json(b_.overview()),
                 outline.lines(b_.folder) or "No outline of the recording.",
                 f"Your section: {sec['no']} of {n}, {_clock(a)}–{_clock(b)} ({a:.0f}–{b:.0f} s). Phases in it: "
                 + "; ".join(f"{_clock(p['start'])} {p['kind']} {p['title']}" for p in sec["phases"])
                 + ". The sections next to yours are cut at the same time; a closing round smooths the borders."]
        here = _window_items(b_, a, b)
        parts.append("Items in your section so far (decide the rule proposals):\n" + "\n".join(summary_line(x) for x in here)
                     if here else "No items in your section yet.")
        found = locks.load(b_.folder)
        mine = [line for x, line in zip(locks.state(b_.folder, b_.edit), locks.lines(b_.folder, b_.edit))
                if x["end"] > a - MARGIN and x["start"] < b + MARGIN]
        cands = [x for x in found.get("candidates", []) if x["end"] > a - MARGIN and x["start"] < b + MARGIN]
        parts.append(("Privacy locks here (hide every one):\n" + "\n".join(mine)) if mine else "No privacy locks here.")
        if cands:
            parts.append("Candidates here (look and decide):\n" + "\n".join(
                f"{x['id']} {x['kind']} ({x['sense']}) {x['start']:.1f}-{x['end']:.1f}"
                + (f" region {x['region']}" if x.get("region") else "") + (f" {x['text']}" if x.get("text") else "")
                for x in cands[:40]))
        if not found.get("checked"):
            parts.append("The safety net has not looked at this recording; check privacy yourself.")
        sentences = "\n".join(
            ("  " if s["start"] < a or s["start"] >= b else "") + f"[{_clock(s['start'])} {s['start']:.1f}-{s['end']:.1f}] {s['text']}"
            for s in b_.segments if a - 30 <= s["start"] < b + 30)
        parts.append("Transcript of your section by sentence (indented lines lie outside it, for context):\n" + sentences)
        return "\n\n".join(parts)

    def _closing_message(self, plan: dict) -> str:
        b_ = self.bench
        inputs = plan.get("inputs", [])
        return "\n\n".join([
            "Recording: " + as_json(b_.overview()),
            outline.lines(b_.folder) or "No outline of the recording.",
            ("Slide inputs (clip candidates): " + "; ".join(f"{x['start']:.0f}-{x['end']:.0f} {x['title']}" for x in inputs))
            if inputs else "No slide inputs.",
            "Notes of the sections:\n" + ("\n".join(f"{n['section']}: {n['note']}" for n in self.notes if n["note"]) or "none"),
            "Worth a look, from the sections:\n" + "\n".join(f"{c['id']}: {c.get('why', '')}" for c in self.checks)
            if self.checks else "The sections marked nothing worth a look.",
            "The edit list:\n" + "\n".join(summary_line(x) for x in b_.edit["items"]),
            "Problems now: " + as_json(b_.check() or ["none"]),
        ])

    # ---------------------------------------------------------- the run
    def _report(self, n: int) -> None:
        with self.lock:
            found = sum(1 for x in self.bench.edit["items"] if x["source"] == "agent")
            self.progress(f"Feinschnitt: {self.done} von {n} Abschnitten fertig, {found} Vorschläge bisher")

    def _section(self, sec: dict, n: int, instructions: str, tools: list[dict], extra: str = "") -> SectionAgent:
        """One section in a conversation of its own; extra goes at the end of its first message (the findings
        of the check on the rendered result, cut_review.py)."""
        client = self.clients("section_light" if sec["light"] else "section")
        agent = SectionAgent(self.bench, client, sec, self.lock, lambda text: self._report(n))
        with self.lock:
            first = self._section_message(sec, n) + ("\n\n" + extra if extra else "")
        t0 = time.monotonic()
        _, stopped = agent.converse(instructions, first, tools, stage=f"section {sec['no']}", ends=("section_done",))
        with self.lock:
            self.done += 1
            self.sections.append({"no": sec["no"], "start": round(sec["start"], 1), "end": round(sec["end"], 1),
                                  "light": sec["light"], "model": getattr(client, "model", ""), "rounds": agent.rounds,
                                  "images": agent.images, "seconds": round(time.monotonic() - t0, 1),
                                  "usage": agent.usage.as_dict(), **({"stopped": stopped} if stopped else {})})
        self._report(n)
        return agent

    def run(self) -> str:
        started = time.monotonic()
        b_ = self.bench
        b_.add_rules()
        plan = outline.load(b_.folder)
        plan = {} if plan.get("error") else plan
        self._chapters(plan)
        sections = plan_sections(plan, b_.duration, b_.segments)
        self._log(event="start", version=VERSION, sections=len(sections), workers=self.workers, folder=str(b_.folder))
        off = b_.kinds_off
        ui = b_.settings.get("ui_language", "de")
        language = {"de": "German", "en": "English"}.get(ui, ui)
        tools = [t for t in tools_for(off) if t["name"] not in SECTION_ONLY] + [SECTION_DONE]
        instructions = SECTION.replace("{ui_language}", language)
        self.progress(f"Feinschnitt: {len(sections)} Abschnitte, {min(self.workers, len(sections))} gleichzeitig …")
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            agents = list(pool.map(lambda sec: self._section(sec, len(sections), instructions, tools), sections))
        for sec, agent in zip(sections, agents):                  # in the order of the recording
            self.rounds += agent.rounds
            self.images += agent.images
            self.usage.add(agent.usage)
            self.notes.append({"section": f"{sec['no']} ({_clock(sec['start'])}–{_clock(sec['end'])})", "note": agent.note})
            self.checks += agent.checks
        self.sections.sort(key=lambda s: s["no"])
        before = (self.rounds, self.images, dict(self.usage.as_dict()), time.monotonic())
        self.progress("Abschluss: Titel, Clips und Zusammenfassung …")
        ended, stopped = self.converse(CLOSING.replace("{ui_language}", language), self._closing_message(plan),
                                       [t for t in tools_for(off) if t["name"] in CLOSING_TOOLS], stage="closing")
        closing = {"model": getattr(self.client, "model", ""), "rounds": self.rounds - before[0],
                   "images": self.images - before[1], "seconds": round(time.monotonic() - before[3], 1),
                   "usage": {k: v - before[2].get(k, 0) for k, v in self.usage.as_dict().items()},
                   **({"stopped": stopped} if stopped else {})}
        self.summary = self.summary or ended or stopped
        return self.finish(started, extra={"version": VERSION, "sections": self.sections, "closing": closing,
                                           "workers": self.workers})


def run(folder: Path, key, listener=None, settings: dict | None = None,
        progress: Callable[[str], None] | None = None, models: dict | None = None) -> dict:
    """Propose the cut of a session folder with version 2; returns the edit list. key: the OpenAI key, or a
    function giving the key of a service; models: the server's configuration of the stages (models.json),
    where "cut_workers" sets how many sections run at once."""
    keys = key if callable(key) else (lambda service: key if service == "openai" else None)
    configs = {stage: llm.stage_config(stage, models) for stage in ("section", "section_light", "closing")}
    for config in configs.values():
        llm.make(config, keys)                       # fails early without a key, before any section starts
    bench = Workbench(Path(folder), settings, listener)
    try:
        StagedCut(bench, lambda stage: llm.make(configs[stage], keys), progress,
                  int((models or {}).get("cut_workers") or WORKERS)).run()
        return bench.edit
    finally:
        bench.close()
