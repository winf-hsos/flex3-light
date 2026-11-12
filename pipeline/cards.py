"""The pictures the cut adds: title, chapter, pause and closing cards, and lower thirds.

In the colors, fonts and logo of the server's style (style.py), as PNG at the size of the video;
without a style, in the university's blue with white type. Cards fill the frame; a lower third is a
band at the bottom left on a transparent picture, laid over the running video.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import style as styles


def _font(s: dict, size: int, weight: str = "regular"):
    try:
        return ImageFont.truetype(styles.font(s, weight), size)
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


def _mark(img: Image.Image, s: dict, x: int, y: int, size: int) -> None:
    """The style's logo, fitted into a square at the bottom right; without one the FLEX3 mark, a rounded
    square in the text color with a dot in the background color."""
    logo = styles.asset(s, "logo")
    if logo is not None:
        try:
            with Image.open(logo) as raw:
                pic = raw.convert("RGBA")
            pic.thumbnail((size * 4, size))                 # a wide logo may be up to four times as wide
            img.paste(pic, (x + size - pic.width, y + (size - pic.height) // 2), pic)
            return
        except OSError:
            pass
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((x, y, x + size, y + size), radius=size * 0.22, fill=styles.rgb(s, "text"))
    r = size * 0.2
    draw.ellipse((x + size / 2 - r, y + size / 2 - r, x + size / 2 + r, y + size / 2 + r),
                 fill=styles.rgb(s, "background"))


def card(path: Path, W: int, H: int, title: str, kicker: str = "", footer: str = "", style: dict | None = None) -> Path:
    """A full card: a small line above (kicker), the title large, a line at the bottom (footer)."""
    s = style or styles.load(None)
    ink = styles.rgb(s, "text")
    img = Image.new("RGB", (W, H), styles.rgb(s, "background"))
    d = ImageDraw.Draw(img)
    left, width = int(W * 0.1), int(W * 0.8)
    big, small = _font(s, H // 13, "semibold"), _font(s, H // 30)
    lines = _wrap(d, title, big, width)
    line_h = int(H / 13 * 1.25)
    block = len(lines) * line_h + (int(H / 30 * 2.2) if kicker else 0)
    y = (H - block) // 2
    if kicker:
        d.text((left, y), kicker, fill=ink, font=small)
        y += int(H / 30 * 2.2)
    for line in lines:
        d.text((left, y), line, fill=ink, font=big)
        y += line_h
    if footer:
        d.text((left, int(H * 0.86)), footer, fill=ink, font=small)
    _mark(img, s, W - left - H // 18, int(H * 0.855), H // 18)
    img.save(path)
    return path


def lower_third(path: Path, W: int, H: int, text: str, kicker: str = "", style: dict | None = None) -> Path:
    """A band at the bottom left on a transparent picture."""
    s = style or styles.load(None)
    ink = styles.rgb(s, "text")
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    big, small = _font(s, H // 26, "semibold"), _font(s, H // 44)
    pad = H // 50
    tw = max(d.textlength(text, font=big), d.textlength(kicker, font=small) if kicker else 0)
    th = H // 26 + (H // 44 + pad // 2 if kicker else 0)
    x0, y1 = int(W * 0.05), int(H * 0.86)
    y0 = y1 - th - 2 * pad
    d.rounded_rectangle((x0, y0, x0 + tw + 2 * pad + H // 120, y1), radius=pad // 2,
                        fill=(*styles.rgb(s, "background"), 235))
    d.rectangle((x0, y0, x0 + H // 120, y1), fill=(*styles.rgb(s, "accent"), 255))
    y = y0 + pad
    if kicker:
        d.text((x0 + pad + H // 120, y - pad // 4), kicker, fill=ink, font=small)
        y += H // 44 + pad // 2
    d.text((x0 + pad + H // 120, y - pad // 4), text, fill=ink, font=big)
    img.save(path)
    return path
