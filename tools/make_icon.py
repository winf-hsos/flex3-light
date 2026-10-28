"""Draws the app icon: assets/icon.png (256 px) and assets/icon.ico (16 to 256 px).

The same mark as in the window's sidebar: a rounded square in the university's blue
with a white dot, the record button reduced to its shape. Each size is drawn on its own
from a large master, and the small ones get a bigger dot and smaller corners, so the
mark stays clear in the taskbar.

    python tools/make_icon.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
BLUE = (0, 158, 227, 255)
WHITE = (255, 255, 255, 255)
SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
MASTER = 1024


def draw(size: int) -> Image.Image:
    small = size <= 24
    scale = MASTER / size
    img = Image.new("RGBA", (MASTER, MASTER), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    inset = (0.5 if small else 1) * scale            # leave a pixel free, as Windows' own icons do
    radius = MASTER * (0.18 if small else 0.22)
    d.rounded_rectangle((inset, inset, MASTER - inset, MASTER - inset), radius=radius, fill=BLUE)
    dot = MASTER * (0.46 if small else 0.40)          # diameter of the white dot
    c = MASTER / 2
    d.ellipse((c - dot / 2, c - dot / 2, c + dot / 2, c + dot / 2), fill=WHITE)
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    images = [draw(s) for s in SIZES]
    out = ROOT / "assets"
    images[-1].save(out / "icon.png")
    images[-1].save(out / "icon.ico", sizes=[(s, s) for s in SIZES], append_images=images[:-1])
    print("written", out / "icon.ico")


if __name__ == "__main__":
    main()
