"""Run the FLEX3 Light server on this computer, to try the app against it.

    python tools/dev_server.py                 run it (Ctrl+C stops it)
    python tools/dev_server.py --invite        print a connection code for you as lecturer

Paste the connection code in FLEX3 Light under Einstellungen, Server, and click
"Verbinden"; the app exchanges it for its own device token, which lives in the Windows
Credential Manager and is never shown. For other lecturers or devices use the admin tool
(python -m flex3_light_server.admin, see server/README.md).

Data lives in %LOCALAPPDATA%\\flex3-light-devserver, outside any cloud-synced folder:
sessions/ holds the finished session folders, profiles/ the module lists, accounts.sqlite
lecturers and devices. A tokens.json from before 2026-11-06 keeps working.

To try the offline mode, stop the server: the app keeps recordings and module changes
locally and delivers them when the server runs again.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
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
    print(f"FLEX3 Light server on {url}, data in {DATA}")
    uvicorn.run(create_app(DATA, tokens), host="127.0.0.1", port=a.port, log_level="info")
