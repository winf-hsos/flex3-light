"""The cut agent: a language model with tools that writes the edit list of a recording.

It reads the transcript, looks at frames and contact sheets, listens to spans and
decides cuts, pauses, chapters and the rest (cut_tools.py, edit.py): the lecturer gets a
finished proposal with a short summary and changes only what he wants to. It never
renders; the lecturer approves the list first. Every tool call is logged in edit.log.jsonl.

The model sits behind pipeline/llm.py (since 2026-11-10): the stage "cut" of the server's
configuration says which adapter and model, by default OpenAI gpt-6.1-sol with reasoning
effort "high" over the Responses API. There is no budget of rounds or pictures (decision of
2026-11-10); every run measures its rounds, pictures, tokens and time instead, and only an
emergency stop ends a loop that goes nowhere. When the agent is done, its list is kept as
edit.agent.json, so what the lecturer changed before approving can be compared with it.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import time
from pathlib import Path
from typing import Callable

from . import edit, llm, locks
from .cut_tools import Workbench, as_json, summary_line

MODEL, EFFORT = llm.DEFAULTS["cut"]["model"], llm.DEFAULTS["cut"]["effort"]
SAFETY_ROUNDS = 250        # not a budget: only against an agent that never finishes
SAME_ROUNDS = 4            # rounds with exactly the same calls in a row count as stuck
AGENT_COPY = "edit.agent.json"
OpenAIResponses, AgentError = llm.OpenAIResponses, llm.ModelError     # older names, used by the summary

INSTRUCTIONS = """You are the editor of lecture recordings at a university of applied sciences. You prepare the
cut of one recording: a list of proposals the lecturer approves before anything is rendered.

Goal: a long version that is pleasant to watch and loses nothing of the teaching. Remove dead time (silence at the
edges, microphone checks, setting up, waiting, long pauses during exercises, technical trouble), passages that are
explicitly not for the recording, organisational talk that only concerned the people in the room, and anything
private on screen or in sound (mail, chats, file names with personal data, voices and names of students). Do not
remove false starts, repetitions or slow passages of the teaching itself: a lecture cut too tightly feels choppy and
loses trust. When unsure, propose with low confidence instead of leaving it out; the lecturer decides.

Also propose: chapters with short titles (the first at 0:00, at least three, each at least ten seconds; slide titles
are good chapter titles), a title and a description of the recording, clips (passages that stand on their own as
short videos), zooms where screen content is too small to read, the camera layout where it matters, and a thumbnail.

Work from coarse to fine: read the transcript and the rule proposals, get the structure and chapters, then check
edges and pauses, then privacy (programs in front, what is on screen, quiet voices from the room: compare loudness
and listen), then polish. Look before you judge: use contact sheets to scan long spans and single frames for detail.
Never place a blur or a zoom by eye: find the text with locate_text (an empty query lists everything readable, which
is also how you scan a suspicious frame for names, mail addresses or student numbers), take its region, and look at it
with check_region before you propose it. Where the content moves during the span (scrolling, a window moved), check
the region at the start, middle and end and cover all of it, or split the span.
You decide. The lecturer wants a finished proposal and changes only what he disagrees with, so leave nothing open:
decide every rule proposal with assess_rule ("agree" accepts it, "disagree" rejects it, with a short reason), and
everything you propose is accepted unless you set state "rejected" to show an alternative you weighed. Where a rule
proposal is right in substance but wrong in its bounds (it cuts through a sentence, it misses part of what is on
screen), disagree and propose a better item with replaces set to the rule proposals it stands in for. Give every
cut, pause, mute, freeze and blur a category: dead_time (silence, setting up, waiting), pause (exercises, breaks),
technical (microphone checks, trouble with devices or software), off_record (said not to be for the recording),
organisation (only for the people in the room), privacy (mail, chats, names, voices from the room) or other.

At the end, before finish, call set_summary, always in the same shape: for every category you cut, one sentence on
what kind of passages these are and why they go (classes, not single cuts; the counts and times are added for you);
"check": at most five items where your decision was close and a look is worth it, with why; "notes": at most three
short remarks that matter to the lecturer, such as a tip for the next recording (a notification popped up, the
microphone was too quiet at the start). Clips and chapters are listed from the items by themselves.

The title of the recording and every chapter title also appear on cards in the video (a title card at the start, a
card at each chapter): keep chapter titles to about five words, and the title to one line of about eight words.

