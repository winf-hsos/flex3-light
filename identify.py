"""Show briefly which screen area gets recorded, like "Identify" in the Windows display settings.

A frame in the university's blue goes around exactly the recorded area (without the
taskbar when that is left out) and a label sits in its middle, for a few seconds.
Tk draws it in a thread of its own, so every Tk call stays in that thread; the
coordinates are physical pixels, because monitors.py makes the process DPI aware.
"""
from __future__ import annotations

import threading
import tkinter as tk

BLUE, TEXT, MUTED = "#009ee3", "#1d2733", "#5f6b78"
KEY = "#ff00fe"            # colour that Windows renders transparent
BORDER = 8
SECONDS = 2.5

_busy = threading.Lock()


def show(x: int, y: int, width: int, height: int, title: str, detail: str) -> None:
    """Show the frame without blocking; a second call while it is visible does nothing."""
    if _busy.acquire(blocking=False):
        threading.Thread(target=_run, args=(x, y, width, height, title, detail), daemon=True).start()


def _run(x: int, y: int, width: int, height: int, title: str, detail: str) -> None:
    try:
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-transparentcolor", KEY)
        root.geometry(f"{width}x{height}+{x}+{y}")
        c = tk.Canvas(root, width=width, height=height, bg=KEY, highlightthickness=0)
        c.pack()
        half = BORDER // 2
        c.create_rectangle(half, half, width - half, height - half, outline=BLUE, width=BORDER)
        cx, cy, w, h = width // 2, height // 2, 560, 128
        c.create_rectangle(cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2, fill="white", outline=BLUE, width=3)
        c.create_text(cx, cy - 16, text=title, font=("Segoe UI Semibold", 20), fill=TEXT)
        c.create_text(cx, cy + 26, text=detail, font=("Segoe UI", 12), fill=MUTED)
        root.after(int(SECONDS * 1000), root.destroy)
        root.mainloop()
        del c, root                        # let Tk go away in its own thread
    finally:
        _busy.release()
