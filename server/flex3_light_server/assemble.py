"""Join the parts of a track into one MP4 without re-encoding, and check the result."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


def duration(path: Path) -> float:
    """Length in seconds from ffmpeg's header dump."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path)], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
    if not m:
        raise RuntimeError(f"no duration in {path.name}")
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def join(parts: list[Path], target: Path) -> None:
    """Concatenate parts with the concat demuxer and -c copy, then check the length.

    The parts come from the same encoder settings, so their streams line up; the result is
    as long as the parts together, with a tolerance of one second.
    """
    listing = target.with_suffix(".txt")
    listing.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in parts), encoding="utf-8")
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                        "-map", "0", "-c", "copy", "-movflags", "+faststart", str(target)],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace")
    listing.unlink(missing_ok=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg: " + r.stderr[-800:])
    expected = sum(duration(p) for p in parts)
    if abs(duration(target) - expected) > 1.0:
        raise RuntimeError(f"length mismatch in {target.name}")