Privacy locks: before you start, fixed rules have searched every frame and the whole sound (privacy.json). Each lock
is a span where something private is visible or audible: a mail address, a key, a student number, a voice from the
room, a span the lecturer marked with the privacy key, a mailbox in front. Locks are hard: the video is not rendered
while one stays visible or audible. Hide every lock: mute for a voice (unless the span goes anyway), blur over its
region for text in the picture (check the region first; the lock's region is from the text recognition), a cut where
the whole picture is private. Check the lock before you decide how, but you cannot lift it: if you are sure a lock is a
false alarm (the lecturer's own address on a slide, a voice that is the lecturer's), leave it and say so in "notes";
only the lecturer lifts a lock. Candidates are weaker hints: look at them and decide.

Write every title, description and chapter title in the language of the lecture, short and factual. Write reasons,
the summary sentences, "check", "notes" and the final summary in the language of the interface, as the lecturer reads
them while approving: {ui_language}. Times are seconds of the recording. Boundaries of spans are moved out of words
automatically. Call check_edit before you finish, fix what it reports, then call finish with a short summary."""


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required,
                           "additionalProperties": False}}


T = {"type": "number"}
S = {"type": "string"}
TRACK = {"type": "string", "enum": ["program", "screen", "camera"]}
TOOLS = [
    _tool("get_words", "Words with start time, confidence and, if known, speaker, between two times.",
          {"start": T, "end": T}, ["start", "end"]),
    _tool("search_transcript", "Sentences containing a text (case-insensitive).", {"query": S}, ["query"]),
    _tool("get_frame", "One frame of a track at a time, as a picture.",
          {"t": T, "track": TRACK, "width": {"type": "integer", "description": "pixels, 320 to 1920"}},
          ["t", "track", "width"]),
    _tool("get_contact_sheet", "A grid of small frames with timestamps, evenly spread over a span; to scan long "
          "spans quickly.", {"start": T, "end": T, "count": {"type": "integer", "description": "2 to 30"},
                             "track": TRACK}, ["start", "end", "count", "track"]),
    _tool("locate_text", "Where a text is in a frame, by the computer's own text recognition: exact regions "
          "[x, y, w, h] as fractions of the frame, ready for blur or zoom. An empty query lists all text in the frame "
          "with its region, to scan a picture for names, mail addresses or other personal data.",
          {"t": T, "query": S, "track": TRACK}, ["t", "query", "track"]),
    _tool("check_region", "A region of a frame enlarged, with its outline drawn in red and some surroundings, to "
          "verify that a blur covers everything or a zoom frames the right content before proposing it.",
          {"t": T, "region": {"type": "array", "items": T, "minItems": 4, "maxItems": 4}, "track": TRACK},
          ["t", "region", "track"]),
    _tool("get_scene_changes", "Times of clear picture changes (new slide, other program) in a span.",
          {"start": T, "end": T, "track": TRACK}, ["start", "end", "track"]),
    _tool("get_loudness", "Loudness in dBFS per second over a span, with the lecturer's usual level.",
          {"start": T, "end": T}, ["start", "end"]),
    _tool("listen", "Listen to a span (at most 10 minutes): loudness-normalised and transcribed again with "
          "speaker labels, so quiet voices from the room become visible.", {"start": T, "end": T},
          ["start", "end"]),
    _tool("get_activity", "Programs and window titles in front, and privacy-key spans, in a span.",
          {"start": T, "end": T}, ["start", "end"]),
    _tool("propose", "Add a proposal to the edit list. Kinds: " + ", ".join(edit.KINDS) + ". chapter and thumbnail "
          "take only start (end null). params by kind: pause {mode: card|shorten|cut}, blur/zoom {region: [x, y, w, h] "
          "as fractions of the frame}, layout {camera: big|small|hidden}, chapter/clip {title, summary}, lower_third "
          "{text}, thumbnail {track}.",
          {"kind": {"type": "string", "enum": list(edit.KINDS)}, "start": T, "end": {"type": ["number", "null"]},
           "reason": S, "confidence": {"type": "number", "description": "0 to 1"},
           "params": {"type": "object", "description": "kind-specific, see above"},
           "replaces": {"type": "array", "items": S, "description": "ids of rule proposals this one stands in for"},
           "category": {"type": ["string", "null"], "enum": list(edit.CATEGORIES) + [None],
                        "description": "why it goes; for cut, pause, mute, freeze and blur"},
           "state": {"type": "string", "enum": ["accepted", "rejected"],
                     "description": "accepted, or rejected for an alternative you weighed and decided against"}},
          ["kind", "start", "end", "reason", "confidence", "params"]),
    _tool("assess_rule", "Decide a rule's proposal: agree accepts it, disagree rejects it. Its own reason stays; "
          "yours is shown beside it.",
          {"id": S, "verdict": {"type": "string", "enum": list(edit.VERDICTS)}, "reason": S},
          ["id", "verdict", "reason"]),
    _tool("change_item", "Change one of your own proposals: bounds, reason, confidence, category or state.",
          {"id": S, "start": {"type": ["number", "null"]}, "end": {"type": ["number", "null"]},
           "reason": {"type": ["string", "null"]}, "confidence": {"type": ["number", "null"]},
           "category": {"type": ["string", "null"], "enum": list(edit.CATEGORIES) + [None]},
           "state": {"type": ["string", "null"], "enum": ["accepted", "rejected", None]}},
          ["id", "start", "end", "reason", "confidence"]),
    _tool("set_summary", "The summary for the lecturer, always in the same shape (see instructions).",
          {"cuts": {"type": "array", "items": {"type": "object", "properties": {
              "category": {"type": "string", "enum": list(edit.CATEGORIES)}, "text": S},
              "required": ["category", "text"]}},
           "check": {"type": "array", "items": {"type": "object", "properties": {"id": S, "why": S},
                                                "required": ["id", "why"]}},
           "notes": {"type": "array", "items": S}},
          ["cuts", "check", "notes"]),
    _tool("withdraw_item", "Remove one of your own proposals.", {"id": S}, ["id"]),
    _tool("list_items", "The edit list as it stands.", {}, []),
    _tool("set_metadata", "Title and description of the recording for the video platforms.",
          {"title": S, "description": S}, ["title", "description"]),
    _tool("check_edit", "Problems the edit list would have when rendered.", {}, []),
    _tool("finish", "End the work with a short summary for the lecturer.", {"summary": S}, ["summary"]),
]
for t in TOOLS:                                   # loose schemas: params is free-form
    t["strict"] = False


def tools_for(kinds_off: set[str]) -> list[dict]:
    """The tools with the kinds the lecturer switched off taken out of propose, so they are not even offered."""
    if not kinds_off:
        return TOOLS
    out = []
    for t in TOOLS:
        if t["name"] == "propose":
            t = json.loads(json.dumps(t))
            kinds = t["parameters"]["properties"]["kind"]
            kinds["enum"] = [k for k in kinds["enum"] if k not in kinds_off]
            t["description"] += " Not available here (switched off by the lecturer): " + ", ".join(sorted(kinds_off)) + "."
        out.append(t)
    return out


LANGUAGES = {"de": "German", "en": "English"}


def instructions_for(kinds_off: set[str], ui_language: str = "de") -> str:
    """The instructions, with what the lecturer switched off and the language of the interface."""
    text = INSTRUCTIONS.replace("{ui_language}", LANGUAGES.get(ui_language, ui_language))
    if not kinds_off:
        return text
    names = ", ".join(sorted(kinds_off))
    return (text + f"\n\nThe lecturer has switched off: {names}. Do not propose these and do not work around "
            "them with other kinds; where one of them would have been the right answer (say, a pixelation for a mail on "
            "screen), mention it among the items worth a look or in the notes, so the lecturer can decide.")


# what the window says the agent is doing, by tool
DOING = {"get_words": "liest die Transkription", "search_transcript": "sucht in der Transkription",
         "get_frame": "sieht sich ein Bild an", "get_contact_sheet": "sieht sich eine Strecke in Bildern an",
         "get_scene_changes": "sucht Folienwechsel", "locate_text": "sucht Text im Bild",
         "check_region": "prüft einen Bildausschnitt", "get_loudness": "misst die Lautstärke",
         "listen": "hört genau hin", "get_activity": "prüft die Programme im Vordergrund",
         "propose": "schreibt Vorschläge", "assess_rule": "entscheidet über die Regel-Vorschläge",
         "set_summary": "fasst den Schnitt zusammen",
         "change_item": "überarbeitet einen Vorschlag",
         "withdraw_item": "verwirft einen eigenen Vorschlag", "list_items": "geht die Liste durch",
         "set_metadata": "schreibt Titel und Beschreibung", "check_edit": "prüft die Liste",
         "finish": "ist fertig"}


class CutAgent:
    def __init__(self, bench: Workbench, client, progress: Callable[[str], None] | None = None,
                 safety_rounds: int = SAFETY_ROUNDS):
        self.bench, self.client, self.progress = bench, client, progress or (lambda text: None)
        self.safety_rounds = safety_rounds
        self.images, self.rounds, self.usage, self.summary = 0, 0, llm.Usage(), ""
        self.log_path = bench.folder / "edit.log.jsonl"

    def _log(self, **entry) -> None:
        entry["at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _first_message(self) -> str:
        rules = self.bench.add_rules()
        return "\n\n".join([
            "Recording: " + as_json(self.bench.overview()),
            "Rule proposals already in the edit list (check them):\n" + "\n".join(summary_line(x) for x in rules)
            if rules else "No rule proposals.",
            self._privacy(),
            "Transcript by sentence, [clock start-end in seconds]:\n" + self.bench.sentences(),
        ])

    def _privacy(self) -> str:
        """The locks and candidates of the safety net, and what it could not check."""
        found = locks.load(self.bench.folder)
        checked = found.get("checked") or {}
        if not checked:
            return "The safety net has not looked at this recording; check privacy yourself."
        head = ("Privacy locks (hide every one):\n" + "\n".join(locks.lines(self.bench.folder, self.bench.edit))
                if found.get("locks") else "No privacy locks.")
        cands = found.get("candidates", [])
        tail = ("\nCandidates (look and decide):\n" + "\n".join(
            f"{x['id']} {x['kind']} ({x['sense']}) {x['start']:.1f}-{x['end']:.1f}"
            + (f" region {x['region']}" if x.get("region") else "") + (f" {x['text']}" if x.get("text") else "")
            for x in cands[:80])) if cands else ""
        gaps = [note for key, note in (("picture", "the picture was not read"), ("sound", "the sound was not listened to"))
                if checked.get(key) is False]
        return head + tail + ("\nNot checked by the safety net: " + "; ".join(gaps) + ". Check that part yourself."
                              if gaps else "")

    def _call(self, name: str, args: dict) -> tuple[str, llm.Image | None]:
        """Run one tool; returns its text output and, for pictures, the picture."""
        b = self.bench
        if name in ("get_frame", "get_contact_sheet", "check_region"):
            if name == "get_frame":
                data = b.frame(float(args["t"]), args.get("track", "program"), int(args.get("width") or 1280))
                label = f"Frame {args.get('track', 'program')} at {args['t']} s"
            elif name == "check_region":
                data = b.check_region(float(args["t"]), args["region"], args.get("track", "program"))
                label = f"Region {args['region']} at {args['t']} s, outlined in red"
            else:
                data = b.contact_sheet(float(args["start"]), float(args["end"]), int(args.get("count") or 12),
                                       args.get("track", "program"))
                label = f"Contact sheet {args.get('track', 'program')} {args['start']}–{args['end']} s"
            self.images += 1
            return f"{label}: the picture follows in the next message.", llm.Image(data, label)
        if name == "locate_text":
            return as_json(b.locate_text(float(args["t"]), args.get("query", ""), args.get("track", "program"))), None
        if name == "get_words":
            return as_json(b.get_words(float(args["start"]), float(args["end"]))), None
        if name == "search_transcript":
            return as_json(b.search(args["query"])), None
        if name == "get_scene_changes":
            return as_json(b.scene_changes(float(args["start"]), float(args["end"]), args.get("track", "program"))), None
        if name == "get_loudness":
            return as_json(b.loudness(float(args["start"]), float(args["end"]))), None
        if name == "listen":
            return as_json(b.listen(float(args["start"]), float(args["end"]))), None
        if name == "get_activity":
            return as_json(b.get_activity(float(args["start"]), float(args["end"]))), None
        if name == "propose":
            end = args.get("end")
            entry = b.propose(args["kind"], float(args["start"]), None if end is None else float(end),
                              args["reason"], float(args.get("confidence", 0.5)), args.get("replaces"),
                              args.get("category"), args.get("state") or "accepted", **(args.get("params") or {}))
            return "added " + summary_line(entry), None
        if name == "assess_rule":
            return "noted " + summary_line(b.assess(args["id"], args["verdict"], args["reason"])), None
        if name == "change_item":
            fields = {k: args.get(k) for k in ("start", "end", "reason", "confidence", "category", "state")}
            return "changed " + summary_line(b.change(args["id"], **fields)), None
        if name == "withdraw_item":
            b.withdraw(args["id"])
            return "withdrawn", None
        if name == "list_items":
            return "\n".join(summary_line(x) for x in b.edit["items"]) or "empty", None
        if name == "set_metadata":
            b.set_metadata(args.get("title"), args.get("description"))
            return "saved", None
        if name == "set_summary":
            b.set_summary(args.get("cuts") or [], args.get("check"), args.get("notes"))
            return "saved", None
        if name == "check_edit":
            return as_json(b.check() or ["no problems"]), None
        if name == "finish":
            self.summary = args.get("summary", "")
            return "done", None
        raise ValueError(f"unknown tool {name}")

    def run(self) -> str:
        started = time.monotonic()
        self._log(event="start", model=getattr(self.client, "model", ""), adapter=type(self.client).__name__,
                  folder=str(self.bench.folder))
        off = self.bench.kinds_off
        tools = [llm.Tool(t["name"], t["description"], t["parameters"]) for t in tools_for(off)]
        instructions = instructions_for(off, self.bench.settings.get("ui_language", "de"))
        messages = [llm.Message("user", [llm.Text(self._first_message())])]
        last_calls, same, stopped = None, 0, ""
        while True:
            if self.rounds >= self.safety_rounds:
                stopped = f"Notstopp nach {self.rounds} Runden; die Liste ist unvollständig."
                break
            self.rounds += 1
            t0 = time.monotonic()
            reply = self.client.complete(instructions, messages, tools)
            self.usage.add(reply.usage)
            self._log(event="round", round=self.rounds, seconds=round(time.monotonic() - t0, 1),
                      usage=reply.usage.as_dict(), calls=len(reply.calls))
            if reply.text:
                self._log(event="message", text=reply.text[:2000])
            if not reply.calls:
                self.summary = self.summary or reply.text
                break
            messages.append(reply.message())
            results = []
            for c in reply.calls:
                try:
                    out, picture = self._call(c.name, c.arguments)
                except Exception as e:                     # the model gets the error and can correct itself
                    out, picture = f"error: {e}", None
                self._log(event="tool", round=self.rounds, name=c.name, args=c.arguments, result=out[:600])
                found = sum(1 for x in self.bench.edit["items"] if x["source"] == "agent")
                self.progress(f"Der Schnitt-Agent {DOING.get(c.name, 'arbeitet')}"
                              + (f", {found} Vorschläge bisher" if found else ""))
                results.append(llm.ToolResult(c.id, out[:60000], [picture] if picture else []))
            messages.append(llm.Message("user", results))
            if self.summary and any(c.name == "finish" for c in reply.calls):
                break
            signature = json.dumps([(c.name, c.arguments) for c in reply.calls], sort_keys=True, default=str)
            same = same + 1 if signature == last_calls else 1
            last_calls = signature
            if same >= SAME_ROUNDS:
                stopped = f"Notstopp: {SAME_ROUNDS} Runden mit denselben Aufrufen; die Liste ist unvollständig."
                break
        if stopped:
            self.summary = self.summary or stopped
            self._log(event="stopped", reason=stopped)
        seconds = round(time.monotonic() - started, 1)
        decided_here = self.bench.decide_rest()            # nothing stays open for the lecturer
        if "cards" not in self.bench.edit:                  # the texts of the added cards, to change before approving
            self.bench.edit["cards"] = edit.default_cards(self.bench.edit, self.bench.meta, self.bench.settings)
        self.bench.edit["agent"] = {"model": getattr(self.client, "model", ""), "adapter": type(self.client).__name__,
                                    "summary": self.summary, "decided_by_fallback": decided_here,
                                    "rounds": self.rounds, "images": self.images, "seconds": seconds,
                                    "usage": self.usage.as_dict(),
                                    "finished": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
        self.bench.save()
        shutil.copyfile(self.bench.folder / "edit.json", self.bench.folder / AGENT_COPY)   # to compare with the approval
        self._log(event="finish", summary=self.summary, usage=self.usage.as_dict(), images=self.images,
                  rounds=self.rounds, seconds=seconds)
        return self.summary


def run(folder: Path, key, listener=None, settings: dict | None = None,
        progress: Callable[[str], None] | None = None, config: dict | None = None) -> dict:
    """Propose the cut of a session folder; returns the edit list. key: the OpenAI key, or a function
    giving the key of a service; config: the model of the stage (llm.stage_config("cut", ...))."""
    keys = key if callable(key) else (lambda service: key if service == "openai" else None)
    client = llm.make(config or llm.stage_config("cut"), keys)
    bench = Workbench(Path(folder), settings, listener)
    try:
        CutAgent(bench, client, progress).run()
        return bench.edit
    finally:
        bench.close()
