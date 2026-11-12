"""The timeline of the cut: which spans of the recording stay, and where a time lands afterwards.

From the accepted items of the edit list: cuts take their span out; a long pause is taken
out down to a card "Pause entfernt" of CARD seconds at its start (the card covers those
seconds), shortened to about one second at either end (moved out of a word, edit.pause_inner),
or cut without a trace, as its mode says.
All bounds are put on frame boundaries, so that picture and sound lose exactly the same
time and stay in sync however many cuts there are.

map_time() turns a time of the recording into the time in the cut video, which is how
subtitles, chapters and clips follow the cut. Cards that add time come on top: a title
card at the start, a card for each chapter and a closing card (inserts()); shift() moves
a time of the cut video past every card inserted before it. How long the cards last, which
sounds they carry and whether an intro and an outro frame the video is the server's style
(style.py); without one, the lengths below.
"""
from __future__ import annotations

from . import edit, subtitles

CARD = 2.5                       # seconds a removed pause shows its card
SHORTENED = edit.SHORTENED       # seconds a shortened pause keeps at either end, out of a word (edit.pause_inner)
TITLE, CHAPTER, CLOSING = 4.0, 2.0, 4.0     # seconds of the inserted cards, unless the style says otherwise
ORDER = {"intro": 0, "title": 1, "chapter": 2, "closing": 3, "outro": 4}


def _frame(t: float, fps: float) -> float:
    return round(round(t * fps) / fps, 6)


def plan(e: dict, fps: float = 30.0) -> dict:
    """{"removed": [(a, b)], "cards": [(a, b, text)], "kept": [(a, b)], "duration": new length}, all
    in seconds of the recording except duration."""
    spans, cards = [], []
    for x in e["items"]:
        if x["state"] != "accepted" or x["kind"] not in edit.REMOVING:
            continue
        a, b = x["start"], x["end"]
        if x["kind"] == "pause":
            mode = x["params"].get("mode", "card")
            if mode == "card" and b - a > CARD + 0.5:
                minutes = (b - a) / 60
                text = f"Pause entfernt ({minutes:.0f} min)" if minutes >= 1.5 else "Pause entfernt"
                cards.append((_frame(a, fps), _frame(a + CARD, fps), text))
                a = a + CARD
            elif mode == "shorten" and b - a > 2 * SHORTENED + 0.5:
                a, b = edit.pause_inner(x)
        spans.append((_frame(a, fps), _frame(b, fps)))
    removed: list[list[float]] = []
    for a, b in sorted(spans):
        if b <= a:
            continue
        if removed and a <= removed[-1][1]:
            removed[-1][1] = max(removed[-1][1], b)
        else:
            removed.append([a, b])
    end = _frame(e["duration"], fps)
    kept, t = [], 0.0
    for a, b in removed:
        if a > t:
            kept.append((t, a))
        t = max(t, b)
    if t < end:
        kept.append((t, end))
    cards = [c for c in cards if not any(a <= c[0] < b for a, b in removed)]   # a card inside another cut is gone
    return {"removed": [(a, b) for a, b in removed], "cards": cards, "kept": kept,
            "duration": round(sum(b - a for a, b in kept), 3)}


def map_time(t: float, p: dict) -> float | None:
    """Where a time of the recording lands in the edited video; None if it was cut away."""
    offset = 0.0
    for a, b in p["kept"]:
        if t < a:
            return None
        if t <= b:
            return round(offset + t - a, 3)
        offset += b - a
    return None


def map_forward(t: float, p: dict) -> float:
    """Like map_time, but a time inside a cut moves to where the video goes on after it."""
    offset = 0.0
    for a, b in p["kept"]:
        if t <= b:
            return round(offset + max(0.0, t - a), 3)
        offset += b - a
    return round(offset, 3)


