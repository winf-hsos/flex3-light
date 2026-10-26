"""FLEX3 Light: a small window on top of OBS, HTML user interface via pywebview (Edge WebView2).

Before recording: module, topic, screen, camera, microphone, options, folders.
While recording the window shrinks to a slim bar with time, level, pause and
stop. After stopping, the recording is split and filed.

Global hotkeys:
    Ctrl+Alt+R   start recording; press twice within 3 seconds to stop
    Ctrl+Alt+P   pause and resume
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from pathlib import Path

import webview

# Only the bare background of a drag region moves the window; a press on a button
# inside it must stay a click.
webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = True

from core import Core

TITLE = "FLEX3 Light"
APP_ID = "winf-hsos.Flex3Light"
# The lecture recorder this app was forked from. Both drive the same OBS and register
# the same hotkeys, so only one of them may run at a time.
LECTURE_RECORDER_MUTEX = "Local\\LectureRecorder"
HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))   # unpack folder of the packaged app
FULL = (900, 440)           # fields left, camera preview right, record button and status below
BAR = (480, 58)
ASK = (560, 58)


class Api:
    """Methods callable from JavaScript as window.pywebview.api.<name>()."""

    def __init__(self, core: Core):
        self._core = core
        self._window = None
        self._full_position = None
        self._full_size = FULL
        self._mode = "full"

    def data(self):
        return self._core.data()

    def status(self):
        return self._core.status()

    def select(self, kind, name):
        self._core.select(kind, name)

    def option(self, name, value):
        self._core.set_option(name, value)

    def texts(self, module, topic):
        self._core.set_text(module or "", topic or "")

    def save_module(self, entry):
        return self._core.save_module(entry or {})

    def delete_module(self, eid):
        self._core.delete_module(eid)

    def folder(self, kind):
        current = self._core.archive if kind == "archive_dir" else self._core.raw_dir
        picked = self._window.create_file_dialog(webview.FOLDER_DIALOG, directory=str(current))
        if picked:
            self._core.set_folder(kind, picked[0] if isinstance(picked, (list, tuple)) else picked)

    def start(self):
        return self._core.start()

    def pause(self):
        self._core.pause()

    def stop(self):
        self._core.stop()

    def resume(self):
        self._core.resume()

    def finish(self, save):
        self._core.finish(bool(save))

    def open_result(self):
        self._core.open_result()

    def view(self, mode):
        """Switch the window between "full" (setup), "bar" (recording) and "ask" (save question).

        Only the bar and the save question stay on top of other windows; the setup window
        behaves like any other window.
        """
        w = self._window
        set_topmost(mode != "full")
        if mode == "full":
            capture_exclusion(False)              # the recording is over: screenshots may show it again
            w.resize(*self._full_size)
            if self._full_position:
                w.move(*self._full_position)
        else:
            size = BAR if mode == "bar" else ASK
            if self._mode == "full":
                self._full_position = (w.x, w.y)
                screen_width = webview.screens[0].width               # logical pixels, like move()
                w.move(max(0, (screen_width - size[0]) // 2), 12)     # top center, draggable
            else:                                                    # bar <-> question: stay centered
                old = BAR if self._mode == "bar" else ASK
                w.move(max(0, w.x - (size[0] - old[0]) // 2), w.y)
            w.resize(*size)
        self._mode = mode

    def fit(self, delta):
        """Grow or shrink the setup window by delta CSS pixels, so its content fits exactly.

        The inner height of a frameless window falls short of its outer size by a few
        pixels that depend on Windows and the display scaling, so the page measures
        itself and asks for the difference.
        """
        if self._mode != "full" or not int(delta):
            return
        w = self._window
        self._full_size = (w.width, min(900, max(300, w.height + int(delta))))
        w.resize(*self._full_size)

    def widen(self, width):
        """Make the bar wide enough for its content (CSS pixels), keeping it centered."""
        w = self._window
        grow = int(width) - w.width                          # pywebview sizes are logical pixels
        if grow > 0:
            w.move(max(0, w.x - grow // 2), w.y)
            w.resize(int(width), BAR[1])

    def minimize(self):
        self._window.minimize()

    def close(self):
        self._core.finish(save=True)          # closing during a recording keeps it
        self._window.destroy()


def set_topmost(on_top: bool) -> None:
    """Keep the window above all others, or not.

    Not through pywebview's window.on_top: on Windows it sets the form's TopMost property
    from the calling thread, and called from the JavaScript bridge that hung the app as
    soon as a recording started (2026-10-26), so OBS kept recording with nobody to stop it.
    SetWindowPos is safe from any thread; pywebview moves and resizes the window the same way.
    """
    hwnd = ctypes.windll.user32.FindWindowW(None, TITLE)
    if hwnd:
        HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
        SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10
        ctypes.windll.user32.SetWindowPos(hwnd, HWND_TOPMOST if on_top else HWND_NOTOPMOST, 0, 0, 0, 0,
                                          SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)


def capture_exclusion(exclude: bool) -> None:
    """Hide the window from screen capture (WDA_EXCLUDEFROMCAPTURE) or show it again.

    Hidden it stays visible on screen but never ends up in a recording; Windows applies
    this to every capture, screenshots included. So it is only switched on right before
    a recording starts and off again when the setup window comes back.
    """
    if os.environ.get("FLEX3_LIGHT_VISIBLE"):          # tests that take screenshots set this
        return
    hwnd = ctypes.windll.user32.FindWindowW(None, TITLE)
    if hwnd:
        ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0x11 if exclude else 0)
        if exclude:
            time.sleep(0.1)                            # let the compositor apply it before OBS records


def single_instance() -> bool:
    """Only one window: two instances fight over hotkeys and the recording. A second
    start brings the existing window to the front and exits."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)   # windll's GetLastError can be overwritten
    single_instance.mutex = kernel32.CreateMutexW(None, False, "Local\\Flex3Light")
    if ctypes.get_last_error() != 183:                  # ERROR_ALREADY_EXISTS
        return True
    hwnd = ctypes.windll.user32.FindWindowW(None, TITLE)
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 9)        # SW_RESTORE
        ctypes.windll.user32.SetForegroundWindow(hwnd)
    return False


