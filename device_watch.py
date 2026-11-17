"""A cheap fingerprint of the cameras, microphones and screens Windows has right now.

Asking OBS for its device lists takes one to two seconds (DirectShow probes every camera), far
too long to do every few seconds. This fingerprint costs a few milliseconds, so the app checks it
while idle and asks OBS again only when it changes, that is when something was plugged in or out.
"""
from __future__ import annotations

import ctypes
import sys

# Device interface classes: capture filters (cameras and microphones as DirectShow sees them),
# cameras of the newer camera stack, and audio devices.
KSCATEGORY_CAPTURE = "{65E8773D-8F56-11D0-A3B9-00A0C9223196}"
KSCATEGORY_VIDEO_CAMERA = "{E5323777-F976-4F5B-9B55-B94699C46E44}"
KSCATEGORY_AUDIO = "{6994AD04-93EF-11D0-A3CC-00A0C9223196}"
SM_CMONITORS = 80


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


def _interfaces(guid: str) -> str:
    """The present interfaces of one class as one string; '' if Windows does not answer."""
    g = _GUID()
    if ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(guid), ctypes.byref(g)) != 0:
        return ""
    cfg = ctypes.windll.cfgmgr32
    size = ctypes.c_ulong()
    if cfg.CM_Get_Device_Interface_List_SizeW(ctypes.byref(size), ctypes.byref(g), None, 0) != 0:
        return ""
    buf = ctypes.create_unicode_buffer(size.value)
    # a device arriving between the two calls makes the buffer too small: '' then, and the
    # changed fingerprint only costs one extra look at OBS
    if cfg.CM_Get_Device_Interface_ListW(ctypes.byref(g), None, buf, size, 0) != 0:
        return ""
    return buf[:size.value]


def fingerprint() -> tuple:
    """Changes whenever a camera, microphone or screen comes or goes; () outside Windows."""
    if sys.platform != "win32":
        return ()
    try:
        return (_interfaces(KSCATEGORY_CAPTURE), _interfaces(KSCATEGORY_VIDEO_CAMERA),
                _interfaces(KSCATEGORY_AUDIO), ctypes.windll.winmm.waveInGetNumDevs(),
                ctypes.windll.user32.GetSystemMetrics(SM_CMONITORS))
    except Exception:
        return ()
