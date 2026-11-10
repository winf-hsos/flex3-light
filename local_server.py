"""Start the FLEX3 Light server on this computer when the app is connected to one here.

Since phase 5 (2026-11-09) the app only records; the server does everything after the
recording. Until the server moves to the workstation (phase 6) it runs on the lecturer's
own computer, at the address of the connection code (http://127.0.0.1:8765). The app
starts it when it is not running yet (decision of 2026-11-09), as a process of its own
without a window, so it goes on working when the window is closed; it ends by itself
once nobody has asked for anything for a while and its pipeline has nothing left to do
(tools/dev_server.py --idle-exit). A server elsewhere is left alone.
"""
from __future__ import annotations

import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCAL_HOSTS = ("127.0.0.1", "localhost")
IDLE_MINUTES = 30
# A hidden console rather than none (DETACHED_PROCESS): the server's own children, ffmpeg above all,
# share it instead of each opening a window of its own (2026-11-10).
NO_WINDOW, NEW_GROUP = 0x08000000, 0x00000200


def is_local(url: str) -> bool:
    return urllib.parse.urlsplit(url or "").hostname in LOCAL_HOSTS


def running(url: str, timeout: float = 2) -> bool:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=timeout) as r:
            return r.status == 200
    except OSError:
        return False


def ensure(url: str) -> bool:
    """Start the local server if url points to this computer and nothing answers there.
    Returns True if it was started. The frozen exe has no server in it and starts nothing."""
    if not is_local(url) or getattr(sys, "frozen", False) or running(url):
        return False
    port = urllib.parse.urlsplit(url).port or 8765
    python = Path(sys.executable)
    if python.name.lower() == "pythonw.exe":            # a console Python, so the server can log; no window
        python = python.with_name("python.exe")
    log = Path.home() / "AppData" / "Local" / "flex3-light-devserver" / "server.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "wb") as out:                        # the latest run only: the open page asks every 5 s
        subprocess.Popen([str(python), str(HERE / "tools" / "dev_server.py"), "--port", str(port),
                          "--idle-exit", str(IDLE_MINUTES)],
                         cwd=str(HERE), stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                         creationflags=NO_WINDOW | NEW_GROUP if sys.platform == "win32" else 0, close_fds=True)
    return True
