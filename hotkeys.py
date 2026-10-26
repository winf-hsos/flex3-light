"""Global hotkeys and Alt+Tab detection.

Runs a Windows message loop in its own thread and reports to a queue:
    ("hotkey", 1)   Ctrl+Alt+R
    ("hotkey", 2)   Ctrl+Alt+P
    ("hotkey", 3)   Ctrl+Alt+H   privacy: freeze the screen track until pressed again
    ("alttab", ("down" | "up", wall_clock_time))
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import queue
import threading
import time

HOTKEY_RECORD, HOTKEY_PAUSE, HOTKEY_PRIVATE = 1, 2, 3


class GlobalKeys(threading.Thread):
    MODIFIERS = 0x0002 | 0x0001 | 0x4000      # Ctrl + Alt, no auto repeat

    def __init__(self, target: queue.Queue):
        super().__init__(daemon=True)
        self.target = target
        self.failed: list[str] = []

    def run(self) -> None:
        u = ctypes.windll.user32
        for hid, vk, label in ((HOTKEY_RECORD, 0x52, "Strg+Alt+R"), (HOTKEY_PAUSE, 0x50, "Strg+Alt+P"),
                               (HOTKEY_PRIVATE, 0x48, "Strg+Alt+H")):
            if not u.RegisterHotKey(None, hid, self.MODIFIERS, vk):
                self.failed.append(label)
        self._install_alttab_hook(u)
        msg = wt.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == 0x0312:   # WM_HOTKEY
                self.target.put(("hotkey", msg.wParam))

    def _install_alttab_hook(self, u) -> None:
        """Low-level keyboard hook: Alt+Tab starts with Tab while Alt is held and ends when
        Alt is released. The switcher overlay cannot be excluded from capture; with these
        times split.py freezes the screen track instead."""
        LRESULT = ctypes.c_ssize_t
        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)
        u.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wt.WPARAM, wt.LPARAM]
        u.CallNextHookEx.restype = LRESULT
        u.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, wt.DWORD]
        u.SetWindowsHookExW.restype = ctypes.c_void_p
        u.GetAsyncKeyState.restype = ctypes.c_short
        state = {"active": False}

        def callback(code, w_param, l_param):
            if code == 0:
                vk = ctypes.cast(l_param, ctypes.POINTER(wt.DWORD))[0]
                down = w_param in (0x0100, 0x0104)          # WM_KEYDOWN, WM_SYSKEYDOWN
                if vk == 0x09 and down and not state["active"] and (u.GetAsyncKeyState(0x12) & 0x8000):
                    state["active"] = True
                    self.target.put(("alttab", ("down", time.time())))
                elif vk in (0x12, 0xA4, 0xA5) and not down and state["active"]:
                    state["active"] = False
                    self.target.put(("alttab", ("up", time.time())))
            return u.CallNextHookEx(None, code, w_param, l_param)

        self._callback = HOOKPROC(callback)    # keep a reference, or Python frees it
        if not u.SetWindowsHookExW(13, self._callback, None, 0):   # WH_KEYBOARD_LL
            self.failed.append("Alt+Tab-Erkennung")
