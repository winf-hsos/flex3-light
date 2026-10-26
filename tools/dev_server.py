"""Run the FLEX3 Light server on this computer, to try the app against it.

    python tools/dev_server.py --configure      once: point FLEX3 Light at this server
    python tools/dev_server.py                  run it (Ctrl+C stops it)
    python tools/dev_server.py --unconfigure    back to recording locally only

Data and tokens live in %LOCALAPPDATA%\\flex3-light-devserver, outside any cloud-synced
folder: sessions/ holds the finished session folders, profiles/ the module lists. On the
first run it creates one token for you as lecturer on this computer. --configure writes
the server address and that token into FLEX3 Light's settings; the token is never printed.
FLEX3 Light must be closed for that, because it writes its settings back on exit.

To try the offline mode, stop the server: the app keeps recordings and module changes
locally and delivers them when the server runs again.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

DATA = Path(os.environ["LOCALAPPDATA"]) / "flex3-light-devserver"
TOKENS = DATA / "tokens.json"
SETTINGS = Path(os.environ["APPDATA"]) / "flex3-light" / "settings.json"


def app_running() -> bool:
    handle = ctypes.windll.kernel32.OpenMutexW(0x00100000, False, "Local\\Flex3Light")
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
    return bool(handle)


def own_token() -> tuple[str, dict]:
    """The token for this lecturer on this computer, created on the first run."""
    DATA.mkdir(parents=True, exist_ok=True)
    tokens = json.loads(TOKENS.read_text(encoding="utf-8")) if TOKENS.exists() else {}
    user = os.environ.get("USERNAME", "lecturer").lower()
    device = "flex3-light-" + os.environ.get("COMPUTERNAME", "pc").lower()
    for token, who in tokens.items():
        if isinstance(who, dict) and who.get("user_id") == user and who.get("device_id") == device:
            return token, who
    token, who = secrets.token_urlsafe(32), {"device_id": device, "user_id": user, "user_name": user}
    tokens[token] = who
    TOKENS.write_text(json.dumps(tokens, indent=1), encoding="utf-8")
    return token, who


def change_settings(update: dict, remove: tuple[str, ...] = ()) -> None:
    if app_running():
        sys.exit("FLEX3 Light läuft gerade. Bitte erst schließen, sonst überschreibt es die Einstellungen.")
    s = json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.exists() else {}
    s.update(update)
    for key in remove:
        s.pop(key, None)
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--configure", action="store_true")
    ap.add_argument("--unconfigure", action="store_true")
    a = ap.parse_args()
    url = f"http://127.0.0.1:{a.port}"
    if a.unconfigure:
        change_settings({}, ("server_url", "server_token", "device_id"))
        print("FLEX3 Light nimmt wieder nur lokal auf.")
        sys.exit()
    token, who = own_token()
    if a.configure:
        change_settings({"server_url": url, "server_token": token, "device_id": who["device_id"]})
        print(f"FLEX3 Light liefert jetzt an {url} (Lehrperson {who['user_id']}, Gerät {who['device_id']}).")
        sys.exit()
    import uvicorn
    from flex3_light_server.app import create_app

    tokens = json.loads(TOKENS.read_text(encoding="utf-8"))
    print(f"FLEX3 Light server on {url}, data in {DATA}")
    uvicorn.run(create_app(DATA, tokens), host="127.0.0.1", port=a.port, log_level="info")