def lecture_recorder_running() -> bool:
    """True while the lecture recorder runs; it holds its single-instance mutex."""
    handle = ctypes.windll.kernel32.OpenMutexW(0x00100000, False, LECTURE_RECORDER_MUTEX)   # SYNCHRONIZE
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
    return bool(handle)


def main(test_func=None) -> None:
    if not single_instance():
        return
    if lecture_recorder_running():
        ctypes.windll.user32.MessageBoxW(None, "Der Lecture Recorder läuft gerade. Beide Programme steuern "
                                         "dasselbe OBS, bitte ihn zuerst schließen.", TITLE, 0x30)
        return
    # Own taskbar identity, so a pinned Start menu entry with the same ID and the
    # running window share one taskbar button.
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    try:
        core = Core()
    except Exception as e:
        ctypes.windll.user32.MessageBoxW(None, f"OBS ließ sich nicht vorbereiten:\n{e}", TITLE, 0x10)
        sys.exit(1)
    api = Api(core)
    window = webview.create_window(TITLE, url=str(HERE / "ui.html"), js_api=api,
                                   width=FULL[0], height=FULL[1], min_size=(300, 50), resizable=False,
                                   frameless=True, on_top=False, background_color="#f5f7fa", easy_drag=False)
    api._window = window
    core.on_question = window.restore         # stopped by hotkey while minimized: show the question
    core.on_capture = capture_exclusion       # hidden from capture only while recording
    # Closed by Windows (Alt+F4, taskbar) during a recording: keep it, like the close button does,
    # instead of leaving OBS recording with nobody to stop it.
    window.events.closing += lambda: core.finish(save=True)
    if test_func:
        webview.start(test_func, window)
    else:
        webview.start()


if __name__ == "__main__":
    main()
