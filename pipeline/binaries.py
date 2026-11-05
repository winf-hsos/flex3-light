"""Where the pipeline finds ffmpeg, and how it reads a video's basic data with it.

On the PATH by default. The packaged app carries its own ffmpeg.exe and sets it here at
start (core.py, hardware.ffmpeg()); the FLEX3 server sets its own. ffprobe is not needed:
the packaged app has none, so a video's size and frame rate are read from ffmpeg's own
description of its input.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

NO_WINDOW = 0x08000000
_ffmpeg = "ffmpeg"
_hwaccel: str | None = None


_typst = "typst"


def configure(ffmpeg: str | None = None, hwaccel: str | None = None, typst: str | None = None) -> None:
    """ffmpeg: path to ffmpeg(.exe); hwaccel: a decoder ffmpeg offers on this machine, e.g. cuda;
    typst: path to typst(.exe), which sets the summary as PDF."""
    global _ffmpeg, _hwaccel, _typst
    if ffmpeg:
        _ffmpeg = ffmpeg
    if typst:
        _typst = typst
    _hwaccel = hwaccel


def ffmpeg() -> str:
    return _ffmpeg


def typst() -> str:
    return _typst


def hwaccel() -> list[str]:
    """Input options that let the graphics card decode, or nothing."""
    return ["-hwaccel", _hwaccel] if _hwaccel else []


def video_info(path: Path) -> dict:
    """{"width", "height", "fps", "duration"} of the first video stream."""
    err = subprocess.run([_ffmpeg, "-hide_banner", "-i", str(path)], stdin=subprocess.DEVNULL, capture_output=True,
                         text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW).stderr
    size = re.search(r"Stream #\S+.*?Video: .*?(\d{2,5})x(\d{2,5})", err)
    fps = re.search(r"Stream #\S+.*?Video: .*?(\d+(?:\.\d+)?) fps", err)
    d = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", err)
    return {"width": int(size.group(1)) if size else 1920, "height": int(size.group(2)) if size else 1080,
            "fps": float(fps.group(1)) if fps else 30.0,
            "duration": int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3)) if d else 0.0}
