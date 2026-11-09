"""Run the FLEX3 Light server on this computer, to try the app against it.

    python tools/dev_server.py                 run it (Ctrl+C stops it)
    python tools/dev_server.py --invite        print a connection code for you as lecturer
    python tools/dev_server.py --no-keys       run it without keys: the pipeline waits
    python tools/dev_server.py --idle-exit 30  end it after 30 minutes without requests and with nothing to do

Paste the connection code in FLEX3 Light under Einstellungen, Server, and click
"Verbinden"; the app exchanges it for its own device token, which lives in the Windows
Credential Manager and is never shown. For other lecturers or devices use the admin tool
(python -m flex3_light_server.admin, see server/README.md).

Data lives in %LOCALAPPDATA%\\flex3-light-devserver, outside any cloud-synced folder:
sessions/ holds the finished session folders, profiles/ the module lists, accounts.sqlite
lecturers and devices. A tokens.json from before 2026-11-06 keeps working.

The pipeline on this server uses the keys for the AI services that FLEX3 Light keeps in the
Windows Credential Manager of this computer ("FLEX3 Light/openai" and so on), standing in for
the project's keys a real server gets from FLEX3_LIGHT_SERVER_SECRETS. They are read when a
step needs them and never printed. What starts by itself follows the lecturer's profile
("Nach der Aufnahme").

Since phase 5 (2026-11-09) FLEX3 Light starts this server itself when its connection points
to this computer and nothing answers (local_server.py), with --idle-exit, and writes its
output to server.log in the data folder.

To try the offline mode, stop the server: the app keeps recordings and module changes
locally and delivers them when the server runs again.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from flex3_light_server.accounts import Accounts, connection_code  # noqa: E402

DATA = Path(os.environ["LOCALAPPDATA"]) / "flex3-light-devserver"
TOKENS = DATA / "tokens.json"


def invite(url: str) -> str:
    """A connection code for the person at this computer; their profile is kept across runs."""
    accounts = Accounts(DATA / "accounts.sqlite")
    try:
        user_id = os.environ.get("USERNAME", "lecturer").lower()      # as in the token file of before
        if not accounts.find_user(user_id):
            accounts.add_user(user_id, user_id=user_id)
        return connection_code(accounts.invite(user_id), url)
    finally:
        accounts.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--invite", action="store_true")
    ap.add_argument("--no-keys", action="store_true")
    ap.add_argument("--idle-exit", type=float, default=0, metavar="MINUTES")
    a = ap.parse_args()
    url = f"http://127.0.0.1:{a.port}"
    DATA.mkdir(parents=True, exist_ok=True)
    if a.invite:
        print("In FLEX3 Light unter Einstellungen, Server einfügen (gilt einmal, 14 Tage):")
        print(invite(url))
        sys.exit()
    import uvicorn
    from flex3_light_server.app import create_app

    tokens = json.loads(TOKENS.read_text(encoding="utf-8")) if TOKENS.exists() else {}
    keys = None
    if not a.no_keys:
        sys.path.insert(0, str(ROOT))
        import credentials                       # the app's Credential Manager store
        keys = credentials.get
    print(f"FLEX3 Light server on {url}, data in {DATA}", flush=True)
    app = create_app(DATA, tokens, keys=keys)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=a.port, log_level="info"))
    if a.idle_exit > 0:
        last = [time.monotonic()]

        @app.middleware("http")
        async def seen(request, call_next):
            last[0] = time.monotonic()
            return await call_next(request)

        def watch():
            runner = app.state.worker.runner
            while not server.should_exit:
                time.sleep(30)
                if time.monotonic() - last[0] > a.idle_exit * 60 and not runner.queue and not runner.active:
                    print("Nothing to do and nobody asking: the server ends.", flush=True)
                    server.should_exit = True

        threading.Thread(target=watch, daemon=True).start()
    server.run()
