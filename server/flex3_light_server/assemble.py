"""Join the parts of a track into one MP4 without re-encoding, and check the result."""
from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

# No console window per ffmpeg call: started from FLEX3 Light, the server has no visible console,
# and every ffmpeg would otherwise flash a window of its own (2026-11-10, hundreds per lecture).
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def duration(path: Path, attempts: int = 3) -> float:
    """Length in seconds from ffmpeg's header dump; asked again after a second if ffmpeg says
    nothing, as a single call can fail for reasons that have nothing to do with the file."""
    for attempt in range(attempts):
        r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path)], stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
        if m:
            break
        if attempt < attempts - 1:
            time.sleep(1)
    else:
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
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=NO_WINDOW)
    listing.unlink(missing_ok=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg: " + r.stderr[-800:])
    expected = sum(duration(p) for p in parts)
    if abs(duration(target) - expected) > 1.0:
        raise RuntimeError(f"length mismatch in {target.name}")
