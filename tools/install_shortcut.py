"""Create a Start menu entry that runs the recorder from this source folder.

    python tools/install_shortcut.py

The entry starts recorder.py with pythonw, so it always runs the current code
in this folder rather than a released exe. It carries the same AppUserModelID
as the window, so pinning it to the taskbar gives one button for both.
Needs pywin32.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pythoncom
from win32com.propsys import propsys, pscon
from win32com.shell import shell

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from recorder import APP_ID, TITLE  # noqa: E402

if __name__ == "__main__":
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    link = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{TITLE}.lnk"
    sl = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
    sl.SetPath(str(pythonw))
    sl.SetArguments(f'"{ROOT / "recorder.py"}"')
    sl.SetWorkingDirectory(str(ROOT))
    sl.SetIconLocation(str(ROOT / "assets" / "icon.ico"), 0)
    sl.SetDescription("Record and stream lectures with OBS")
    store = sl.QueryInterface(propsys.IID_IPropertyStore)
    store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(APP_ID))
    store.Commit()
    sl.QueryInterface(pythoncom.IID_IPersistFile).Save(str(link), 0)
    print(f"created {link}")
