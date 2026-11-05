"""Build a single flex3-light.exe with PyInstaller, ffmpeg and Typst included.

    python tools/build_exe.py

Takes ffmpeg (it needs h264_nvenc and libx264, e.g. a gyan.dev full build) and
typst (which sets the summary as PDF) from the PATH and writes dist/flex3-light.exe.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

if __name__ == "__main__":
    ffmpeg, typst = shutil.which("ffmpeg"), shutil.which("typst")
    if not ffmpeg or not typst:
        sys.exit("ffmpeg and typst must both be on the PATH")
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
                    "--name", "flex3-light", "--icon", str(ROOT / "assets" / "icon.ico"),
                    "--add-data", f"{ROOT / 'ui.html'};.", "--add-data", f"{ROOT / 'assets' / 'icon.ico'};assets",
                    "--add-binary", f"{ffmpeg};.", "--add-binary", f"{typst};.",
                    "--add-data", f"{ROOT / 'pipeline' / 'prompts'};pipeline/prompts",
                    "--add-data", f"{ROOT / 'pipeline' / 'templates'};pipeline/templates",
                    "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"),
                    "--specpath", str(ROOT / "build"),
                    str(ROOT / "recorder.py")], check=True, cwd=ROOT)
    exe = ROOT / "dist" / "flex3-light.exe"
    print(f"{exe} ({exe.stat().st_size / 2**20:.0f} MB)")
