"""Build a single flex3-light.exe with PyInstaller, ffmpeg included.

    python tools/build_exe.py

Takes ffmpeg from the PATH (it needs h264_nvenc and libx264, e.g. a gyan.dev
full build) and writes dist/flex3-light.exe.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

if __name__ == "__main__":
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        sys.exit("ffmpeg not found on the PATH")
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
                    "--name", "flex3-light", "--icon", str(ROOT / "assets" / "icon.ico"),
                    "--add-data", f"{ROOT / 'ui.html'};.",
                    "--add-binary", f"{ffmpeg};.",
                    "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"),
                    "--specpath", str(ROOT / "build"),
                    str(ROOT / "recorder.py")], check=True, cwd=ROOT)
    exe = ROOT / "dist" / "flex3-light.exe"
    print(f"{exe} ({exe.stat().st_size / 2**20:.0f} MB)")