def inserts(e: dict, p: dict, style: dict | None = None) -> list[dict]:
    """The cards that add time, in order: {"at" (time in the cut video), "seconds", "kind", "title",
    "kicker", "footer", "sound"}, "sound" being the moment ("title", "chapter", "closing") whose sound
    the style names. They follow e["cards"], which the proposal fills from the settings and the
    lecturer may change, and the style: the length of each card, and an intro and outro video
    ("intro_seconds" and "outro_seconds", measured by render.py) before and after everything."""
    cfg = e.get("cards") or {}
    if not cfg:                                  # a list from before there were cards (approve_cut fills them in)
        return []
    length = (style or {}).get("cards") or {}
    out = []
    if (style or {}).get("intro_seconds"):
        out.append({"at": 0.0, "seconds": style["intro_seconds"], "kind": "intro", "title": "", "kicker": "",
                    "footer": "", "sound": None})
    title = cfg.get("title") or {}
    if title.get("on"):
        out.append({"at": 0.0, "seconds": length.get("title", TITLE), "kind": "title", "title": title.get("title", ""),
                    "kicker": title.get("kicker", ""), "footer": title.get("footer", ""),
                    "sound": "title" if cfg.get("music") else None})
    if cfg.get("chapters", "card") == "card":
        number = 0
        for x in sorted((x for x in e["items"] if x["kind"] == "chapter" and x["state"] == "accepted"),
                        key=lambda x: x["start"]):
            number += 1
            at = map_forward(x["start"], p)
            if (title.get("on") and at < 1.0) or any(abs(at - o["at"]) < 1.0 and o["kind"] == "chapter" for o in out):
                continue                         # right after the title card, or a second card on the same spot
            out.append({"at": at, "seconds": length.get("chapter", CHAPTER), "kind": "chapter",
                        "title": x["params"].get("title", ""), "kicker": f"Kapitel {number}", "footer": "",
                        "sound": "chapter" if cfg.get("chapter_sound", True) else None})
    closing = cfg.get("closing") or {}
    if closing.get("on"):
        out.append({"at": p["duration"], "seconds": length.get("closing", CLOSING), "kind": "closing",
                    "title": closing.get("title", ""), "kicker": closing.get("kicker", ""),
                    "footer": closing.get("footer", ""), "sound": "closing" if cfg.get("music") else None})
    if (style or {}).get("outro_seconds"):
        out.append({"at": p["duration"], "seconds": style["outro_seconds"], "kind": "outro", "title": "", "kicker": "",
                    "footer": "", "sound": None})
    return sorted(out, key=lambda o: (o["at"], ORDER[o["kind"]]))


def shift(t: float, ins: list[dict] | None) -> float:
    """A time of the cut video in the final video: past every card inserted at or before it."""
    return round(t + sum(o["seconds"] for o in ins or [] if o["at"] <= t + 1e-6), 3)


def card_starts(ins: list[dict]) -> list[float]:
    """Where each insert begins in the final video."""
    out, before = [], 0.0
    for o in ins:
        out.append(round(o["at"] + before, 3))
        before += o["seconds"]
    return out


def words(transcript: dict, p: dict, start: float = 0.0, end: float | None = None,
          ins: list[dict] | None = None) -> list[dict]:
    """The words that remain, with times in the final video, optionally only those in [start, end)
    of it and then relative to start (for a clip)."""
    out = []
    for w in transcript.get("words", []):
        a, b = map_time(w["start"], p), map_time(w["end"], p)
        if a is None or b is None or b < a:
            continue
        a, b = shift(a, ins), shift(b, ins)
        if a < start or (end is not None and b > end):
            continue
        out.append({**w, "start": round(a - start, 3), "end": round(b - start, 3)})
    return out


def vtt(timed_words: list[dict]) -> str:
    body = ["WEBVTT", ""]
    for a, b, text in subtitles.cues([w for w in timed_words if w["word"].strip()]):
        body += [f"{subtitles._time(a)} --> {subtitles._time(b)}", text, ""]
    return "\n".join(body)


def chapters(e: dict, p: dict, ins: list[dict] | None = None) -> list[dict]:
    """Accepted chapters in the final video, starting with their card where there is one; the first at
    0:00, none twice at the same time."""
    ins = ins or []
    starts = card_starts(ins)
    out = []
    for x in sorted((x for x in e["items"] if x["kind"] == "chapter" and x["state"] == "accepted"),
                    key=lambda x: x["start"]):
        cut_t = map_forward(x["start"], p)
        card_at = next((s for o, s in zip(ins, starts) if o["kind"] == "chapter" and abs(o["at"] - cut_t) < 1e-6),
                       None)
        t = card_at if card_at is not None else shift(cut_t, ins)
        if out and t - out[-1]["start"] < 1.0:
            continue
        out.append({"start": t, "source_start": x["start"], "title": x["params"].get("title", ""),
                    "summary": x["params"].get("summary", "")})
    if out:
        out[0]["start"] = 0.0
    return out


def youtube_chapters(chs: list[dict]) -> str:
    """The lines YouTube turns into chapters in a description: 0:00 Title, one per line."""
    def clock(t: float) -> str:
        t = int(t)
        return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"
    return "\n".join(f"{clock(c['start'])} {c['title']}" for c in chs)
