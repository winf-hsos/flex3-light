"""Global hotkeys, Alt+Tab detection and the shell's pop-ups.

Runs a Windows message loop in its own thread and reports to a queue:
    ("hotkey", 1)   Ctrl+Alt+R
    ("hotkey", 2)   Ctrl+Alt+P
    ("hotkey", 3)   Ctrl+Alt+H   privacy: freeze the screen track until pressed again
    ("alttab", ("down" | "up", wall_clock_time))
    ("popup", ("down" | "up", wall_clock_time, (cursor_x, cursor_y)))
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import queue
import threading
import time

HOTKEY_RECORD, HOTKEY_PAUSE, HOTKEY_PRIVATE = 1, 2, 3

# Pop-ups of the shell that show other windows: the previews and labels over the taskbar
# (Xaml_WindowedPopupClass, measured on 2026-11-05 with Windows 11: 0.2 to 1.2 s per hover) and
# the full-width island the shell shows while switching (XamlExplorerHostIslandWindow).
POPUP_CLASSES = {"Xaml_WindowedPopupClass", "XamlExplorerHostIslandWindow", "TaskListThumbnailWnd"}
EVENT_OBJECT_DESTROY, EVENT_OBJECT_SHOW, EVENT_OBJECT_HIDE = 0x8001, 0x8002, 0x8003
WINEVENT_OUTOFCONTEXT, WINEVENT_SKIPOWNPROCESS = 0x0000, 0x0002
WM_HOTKEY, WM_TIMER = 0x0312, 0x0113


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
        self._install_popup_hook(u)
        u.SetTimer(None, 0, 250, None)           # drops pop-ups that vanished without telling
        msg = wt.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_HOTKEY:
                self.target.put(("hotkey", msg.wParam))
            elif msg.message == WM_TIMER:
                self._sweep_popups(u)

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

    def _install_popup_hook(self, u) -> None:
        """Event hook on windows appearing and disappearing: while a pop-up of the shell is visible,
        report it like Alt+Tab, so that the screen track stands still. Like the switcher, these
        pop-ups cannot be excluded from capture; they would show small pictures of all windows."""
        self._popups: set[int] = set()
        self._shell_pids: dict[int, bool] = {}
        WINEVENTPROC = ctypes.WINFUNCTYPE(None, wt.HANDLE, wt.DWORD, wt.HWND, ctypes.c_long, ctypes.c_long,
                                          wt.DWORD, wt.DWORD)
        u.SetWinEventHook.argtypes = [wt.DWORD, wt.DWORD, wt.HMODULE, WINEVENTPROC, wt.DWORD, wt.DWORD, wt.DWORD]
        u.SetWinEventHook.restype = wt.HANDLE

        def callback(hook, event, hwnd, id_object, id_child, thread, ms):
            if id_object != 0 or id_child != 0 or not hwnd:           # only whole windows
                return
            try:
                if event == EVENT_OBJECT_SHOW and self._is_shell_popup(u, hwnd):
                    if not self._popups:
                        point = wt.POINT()
                        u.GetCursorPos(ctypes.byref(point))
                        self.target.put(("popup", ("down", time.time(), (point.x, point.y))))
                    self._popups.add(hwnd)
                elif event in (EVENT_OBJECT_HIDE, EVENT_OBJECT_DESTROY) and hwnd in self._popups:
                    self._popups.discard(hwnd)
                    if not self._popups:
                        self.target.put(("popup", ("up", time.time(), None)))
            except Exception:
                pass                                                   # never let a hook raise

        self._win_event = WINEVENTPROC(callback)    # keep a reference, or Python frees it
        if not u.SetWinEventHook(EVENT_OBJECT_DESTROY, EVENT_OBJECT_HIDE, None, self._win_event, 0, 0,
                                 WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS):
            self.failed.append("Erkennung der Taskleisten-Vorschau")

    def _is_shell_popup(self, u, hwnd) -> bool:
        buf = ctypes.create_unicode_buffer(64)
        u.GetClassNameW(hwnd, buf, 64)
        # top-level only; GetParent would return the owner of a pop-up, which these windows have
        if buf.value not in POPUP_CLASSES or u.GetAncestor(hwnd, 1) != u.GetDesktopWindow():   # GA_PARENT
            return False
        pid = wt.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in self._shell_pids:
            name = ""
            k = ctypes.windll.kernel32
            k.OpenProcess.restype = wt.HANDLE
            handle = k.OpenProcess(0x1000, False, pid.value)      # PROCESS_QUERY_LIMITED_INFORMATION
            if handle:
                path, size = ctypes.create_unicode_buffer(260), wt.DWORD(260)
                if k.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                    name = os.path.basename(path.value)
                k.CloseHandle(handle)
            self._shell_pids[pid.value] = name.lower() == "explorer.exe"
        return self._shell_pids[pid.value]

    def _sweep_popups(self, u) -> None:
        gone = {h for h in self._popups if not u.IsWindow(h) or not u.IsWindowVisible(h)}
        if gone:
            self._popups -= gone
            if not self._popups:
                self.target.put(("popup", ("up", time.time(), None)))
