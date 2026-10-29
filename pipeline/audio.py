"""Audio for the transcription: extracted once, cut into pieces at speech pauses.

OpenAI takes at most 25 MB per request, so a lecture goes in pieces of about ten
minutes. They are cut in the middle of a pause, never in a sentence; that follows the
procedure of the project's video assistant (silencedetect at -35 dB and 0.4 s).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import binaries

NO_WINDOW = 0x08000000
BELOW_NORMAL_PRIORITY = 0x00004000
NOISE, MIN_SILENCE = "-35dB", 0.4
TARGET, LONGEST = 600.0, 900.0          # seconds per piece: aim, and hard limit without a pause


def _ffmpeg(*args: str) -> subprocess.CompletedProcess:
    r = subprocess.run([binaries.ffmpeg(), "-hide_banner", "-nostats", *args], stdin=subprocess.DEVNULL, capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       creationflags=NO_WINDOW | BELOW_NORMAL_PRIORITY)
    return r


def extract(video: Path, wav: Path) -> None:
    """Mono 16 kHz PCM of the video's sound."""
    r = _ffmpeg("-y", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav))
    if r.returncode != 0:
        raise RuntimeError("ffmpeg: " + r.stderr[-500:])


def duration(path: Path) -> float:
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", _ffmpeg("-i", str(path)).stderr)
    if not m:
        raise RuntimeError(f"no duration in {path.name}")
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def silences(wav: Path) -> list[tuple[float, float]]:
    """Spans of silence (start, end) in seconds."""
    err = _ffmpeg("-i", str(wav), "-af", f"silencedetect=n={NOISE}:d={MIN_SILENCE}", "-f", "null", "-").stderr
    starts = [float(x) for x in re.findall(r"silence_start: (-?\d+(?:\.\d+)?)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: (\d+(?:\.\d+)?)", err)]
    return [(max(0.0, a), b) for a, b in zip(starts, ends)]


def plan(total: float, quiet: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Pieces (start, end) of about TARGET seconds, each ending in the middle of a pause."""
    pieces, cur = [], 0.0
    while total - cur > LONGEST:
        candidates = [(a + b) / 2 for a, b in quiet if cur + TARGET * 0.7 <= (a + b) / 2 <= cur + LONGEST]
        cut = min(candidates, key=lambda m: abs(m - (cur + TARGET))) if candidates else cur + LONGEST
        pieces.append((cur, cut))
        cur = cut
    pieces.append((cur, total))
    return pieces


def export(wav: Path, start: float, end: float, mp3: Path) -> None:
    """One piece as MP3 at 48 kbit/s, well under OpenAI's 25 MB."""
    r = _ffmpeg("-y", "-v", "error", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(wav),
                "-ac", "1", "-c:a", "libmp3lame", "-b:a", "48k", str(mp3))
    if r.returncode != 0:
        raise RuntimeError("ffmpeg: " + r.stderr[-500:])
