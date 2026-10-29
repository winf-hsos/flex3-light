"""Which program is visible during a recording: process name and window title.

Sampled a few times a second while recording and merged into spans, so the cut agent
later knows that, say, Outlook was visible from 12:03 to 12:20 without looking at a
single frame. The spans stay in the local session folder; window titles can hold
personal data (mail subjects, file names) and are not uploaded.

What counts is what the recording shows, not where the keyboard focus is. A click on
the taskbar, the desktop, the Start menu or Alt+Tab gives the focus to Windows' own
shell (explorer.exe, often without a title) while the window beneath stays on screen;
so does a click into this app's own bar. In those moments the topmost visible program
window is taken instead (found to be wrong on 2026-10-27: Claude Code was on screen,
explorer.exe was logged).
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TITLE_LIMIT = 200
GWL_EXSTYLE, WS_EX_TOOLWINDOW, DWMWA_CLOAKED = -20, 0x80, 14
# Windows' own shell: taskbars, desktop, Start menu, search, Alt+Tab and task view
SHELL_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW", "MultitaskingViewFrame",
                 "XamlExplorerHostIslandWindow", "TaskSwitcherWnd", "TaskSwitcherOverlayWnd", "ForegroundStaging",
                 "Windows.UI.Core.CoreWindow", "NotifyIconOverflowWindow", "TopLevelWindowForOverflowXamlIsland"}

_user32, _kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
_kernel32.OpenProcess.restype = wt.HANDLE
_user32.GetWindowLongW.restype = ctypes.c_long
_WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def _pid(hwnd) -> int:
    pid = wt.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _class(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(128)
    _user32.GetClassNameW(hwnd, buf, 128)
    return buf.value


def _title(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(TITLE_LIMIT + 1)
    _user32.GetWindowTextW(hwnd, buf, TITLE_LIMIT + 1)
    return buf.value


def _process(pid: int) -> str:
    name = ""
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        buf, size = ctypes.create_unicode_buffer(260), wt.DWORD(260)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            name = os.path.basename(buf.value)
        _kernel32.CloseHandle(handle)
    return name


def _cloaked(hwnd) -> bool:
    """Windows on another virtual desktop, or hidden apps, are 'cloaked' but count as visible."""
    value = ctypes.c_int(0)
    try:
        ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value))
    except OSError:
        return False
    return value.value != 0


def _is_program_window(hwnd) -> bool:
    return (_user32.IsWindowVisible(hwnd) and not _user32.IsIconic(hwnd) and not _cloaked(hwnd)
            and not (_user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW)
            and _class(hwnd) not in SHELL_CLASSES and _pid(hwnd) != os.getpid() and _title(hwnd))


def topmost_program_window():
    """The highest window in z-order that is a visible program window, not shell and not this app."""
    found = []

    def visit(hwnd, _):
        if _is_program_window(hwnd):
            found.append(hwnd)
            return False                     # windows come top first: the first one is it
        return True
    _user32.EnumWindows(_WNDENUMPROC(visit), 0)
    return found[0] if found else None


def foreground() -> tuple[str, str] | None:
    """(process name, window title) of the program visible in front; None if there is none."""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd or _class(hwnd) in SHELL_CLASSES or _pid(hwnd) == os.getpid():
        hwnd = topmost_program_window()      # focus on the shell or this app: the window beneath is on screen
    if not hwnd:
        return None
    return _process(_pid(hwnd)), _title(hwnd)


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
