"""The pictures the cut adds: title, chapter, pause and closing cards, and lower thirds.

All in the university's blue with white type, as PNG at the size of the video. Cards fill
the frame; a lower third is a band at the bottom left on a transparent picture, laid over
the running video.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BLUE = (0, 158, 227)
DEEP = (0, 110, 159)
WHITE = (255, 255, 255)


def _font(size: int, weight: str = "regular"):
    name = {"bold": "segoeuib.ttf", "semibold": "seguisb.ttf", "light": "segoeuil.ttf"}.get(weight, "segoeui.ttf")
    try:
        return ImageFont.truetype(name, size)
    except OSError:
        return ImageFont.load_default(size=size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        test = f"{cur} {word}".strip()
        if cur and draw.textlength(test, font=font) > width:
            lines.append(cur)
            cur = word
        else:
            cur = test
    if cur:
        lines.append(cur)
    return lines[:3]


def _mark(draw: ImageDraw.ImageDraw, x: int, y: int, size: int) -> None:
    """The FLEX3 mark: a white rounded square with a blue dot."""
    draw.rounded_rectangle((x, y, x + size, y + size), radius=size * 0.22, fill=WHITE)
    r = size * 0.2
    draw.ellipse((x + size / 2 - r, y + size / 2 - r, x + size / 2 + r, y + size / 2 + r), fill=BLUE)


def card(path: Path, W: int, H: int, title: str, kicker: str = "", footer: str = "") -> Path:
    """A full card: a small line above (kicker), the title large, a line at the bottom (footer)."""
    img = Image.new("RGB", (W, H), BLUE)
    d = ImageDraw.Draw(img)
    left, width = int(W * 0.1), int(W * 0.8)
    big, small = _font(H // 13, "semibold"), _font(H // 30)
    lines = _wrap(d, title, big, width)
    line_h = int(H / 13 * 1.25)
    block = len(lines) * line_h + (int(H / 30 * 2.2) if kicker else 0)
    y = (H - block) // 2
    if kicker:
        d.text((left, y), kicker, fill=WHITE, font=small)
        y += int(H / 30 * 2.2)
    for line in lines:
        d.text((left, y), line, fill=WHITE, font=big)
        y += line_h
    if footer:
        d.text((left, int(H * 0.86)), footer, fill=WHITE, font=small)
    _mark(d, W - left - H // 18, int(H * 0.855), H // 18)
    img.save(path)
    return path


def lower_third(path: Path, W: int, H: int, text: str, kicker: str = "") -> Path:
    """A band at the bottom left on a transparent picture."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    big, small = _font(H // 26, "semibold"), _font(H // 44)
    pad = H // 50
    tw = max(d.textlength(text, font=big), d.textlength(kicker, font=small) if kicker else 0)
    th = H // 26 + (H // 44 + pad // 2 if kicker else 0)
    x0, y1 = int(W * 0.05), int(H * 0.86)
    y0 = y1 - th - 2 * pad
    d.rounded_rectangle((x0, y0, x0 + tw + 2 * pad + H // 120, y1), radius=pad // 2, fill=(*BLUE, 235))
    d.rectangle((x0, y0, x0 + H // 120, y1), fill=(*DEEP, 255))
    y = y0 + pad
    if kicker:
        d.text((x0 + pad + H // 120, y - pad // 4), kicker, fill=WHITE, font=small)
        y += H // 44 + pad // 2
    d.text((x0 + pad + H // 120, y - pad // 4), text, fill=WHITE, font=big)
    img.save(path)
    return path
