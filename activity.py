"""Which program is in front during a recording: process name and window title.

Sampled a few times a second while recording and merged into spans, so the cut agent
later knows that, say, Outlook was visible from 12:03 to 12:20 without looking at a
single frame. The spans stay in the local session folder; window titles can hold
personal data (mail subjects, file names) and are not uploaded.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TITLE_LIMIT = 200

_user32, _kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
_kernel32.OpenProcess.restype = wt.HANDLE


def foreground() -> tuple[str, str] | None:
    """(process name, window title) of the window in front, or None for this app's own windows."""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wt.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == os.getpid():
        return None                          # the control bar itself
    title = ctypes.create_unicode_buffer(TITLE_LIMIT + 1)
    _user32.GetWindowTextW(hwnd, title, TITLE_LIMIT + 1)
    name = ""
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if handle:
        buf, size = ctypes.create_unicode_buffer(260), wt.DWORD(260)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            name = os.path.basename(buf.value)
        _kernel32.CloseHandle(handle)
    return name, title.value


class Tracker:
    """Merges samples into spans and hands each finished span to on_span(start, end, process, title)."""

    def __init__(self, on_span):
        self.on_span = on_span
        self.current: tuple[str, str] | None = None
        self.since = 0.0

    def sample(self, t: float) -> None:
        """t is the recording time of the sample."""
        now = foreground()
        if now is None or now == self.current:
            return
        self.close(t)
        self.current, self.since = now, t

    def close(self, t: float) -> None:
        """End the current span, e.g. on pause or stop."""
        if self.current is not None and t > self.since:
            self.on_span(self.since, t, *self.current)
        self.current = None
