"""Step "subtitles": transcript.json -> subtitles.<language>.vtt.

Cues follow the word timestamps: a new cue starts after the end of a sentence, at a
pause, or when the cue would get too long to read. At most two lines of 42 characters
and seven seconds per cue, the usual limits for readable subtitles.
"""
from __future__ import annotations

import json
from pathlib import Path

LINE, LINES, LONGEST, PAUSE = 42, 2, 7.0, 0.8


def _time(t: float) -> str:
    h, rest = divmod(max(0.0, t), 3600)
    m, s = divmod(rest, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def _lines(words: list[str]) -> str:
    lines, cur = [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > LINE:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    lines.append(cur)
    return "\n".join(lines)


def cues(words: list[dict]) -> list[tuple[float, float, str]]:
    result, cur = [], []
    for w in words:
        if cur:
            text = " ".join(x["word"] for x in cur)
            ends_sentence = text.rstrip().endswith((".", "?", "!")) and cur[-1]["end"] - cur[0]["start"] >= 1.0
            too_long = len(text) + 1 + len(w["word"]) > LINE * LINES or w["end"] - cur[0]["start"] > LONGEST
            if ends_sentence or too_long or w["start"] - cur[-1]["end"] > PAUSE:
                result.append((cur[0]["start"], cur[-1]["end"], _lines([x["word"] for x in cur])))
                cur = []
        cur.append({**w, "word": w["word"].strip()})
    if cur:
        result.append((cur[0]["start"], cur[-1]["end"], _lines([x["word"] for x in cur])))
    return result


def run(session_dir: Path) -> Path:
    session_dir = Path(session_dir)
    transcript = json.loads((session_dir / "transcript.json").read_text(encoding="utf-8"))
    target = session_dir / f"subtitles.{transcript.get('language') or 'de'}.vtt"
    body = ["WEBVTT", ""]
    for start, end, text in cues([w for w in transcript["words"] if w["word"].strip()]):
        body += [f"{_time(start)} --> {_time(end)}", text, ""]
    target.write_text("\n".join(body), encoding="utf-8")
    return target
