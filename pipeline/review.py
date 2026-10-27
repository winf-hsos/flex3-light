"""Review of a transcript: the words the service was unsure about, and their correction.

AssemblyAI rates every word from 0 to 1 (transcript.json keeps it as "confidence").
Most unsure words are short function words at restarts, and nobody wants to look at
all of them, so a word is offered when it is unsure and either longer than three
letters (names and terms are) or very unsure. Longer words come first.

A correction changes the word in transcript.json, rebuilds the sentence around it
and writes the subtitles again; transcript.raw.json stays as the service returned it.
A reviewed word is marked "checked" and is not offered again.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import subtitles

UNSURE = 0.6          # below this a word is worth a look ...
VERY_UNSURE = 0.5     # ... and below this even a short one
CONTEXT = 6           # words on either side


def _letters(word: str) -> int:
    return sum(ch.isalpha() for ch in word)


def _load(session_dir: Path) -> dict:
    return json.loads((Path(session_dir) / "transcript.json").read_text(encoding="utf-8"))


def _save(session_dir: Path, transcript: dict) -> None:
    path = Path(session_dir) / "transcript.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(transcript, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def uncertain(transcript: dict) -> list[dict]:
    """The spots to offer, most promising first: index, word, times, confidence and the words around."""
    words = transcript.get("words", [])
    spots = []
    for i, w in enumerate(words):
        c = w.get("confidence")
        if c is None or w.get("checked") or c >= UNSURE:
            continue
        if _letters(w["word"]) <= 3 and c >= VERY_UNSURE:
            continue
        spots.append({"index": i, "word": w["word"], "start": w["start"], "end": w["end"], "confidence": c,
                      "before": " ".join(x["word"] for x in words[max(0, i - CONTEXT):i]),
                      "after": " ".join(x["word"] for x in words[i + 1:i + 1 + CONTEXT])})
    return sorted(spots, key=lambda s: (_letters(s["word"]) <= 3, s["confidence"]))


def _punctuated(old: str, new: str) -> str:
    """Keep the punctuation around the old word unless the new one brings its own."""
    lead, core, trail = re.match(r"^(\W*)(.*?)(\W*)$", old).groups()
    new = new.strip()
    if not re.search(r"\W$", new):
        new += trail
    if not re.match(r"^\W", new):
        new = lead + new
    return new


def _rebuild(transcript: dict, index: int) -> None:
    """The sentence holding the word and the full text, from the words again."""
    words, w = transcript["words"], transcript["words"][index]
    for s in transcript.get("segments", []):
        if s["start"] - 0.05 <= w["start"] <= s["end"] + 0.05:
            s["text"] = " ".join(x["word"] for x in words if s["start"] - 0.05 <= x["start"] <= s["end"] + 0.05)
            break
    transcript["text"] = " ".join(x["word"] for x in words)


def correct(session_dir: Path, index: int, text: str) -> dict:
    """Replace one word (it may become several) and write new subtitles; returns the changed word."""
    transcript = _load(session_dir)
    w = transcript["words"][index]
    new = _punctuated(w["word"], text)
    if not new.strip(" .,;:!?"):
        raise ValueError("Das Wort darf nicht leer sein.")
    if new != w["word"]:
        w.setdefault("corrected_from", w["word"])
        w["word"] = new
    w["checked"] = True
    _rebuild(transcript, index)
    _save(session_dir, transcript)
    subtitles.run(session_dir)
    return w


def confirm(session_dir: Path, index: int) -> dict:
    """The word is right as it is."""
    transcript = _load(session_dir)
    transcript["words"][index]["checked"] = True
    _save(session_dir, transcript)
    return transcript["words"][index]


def term(word: str) -> str:
    """The form of a word that goes into the module's terms: without punctuation around it."""
    return re.sub(r"^[^\w]+|[^\w]+$", "", word.strip())
