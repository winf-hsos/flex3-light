"""Where ffmpeg lives and which video encoder this machine can use.

With an NVIDIA GPU both OBS and the split use NVENC. Without one they fall back
to x264 on the CPU, which works everywhere but costs noticeably more CPU time.
Set FLEX3_LIGHT_ENCODER=x264 to force the fallback (for testing).
"""
from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
from pathlib import Path

NO_WINDOW = 0x08000000      # CREATE_NO_WINDOW: no console flashing up from a windowed app


@functools.cache
def ffmpeg() -> str:
    """The bundled ffmpeg.exe in the packaged app, otherwise the one on the PATH."""
    bundled = Path(getattr(sys, "_MEIPASS", "")) / "ffmpeg.exe"
    if getattr(sys, "frozen", False) and bundled.exists():
        return str(bundled)
    found = shutil.which("ffmpeg")
    if not found:
        raise RuntimeError("ffmpeg wurde nicht gefunden")
    return found


@functools.cache
def typst() -> str | None:
    """The bundled typst.exe in the packaged app, otherwise the one on the PATH; None without one."""
    bundled = Path(getattr(sys, "_MEIPASS", "")) / "typst.exe"
    if getattr(sys, "frozen", False) and bundled.exists():
        return str(bundled)
    return shutil.which("typst")


@functools.cache
def nvenc() -> bool:
    """True if a short NVENC test encode succeeds."""
    if os.environ.get("FLEX3_LIGHT_ENCODER", "").lower() == "x264":
        return False
    r = subprocess.run([ffmpeg(), "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=640x360:d=0.2",
                        "-c:v", "h264_nvenc", "-f", "null", "-"], stdin=subprocess.DEVNULL, capture_output=True, creationflags=NO_WINDOW)
    return r.returncode == 0


def obs_encoder() -> tuple[str, dict]:
    """OBS encoder id and its settings for the recording.

    NVENC CQP 26 gives about 7.5 Mbit/s for the wide canvas (3.4 GB per hour); the
    simple output preset "HQ" wrote 42 Mbit/s, almost all of it webcam noise.
    """
    if nvenc():
        return "obs_nvenc_h264_tex", {"rate_control": "CQP", "cqp": 26, "preset2": "p5", "tune": "hq",
                                      "multipass": "qres", "profile": "high", "keyint_sec": 2, "bf": 2}
    return "obs_x264", {"rate_control": "CRF", "crf": 23, "preset": "veryfast", "profile": "high", "keyint_sec": 2}


def ffmpeg_video_args() -> list[str]:
    """Encoder arguments for the split; mp4 with the index up front, so players start right away."""
    if nvenc():
        codec = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0"]
    else:
        codec = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21"]
    return codec + ["-pix_fmt", "yuv420p", "-movflags", "+faststart"]
