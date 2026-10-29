"""The timeline of the cut: which spans of the recording stay, and where a time lands afterwards.

From the accepted items of the edit list: cuts take their span out; a long pause is taken
out down to a card "Pause entfernt" of CARD seconds at its start (the card covers those
seconds), shortened to one second at either end, or cut without a trace, as its mode says.
All bounds are put on frame boundaries, so that picture and sound lose exactly the same
time and stay in sync however many cuts there are.

map_time() turns a time of the recording into the time in the edited video, which is how
subtitles, chapters and clips follow the cut.
"""
from __future__ import annotations

from . import edit, subtitles

CARD = 2.5                       # seconds a removed pause shows its card
SHORTENED = 1.0                  # seconds a shortened pause keeps at either end


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
                a, b = a + SHORTENED, b - SHORTENED
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


def words(transcript: dict, p: dict, start: float = 0.0, end: float | None = None) -> list[dict]:
    """The words that remain, with times in the edited video, optionally only those in [start, end)
    of the edited video and then relative to start (for a clip)."""
    out = []
    for w in transcript.get("words", []):
        a, b = map_time(w["start"], p), map_time(w["end"], p)
        if a is None or b is None or b < a:
            continue
        if a < start or (end is not None and b > end):
            continue
        out.append({**w, "start": round(a - start, 3), "end": round(b - start, 3)})
    return out


def vtt(timed_words: list[dict]) -> str:
    body = ["WEBVTT", ""]
    for a, b, text in subtitles.cues([w for w in timed_words if w["word"].strip()]):
        body += [f"{subtitles._time(a)} --> {subtitles._time(b)}", text, ""]
    return "\n".join(body)


def chapters(e: dict, p: dict) -> list[dict]:
    """Accepted chapters in the edited video; the first at 0:00, none twice at the same time."""
    out = []
    for x in sorted((x for x in e["items"] if x["kind"] == "chapter" and x["state"] == "accepted"),
                    key=lambda x: x["start"]):
        t = map_forward(x["start"], p)
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
