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
from ctypes import wintypes
import faulthandler
import logging
import os
import sys
import threading
import time
from pathlib import Path

import webview

# Only the bare background of a drag region moves the window; a press on a button
# inside it must stay a click.
webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = True

from core import APP_DIR, Core, save_settings

LOG_FILE = APP_DIR / "flex3-light.log"
HANG_AFTER = 20             # seconds without a status request from the window count as a hang

TITLE = "FLEX3 Light"
APP_ID = "winf-hsos.Flex3Light"
# The lecture recorder this app was forked from. Both drive the same OBS and register
# the same hotkeys, so only one of them may run at a time.
LECTURE_RECORDER_MUTEX = "Local\\LectureRecorder"
HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))   # unpack folder of the packaged app
FULL = (1120, 700)          # sidebar and pages; while recording only the slim bar
FULL_MIN = (900, 560)       # smaller and the pages would need to scroll sideways
BAR = (530, 58)
ASK = (560, 58)


class Api:
    """Methods callable from JavaScript as window.pywebview.api.<name>()."""

    def __init__(self, core: Core):
        self._core = core
        self._window = None
        self._full_position = None
        saved = core.settings.get("window_size")
        self._full_size = tuple(saved) if saved else FULL
        self._resize_from = self._full_size
        self._normal_rect = None                  # where the window was before it filled the screen
        self._mode = "full"
        self.last_status = time.time()

    def data(self):
        return self._core.data()

    def status(self):
        self.last_status = time.time()            # the window polls every 250 ms; see watch_for_hangs
        return self._core.status()

    def select(self, kind, name):
        self._core.select(kind, name)

    def option(self, name, value):
        self._core.set_option(name, value)

    def texts(self, module, topic):
        self._core.set_text(module or "", topic or "")

    def identify_screen(self):
        return self._core.identify_screen()

    def set_api_key(self, service, secret):
        return self._core.set_api_key(service, secret)

    def delete_api_key(self, service):
        self._core.delete_api_key(service)

    def check_api_key(self, service):
        return self._core.check_api_key(service)

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

    def toggle_private(self):
        self._core.toggle_private()

    def resume(self):
        self._core.resume()

    def finish(self, save):
        self._core.finish(bool(save))

    def open_result(self):
        self._core.open_result()

    def recordings(self):
        return self._core.recordings()

    def recording(self, sid):
        return self._core.recording(sid)

    def open_recording(self, sid):
        self._core.open_recording(sid)

    def redo_transcription(self, sid):
        return self._core.redo_transcription(sid)

    def change_cut_item(self, sid, item_id, changes):
        return self._core.change_cut_item(sid, item_id, changes or {})

    def add_cut_item(self, sid, kind, start, end, params=None):
        return self._core.add_cut_item(sid, kind, start, end, params)

    def remove_cut_item(self, sid, item_id):
        return self._core.remove_cut_item(sid, item_id)

    def set_cut_metadata(self, sid, title, description):
        return self._core.set_cut_metadata(sid, title or "", description or "")

    def set_program_rule(self, sid, program, choice):
        return self._core.set_program_rule(sid, program, choice or "")

    def approve_cut(self, sid, approved=True):
        return self._core.approve_cut(sid, bool(approved))

    def redo_cut(self, sid):
        return self._core.redo_cut(sid)

    def set_cut_settings(self, values):
        self._core.set_cut_settings(values or {})

    def delete_recording(self, sid):
        return self._core.delete_recording(sid)

    def review_word(self, sid, index, text, as_term=False):
        return self._core.review_word(sid, index, text, bool(as_term))

    def view(self, mode):
        """Switch the window between "full" (setup), "bar" (recording) and "ask" (save question).

        Only the bar and the save question stay on top of other windows; the setup window
        behaves like any other window.
        """
        w = self._window
        if mode != "full" and self._normal_rect:     # the bar never fills the screen
            self.toggle_maximize()
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

    def resize_start(self):
        """The window has no frame; the page's edge grips resize it relative to this size."""
        self._resize_from = (self._window.width, self._window.height)

    def resize_by(self, dx, dy):
        if self._mode != "full" or self._normal_rect:
            return
        w, h = self._resize_from
        self._full_size = (max(FULL_MIN[0], w + int(dx)), max(FULL_MIN[1], h + int(dy)))
        self._window.resize(*self._full_size)

    def resize_done(self):
        self._core.settings["window_size"] = list(self._full_size)
        save_settings(self._core.settings)

    def toggle_maximize(self):
        """Fill the work area of the window's monitor, or go back; returns whether it fills it.

        Not through WinForms' maximized state: a frameless form maximized that way covers
        the taskbar. SetWindowPos with the work area keeps it free and is safe from any thread.
        """
        hwnd = ctypes.windll.user32.FindWindowW(None, TITLE)
        if not hwnd:
            return False
        if self._normal_rect:
            x, y, w, h = self._normal_rect
            self._normal_rect = None
        else:
            r = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
            self._normal_rect = (r.left, r.top, r.right - r.left, r.bottom - r.top)

            class MonitorInfo(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                            ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
            info = MonitorInfo(cbSize=ctypes.sizeof(MonitorInfo))
            monitor = ctypes.windll.user32.MonitorFromWindow(hwnd, 2)        # MONITOR_DEFAULTTONEAREST
            ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info))
            work = info.rcWork
            x, y, w, h = work.left, work.top, work.right - work.left, work.bottom - work.top
        SWP_NOZORDER, SWP_NOACTIVATE = 0x4, 0x10
        ctypes.windll.user32.SetWindowPos(hwnd, None, x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE)
        return self._normal_rect is not None

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


