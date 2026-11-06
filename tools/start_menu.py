"""Put FLEX3 Light into the Start menu, from where it can be pinned to the taskbar.

    python tools/start_menu.py

The entry starts recorder.py with pythonw.exe, so no console window opens, shows the app's
icon and carries the same AppUserModelID as the window (recorder.APP_ID). That makes the
running window and a pinned taskbar icon one button instead of two. Windows does not let
programs pin themselves: right-click the running window's taskbar button, or the Start menu
entry, and choose "An Taskleiste anheften".
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pythoncom
from win32com.propsys import propsys, pscon
from win32com.shell import shell

ROOT = Path(__file__).resolve().parent.parent
APP_ID = "winf-hsos.Flex3Light"           # as in recorder.py
NAME = "FLEX3 Light"


def install() -> Path:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    link = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{NAME}.lnk"
    sl = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
    sl.SetPath(str(pythonw))
    sl.SetArguments(f'"{ROOT / "recorder.py"}"')
    sl.SetWorkingDirectory(str(ROOT))
    sl.SetIconLocation(str(ROOT / "assets" / "icon.ico"), 0)
    sl.SetDescription("Vorlesungen aufnehmen, schneiden und veröffentlichen")
    store = sl.QueryInterface(propsys.IID_IPropertyStore)
    store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(APP_ID))
    store.Commit()
    sl.QueryInterface(pythoncom.IID_IPersistFile).Save(str(link), 0)
    return link


if __name__ == "__main__":
    print(f"created {install()}")
