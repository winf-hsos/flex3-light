"""Step "summary": what the edited video teaches, on one A4 page in German and one in English.

Into the session folder:
  summary.pdf    two pages, German first, marked as made automatically
  summary.json   the texts, the model, and the render it was made from (signature)

The language model gets the chapters of the edited video and what is said in each of them
(edited.<language>.vtt, so nothing the lecturer cut reaches the summary) and returns both
languages at once in a fixed structure (prompts/summary.md). Typst sets the pages
(templates/summary.typ); each page takes the largest text size at which it fits.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from . import binaries, cut_agent, files, publish

HERE = Path(__file__).resolve().parent
PROMPT, TEMPLATE = HERE / "prompts" / "summary.md", HERE / "templates" / "summary.typ"
MODEL, EFFORT = cut_agent.MODEL, "medium"
NO_WINDOW = 0x08000000
LANGUAGES = ("de", "en")

_text = {"type": "string"}
_language = {"type": "object", "additionalProperties": False,
             "required": ["overview", "points", "terms", "notes", "chapters"],
             "properties": {"overview": _text, "points": {"type": "array", "items": _text},
                            "terms": {"type": "array", "items": {
                                "type": "object", "additionalProperties": False, "required": ["term", "explanation"],
                                "properties": {"term": _text, "explanation": _text}}},
                            "notes": {"type": "array", "items": _text},
                            "chapters": {"type": "array", "items": _text}}}
SCHEMA = {"type": "object", "additionalProperties": False, "required": list(LANGUAGES),
          "properties": {lang: _language for lang in LANGUAGES}}

LABELS = {
    "de": {"kind": "Zusammenfassung", "points": "Das Wichtigste", "terms": "Begriffe", "notes": "Hinweise",
           "chapters": "Kapitel im Video",
           "footer": "Automatisch aus der Aufzeichnung erstellt (Transkript und KI-Zusammenfassung, FLEX3 Light). "
                     "Maßgeblich ist, was in der Vorlesung gesagt wurde."},
    "en": {"kind": "Summary", "points": "Key points", "terms": "Terms", "notes": "Notes",
           "chapters": "Chapters in the video",
           "footer": "Generated automatically from the recording (transcript and AI summary, FLEX3 Light). "
                     "What was said in the lecture is authoritative."},
}
MONTHS = {"de": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
                 "November", "Dezember"],
          "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                 "November", "December"]}


def current(folder: Path) -> bool:
    """True if summary.pdf belongs to the video rendered last."""
    made = files.read_json(Path(folder) / "render.json").get("signature")
    return bool(made) and publish.locate(Path(folder), "summary.pdf") is not None and \
        files.read_json(Path(folder) / "summary.json").get("signature") == made


# ---------------------------------------------------------------- what the model reads

def _seconds(stamp: str) -> float:
    h, m, s = stamp.replace(",", ".").split(":") if stamp.count(":") == 2 else ("0", *stamp.split(":"))
    return int(h) * 3600 + int(m) * 60 + float(s)


def cues(vtt: str) -> list[tuple[float, str]]:
    """(start, text) per cue of a WebVTT file."""
    out = []
    for block in re.split(r"\n\s*\n", vtt.replace("\r\n", "\n")):
        lines = block.strip().split("\n")
        for i, line in enumerate(lines):
            m = re.match(r"([\d:.,]+)\s+-->\s+[\d:.,]+", line)
            if m:
                text = " ".join(x.strip() for x in lines[i + 1:] if x.strip())
                if text:
                    out.append((_seconds(m.group(1)), text))
                break
    return out


def clock(t: float) -> str:
    t = int(round(t))
    return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def material(chapters: list[dict], said: list[tuple[float, str]]) -> str:
    """The text the model gets: every chapter with its time, title and what was said in it."""
    chapters = sorted(chapters, key=lambda c: c["start"]) or [{"start": 0.0, "title": ""}]
    parts = []
    for k, c in enumerate(chapters):
        end = chapters[k + 1]["start"] if k + 1 < len(chapters) else float("inf")
        text = " ".join(t for s, t in said if (c["start"] <= s < end) or (k == 0 and s < c["start"]))
        head = f"## Chapter {k + 1} at {clock(c['start'])}: {c.get('title', '')}".rstrip(": ")
        parts.append("\n".join(x for x in (head, c.get("summary", ""), text) if x))
    return "\n\n".join(parts)


def ask(client, text: str) -> dict:
    """Both languages from the model, in the structure of SCHEMA."""
    response = client.create({
        "instructions": PROMPT.read_text(encoding="utf-8"),
        "input": [{"role": "user", "content": text}],
        "text": {"format": {"type": "json_schema", "name": "lecture_summary", "strict": True, "schema": SCHEMA}}})
    out = "".join(c.get("text", "") for o in response.get("output", []) if o.get("type") == "message"
                  for c in o.get("content", []) if c.get("type") == "output_text")
    try:
        return json.loads(out)
    except ValueError:
        raise cut_agent.AgentError("Das Sprachmodell hat keine Zusammenfassung geliefert.") from None


# ---------------------------------------------------------------- the pages

def _date(iso: str, lang: str) -> str:
    try:
        d = dt.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    return f"{d.day}. {MONTHS['de'][d.month - 1]} {d.year}" if lang == "de" else \
        f"{d.day} {MONTHS['en'][d.month - 1]} {d.year}"


def _length(seconds: float, lang: str) -> str:
    minutes = max(1, int(round(seconds / 60)))
    h, m = divmod(minutes, 60)
    if lang == "de":
        return f"Video {h} Std. {m} Min." if h else f"Video {m} Min."
    return f"video {h} h {m} min" if h else f"video {m} min"


def pages(meta: dict, title: str, duration: float, chapters: list[dict], texts: dict) -> dict:
    """The data for templates/summary.typ."""
    out = []
    for lang in LANGUAGES:
        t, labels = texts[lang], LABELS[lang]
        date = _date(meta.get("recorded_at", ""), lang)
        lead = (f"Vorlesung vom {date}" if lang == "de" else f"Lecture of {date}") if date else ""
        names = t.get("chapters", [])
        out.append({"lang": lang, "labels": labels, "footer": labels["footer"],
                    "module": meta.get("course_name", ""), "title": title,
                    "meta": ", ".join(x for x in (lead, _length(duration, lang)) if x),
                    "overview": t["overview"], "points": t["points"], "terms": t["terms"], "notes": t["notes"],
                    "chapters": [{"time": clock(c["start"]),
                                  "title": names[k] if k < len(names) and names[k] else c.get("title", "")}
                                 for k, c in enumerate(chapters)]})
    return {"pages": out}


def typeset(data: dict, out: Path, work: Path) -> list[dict]:
    """summary.typ with the data as PDF at out; returns per page the text size and whether it fits."""
    shutil.copy(TEMPLATE, work / "summary.typ")
    files.write_json(work / "summary-data.json", data)
    base = [binaries.typst(), "--root", str(work), "--input", "data=summary-data.json", str(work / "summary.typ")]
    run = lambda args: subprocess.run([base[0], *args], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    r = run(["compile", *base[1:], str(out)])
    if r.returncode != 0:
        raise RuntimeError("Typst: " + r.stderr[:600])
    q = run(["query", *base[1:], "<fit>", "--field", "value"])
    return json.loads(q.stdout) if q.returncode == 0 else []


def run(folder: Path, client, progress: Callable[[str], None] | None = None) -> dict:
    """Write summary.pdf and summary.json for the video rendered last; returns summary.json."""
    folder = Path(folder)
    say = progress or (lambda text: None)
    made = files.read_json(folder / "render.json")
    if not made:
        raise ValueError("Die Zusammenfassung braucht das geschnittene Video.")
    meta = files.read_json(folder / "session.json")
    e = files.read_json(folder / "edit.json")
    chapters = files.read_json(folder / "chapters.json").get("chapters", [])
    vtt = next((folder / f for f in made.get("files", []) if f.startswith("edited.") and f.endswith(".vtt")), None)
    said = cues(vtt.read_text(encoding="utf-8")) if vtt and vtt.exists() else []
    if not said:
        raise ValueError("Für die Zusammenfassung fehlt der Text des geschnittenen Videos (edited.*.vtt).")
    title = (((e.get("cards") or {}).get("title") or {}).get("title") or e.get("title")
             or meta.get("topic") or "")
    say("Die Zusammenfassung wird geschrieben …")
    texts = ask(client, material(chapters, said))
    say("Die Zusammenfassung wird gesetzt …")
    data = pages(meta, title, made.get("duration", 0.0), chapters, texts)
    work = Path(tempfile.mkdtemp(prefix="flex3-summary-"))
    try:
        part = work / "summary.pdf"
        fit = typeset(data, part, work)
        try:
            files.retry(lambda: os.replace(part, folder / "summary.pdf"), tries=5)
        except PermissionError:
            raise RuntimeError("summary.pdf ist gerade geöffnet. Schließ sie und versuch es erneut.") from None
    finally:
        shutil.rmtree(work, ignore_errors=True)
    report = {"created": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
              "signature": made.get("signature"), "model": getattr(client, "model", ""),
              "fit": fit, **texts}
    files.write_json(folder / "summary.json", report)
    return report