def start_error_log() -> None:
    """Write errors to %APPDATA%\\flex3-light\\flex3-light.log: uncaught exceptions in any
    thread, errors pywebview reports from JavaScript calls, and hard crashes (faulthandler).
    The app has no console, so without this a failure leaves no trace."""
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
        os.replace(LOG_FILE, LOG_FILE.with_suffix(".old.log"))
    handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.WARNING, handlers=[handler])
    log = logging.getLogger("flex3-light")
    log.warning("started")
    faulthandler.enable(handler.stream)
    sys.excepthook = lambda t, e, tb: log.error("uncaught error", exc_info=(t, e, tb))
    threading.excepthook = lambda a: log.error("uncaught error in thread %s", a.thread.name if a.thread else "?",
                                               exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    start_error_log.stream = handler.stream


def watch_for_hangs(api: "Api") -> None:
    """Log where every thread is when the window stops asking for its status: that is what
    a hang looks like from the outside. Once per hang, so a long one does not fill the log."""
    reported = False
    while True:
        time.sleep(5)
        stuck = time.time() - api.last_status > HANG_AFTER
        if stuck and not reported and getattr(start_error_log, "stream", None):
            logging.getLogger("flex3-light").warning("window did not ask for its status for %d s, threads:", HANG_AFTER)
            faulthandler.dump_traceback(start_error_log.stream, all_threads=True)
        reported = stuck


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


def set_window_icon(icon: Path) -> None:
    """Give the window FLEX3 Light's icon in the title and the taskbar.

    WinForms takes the icon of the running program, which is python(w).exe when started
    from source; the packaged exe carries its own. WM_SETICON is sent, so it is safe
    from any thread.
    """
    hwnd = ctypes.windll.user32.FindWindowW(None, TITLE)
    if not hwnd or not icon.exists():
        return
    user32 = ctypes.windll.user32
    user32.LoadImageW.restype = ctypes.c_void_p
    user32.SendMessageW.argtypes = (ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p)
    IMAGE_ICON, LR_LOADFROMFILE, WM_SETICON, ICON_SMALL, ICON_BIG = 1, 0x10, 0x80, 0, 1
    SM_CXSMICON, SM_CXICON = 49, 11
    for kind, metric in ((ICON_SMALL, SM_CXSMICON), (ICON_BIG, SM_CXICON)):
        size = user32.GetSystemMetrics(metric)
        handle = user32.LoadImageW(None, str(icon), IMAGE_ICON, size, size, LR_LOADFROMFILE)
        if handle:
            user32.SendMessageW(hwnd, WM_SETICON, kind, handle)


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
    start_error_log()
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
        logging.getLogger("flex3-light").error("OBS could not be prepared", exc_info=True)
        ctypes.windll.user32.MessageBoxW(None, f"OBS ließ sich nicht vorbereiten:\n{e}", TITLE, 0x10)
        sys.exit(1)
    api = Api(core)
    threading.Thread(target=watch_for_hangs, args=(api,), daemon=True, name="hang-watch").start()
    window = webview.create_window(TITLE, url=str(HERE / "ui.html"), js_api=api,
                                   width=api._full_size[0], height=api._full_size[1], min_size=(300, 50), resizable=False,
                                   frameless=True, on_top=False, background_color="#f6f8fa", easy_drag=False)
    api._window = window
    core.on_question = window.restore         # stopped by hotkey while minimized: show the question
    core.on_capture = capture_exclusion       # hidden from capture only while recording
    # Closed by Windows (Alt+F4, taskbar) during a recording: keep it, like the close button does,
    # instead of leaving OBS recording with nobody to stop it.
    window.events.closing += lambda: core.finish(save=True)
    window.events.shown += lambda: set_window_icon(HERE / "assets" / "icon.ico")
    if test_func:
        webview.start(test_func, window)
    else:
        webview.start()


if __name__ == "__main__":
    main()
