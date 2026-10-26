"""Split the wide OBS recording into two synchronized tracks, optionally with a program mix.

    python split.py <recording.mkv> [--out <folder>] [--no-camera]

The screen sits top left (up to 1920x1200), the webcam on the right
(1920x1080, centered in its 1920x1200 field). All outputs carry the same
audio and start at the same instant, so no alignment is needed later.
FLEX3 Light runs this on every one-minute segment while recording and joins
the parts afterwards with join().
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

from hardware import NO_WINDOW, ffmpeg, ffmpeg_video_args

FIELD_W, FIELD_H = 1920, 1200
CAMERA_H = 1080
FPS = 30
MIX_W, MIX_H = 1920, 1080           # program mix after the FLEX3 contract
PIP_W, PIP_MARGIN = 480, 24         # webcam in the mix: a quarter of the width, bottom right
BELOW_NORMAL_PRIORITY = 0x00004000  # BELOW_NORMAL_PRIORITY_CLASS


def duration(path: Path) -> float:
    """Length in seconds, read from ffmpeg's header dump (so no ffprobe is needed)."""
    r = subprocess.run([ffmpeg(), "-hide_banner", "-i", str(path)], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
    if not m:
        raise RuntimeError(f"no duration in {path.name}")
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def freeze_filter(src: str, dst: str, spans: list[tuple[float, float]]) -> str:
    """Filter chain that replaces all frames in each span (seconds) with the frame just before.

    Used for Alt+Tab: the Windows task switcher cannot be excluded from capture,
    so it is covered afterwards while audio and camera keep running.
    """
    if not spans:
        return f"[{src}]null[{dst}]"
    parts, cur = [], src
    for i, (a, b) in enumerate(sorted(spans)):
        first, last = max(1, int(a * FPS)), max(1, int(b * FPS + 0.999))
        nxt = dst if i == len(spans) - 1 else f"fz{i}"
        parts.append(f"[{cur}]split=2[fa{i}][fb{i}];[fa{i}][fb{i}]freezeframes="
                     f"first={first}:last={last}:replace={max(0, first - 2)}[{nxt}]")
        cur = nxt
    return ";".join(parts)


def mix_filter(screen: str, camera: str | None, dst: str) -> str:
    """Program mix for the FLEX3 server: the screen fills a 1920x1080 picture, the webcam sits
    small in the bottom right corner. The layout is a proposal; the contract leaves it open."""
    fit = f"scale={MIX_W}:{MIX_H}:force_original_aspect_ratio=decrease:force_divisible_by=2," \
          f"pad={MIX_W}:{MIX_H}:(ow-iw)/2:(oh-ih)/2"
    if camera is None:
        return f"[{screen}]{fit}[{dst}]"
    return (f"[{screen}]{fit}[mixs];[{camera}]scale={PIP_W}:{PIP_W * 9 // 16}[mixc];"
            f"[mixs][mixc]overlay=W-w-{PIP_MARGIN}:H-h-{PIP_MARGIN}[{dst}]")


def split_recording(source: Path, screen_out: Path, camera_out: Path | None,
                    progress: Callable[[float], None] | None = None,
                    freeze: list[tuple[float, float]] | None = None,
                    screen_size: tuple[int, int] = (FIELD_W, FIELD_H),
                    mix_out: Path | None = None, low_priority: bool = False) -> None:
    """Write the tracks; progress(0..1) is called while encoding.

    freeze: spans in seconds where the screen track stands still (Alt+Tab).
    screen_size: size of the screen image top left in its field (smaller without taskbar).
    mix_out: also write the program mix (see mix_filter) in the same pass.
    low_priority: run ffmpeg below normal priority, so it never slows down a running recording.
    """
    total = duration(source)
    top = (FIELD_H - CAMERA_H) // 2
    sw, sh = screen_size
    frz = freeze_filter("s0", "s", [s for s in (freeze or []) if s[1] > s[0]])
    graph = [f"[0:v]split=2[a][b]" if camera_out else "[0:v]null[a]", f"[a]crop={sw}:{sh}:0:0[s0]", frz]
    if camera_out:
        graph.append(f"[b]crop={FIELD_W}:{CAMERA_H}:{FIELD_W}:{top}[c]")
    tracks = {"s": screen_out, "c": camera_out}
    if mix_out:
        graph.append("[s]split=2[so][sm]")
        tracks["so"] = tracks.pop("s")
        if camera_out:
            graph.append("[c]split=2[co][cm]")
            tracks["co"] = tracks.pop("c")
        graph.append(mix_filter("sm", "cm" if camera_out else None, "m"))
        tracks["m"] = mix_out
    encode = ffmpeg_video_args()
    outputs = []
    for label, out in tracks.items():
        if out:
            outputs += ["-map", f"[{label}]", "-map", "0:a?", *encode, "-c:a", "copy", str(out)]
    screen_out.parent.mkdir(parents=True, exist_ok=True)
    flags = NO_WINDOW | (BELOW_NORMAL_PRIORITY if low_priority else 0)
    p = subprocess.Popen([ffmpeg(), "-y", "-v", "error", "-nostats", "-progress", "pipe:1",
                          "-i", str(source), "-filter_complex", ";".join(graph), *outputs],
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=flags)
    for line in p.stdout:
        if line.startswith("out_time_us=") and progress and total > 0:
            try:
                progress(min(1.0, int(line.split("=")[1]) / 1e6 / total))
            except ValueError:
                pass
    if p.wait() != 0:
        raise RuntimeError("ffmpeg: " + p.stderr.read()[-800:])
    # cross-check: every track as long as the source (one second tolerance)
    for out in tracks.values():
        if out and abs(duration(out) - total) > 1.0:
            raise RuntimeError(f"length mismatch: {out.name}")


def join(parts: list[Path], target: Path) -> None:
    """Concatenate parts of one track without re-encoding; they come from the same encoder
    settings, so their streams line up. Checks that the result is as long as the parts."""
    # The list goes to the temp folder, not next to the target, which may be cloud-synced.
    fd, name = tempfile.mkstemp(suffix=".txt", prefix="flex3-light-join-")
    listing = Path(name)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("".join(f"file '{p.resolve().as_posix()}'\n" for p in parts))
    target.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([ffmpeg(), "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                        "-map", "0", "-c", "copy", "-movflags", "+faststart", str(target)],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    listing.unlink(missing_ok=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg: " + r.stderr[-800:])
    if abs(duration(target) - sum(duration(p) for p in parts)) > 1.0:
        raise RuntimeError(f"length mismatch: {target.name}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("recording")
    ap.add_argument("--out")
    ap.add_argument("--no-camera", action="store_true")
    a = ap.parse_args()
    src = Path(a.recording)
    out = Path(a.out) if a.out else src.parent
    screen = out / f"{src.stem} - screen.mp4"
    camera = None if a.no_camera else out / f"{src.stem} - camera.mp4"
    split_recording(src, screen, camera, lambda x: print(f"\r{x:5.0%}", end=""))
    print()
    for p in [screen, camera]:
        if p:
            print(p)
