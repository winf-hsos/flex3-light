"""The cut agent: a language model with tools that writes the edit list of a recording.

It reads the transcript, looks at frames and contact sheets, listens to spans and
decides cuts, pauses, chapters and the rest (cut_tools.py, edit.py): the lecturer gets a
finished proposal with a short summary and changes only what he wants to. It never
renders; the lecturer approves the list first. Every tool call is logged in edit.log.jsonl.

The model is OpenAI gpt-6.1-sol with reasoning effort "high" over the Responses API
(docs/pipeline.md, 2026-10-28). The conversation is continued with previous_response_id,
so that pictures already seen are not sent again.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from . import edit
from .cut_tools import Workbench, as_json, summary_line

MODEL, EFFORT = "gpt-6.1-sol", "high"
URL = "https://api.openai.com/v1/responses"
MAX_ROUNDS, MAX_IMAGES = 60, 80

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

Write every reason, title, description and chapter title in the language of the lecture, short and factual, as the
lecturer reads them while approving. Times are seconds of the recording. Boundaries of spans are moved out of words
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


class AgentError(RuntimeError):
    pass


class OpenAIResponses:
    def __init__(self, key: str, model: str = MODEL, effort: str = EFFORT, timeout: float = 600):
        self._key, self.model, self.effort, self.timeout = key, model, effort, timeout

    def __repr__(self) -> str:
        return f"OpenAIResponses({self.model})"   # never the key

    def create(self, body: dict) -> dict:
        body = {"model": self.model, "reasoning": {"effort": self.effort}, **body}
        req = urllib.request.Request(URL, data=json.dumps(body).encode("utf-8"), method="POST", headers={
            "Authorization": f"Bearer {self._key}", "Content-Type": "application/json"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:500]
                if e.code in (429, 500, 502, 503) and attempt < 2:
                    time.sleep(10 * (attempt + 1))
                    continue
                raise AgentError(f"OpenAI antwortet mit Fehler {e.code}: {detail}") from None
        raise AgentError("OpenAI antwortet nicht.")


class CutAgent:
    def __init__(self, bench: Workbench, client, progress: Callable[[str], None] | None = None,
                 max_rounds: int = MAX_ROUNDS, max_images: int = MAX_IMAGES):
        self.bench, self.client, self.progress = bench, client, progress or (lambda text: None)
        self.max_rounds, self.max_images = max_rounds, max_images
        self.images, self.usage, self.summary = 0, {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}, ""
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
            "Transcript by sentence, [clock start-end in seconds]:\n" + self.bench.sentences(),
        ])

    def _image(self, data: bytes, label: str) -> dict:
        self.images += 1
        return {"role": "user", "content": [
            {"type": "input_text", "text": label},
            {"type": "input_image", "image_url": "data:image/jpeg;base64," + base64.b64encode(data).decode(),
             "detail": "high"}]}

    def _call(self, name: str, args: dict) -> tuple[str, dict | None]:
        """Run one tool; returns its text output and, for pictures, a message carrying the picture."""
        b = self.bench
        if name in ("get_frame", "get_contact_sheet", "check_region"):
            if self.images >= self.max_images:
                return "Bildbudget erschöpft; arbeite mit dem, was du gesehen hast.", None
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
            return f"{label}: the picture follows in the next message.", self._image(data, label)
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
        self._log(event="start", model=getattr(self.client, "model", ""), folder=str(self.bench.folder))
        body = {"instructions": INSTRUCTIONS, "tools": TOOLS, "input": [
            {"role": "user", "content": [{"type": "input_text", "text": self._first_message()}]}]}
        for round_ in range(1, self.max_rounds + 1):
            response = self.client.create(body)
            u = response.get("usage") or {}
            self.usage["input_tokens"] += u.get("input_tokens", 0)
            self.usage["output_tokens"] += u.get("output_tokens", 0)
            self.usage["reasoning_tokens"] += (u.get("output_tokens_details") or {}).get("reasoning_tokens", 0)
            calls = [o for o in response.get("output", []) if o.get("type") == "function_call"]
            text = " ".join(c.get("text", "") for o in response.get("output", []) if o.get("type") == "message"
                            for c in o.get("content", []) if c.get("type") == "output_text").strip()
            if text:
                self._log(event="message", text=text[:2000])
            if not calls:
                self.summary = self.summary or text
                break
            outputs, pictures = [], []
            for c in calls:
                try:
                    args = json.loads(c.get("arguments") or "{}")
                    out, picture = self._call(c["name"], args)
                except Exception as e:                     # the model gets the error and can correct itself
                    args, out, picture = {}, f"error: {e}", None
                self._log(event="tool", round=round_, name=c["name"], args=args, result=out[:600])
                found = sum(1 for x in self.bench.edit["items"] if x["source"] == "agent")
                self.progress(f"Der Schnitt-Agent {DOING.get(c['name'], 'arbeitet')}"
                              + (f", {found} Vorschläge bisher" if found else ""))
                outputs.append({"type": "function_call_output", "call_id": c["call_id"], "output": out[:60000]})
                if picture:
                    pictures.append(picture)
            if self.summary and any(c["name"] == "finish" for c in calls):
                break
            body = {"previous_response_id": response["id"], "tools": TOOLS, "instructions": INSTRUCTIONS,
                    "input": outputs + pictures}
        else:
            self.summary = self.summary or "Rundenbudget erschöpft; die Liste ist unvollständig."
        decided_here = self.bench.decide_rest()            # nothing stays open for the lecturer
        self.bench.edit["agent"] = {"model": getattr(self.client, "model", ""), "summary": self.summary,
                                    "decided_by_fallback": decided_here,
                                    "images": self.images, "usage": self.usage,
                                    "finished": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
        self.bench.save()
        self._log(event="finish", summary=self.summary, usage=self.usage, images=self.images)
        return self.summary


def run(folder: Path, openai_key: str, listener=None, settings: dict | None = None,
        progress: Callable[[str], None] | None = None, model: str = MODEL, effort: str = EFFORT) -> dict:
    """Propose the cut of a session folder; returns the edit list."""
    bench = Workbench(Path(folder), settings, listener)
    try:
        CutAgent(bench, OpenAIResponses(openai_key, model, effort), progress).run()
        return bench.edit
    finally:
        bench.close()
