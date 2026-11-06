"""Manage lecturers, invitation keys and devices of a FLEX3 Light server.

    python -m flex3_light_server.admin --data DIR --url URL invite "Name"   lecturer (new or existing) + connection code
    python -m flex3_light_server.admin --data DIR users                     lecturers
    python -m flex3_light_server.admin --data DIR devices                   devices with last contact
    python -m flex3_light_server.admin --data DIR revoke DEVICE_ID          a device stops working (lost laptop)

--data defaults to FLEX3_LIGHT_SERVER_DATA, --url to FLEX3_LIGHT_SERVER_URL: the address the
lecturer's FLEX3 Light reaches the server at. The connection code is printed once and is
valid for 14 days; it contains the key and the address, so the lecturer only pastes one line.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .accounts import Accounts, connection_code


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="flex3_light_server.admin")
    ap.add_argument("--data", default=os.environ.get("FLEX3_LIGHT_SERVER_DATA", "data"))
    ap.add_argument("--url", default=os.environ.get("FLEX3_LIGHT_SERVER_URL", ""))
    sub = ap.add_subparsers(dest="command", required=True)
    inv = sub.add_parser("invite", help="invitation key for a lecturer, created if new")
    inv.add_argument("name", help="name or id of the lecturer")
    inv.add_argument("--days", type=int, default=14)
    sub.add_parser("users")
    sub.add_parser("devices")
    rev = sub.add_parser("revoke")
    rev.add_argument("device_id")
    a = ap.parse_args(argv)
    accounts = Accounts(Path(a.data) / "accounts.sqlite")
    try:
        if a.command == "invite":
            user = accounts.find_user(a.name) or accounts.add_user(a.name)
            key = accounts.invite(user["id"], a.days)
            print(f"Lehrperson: {user['name']} ({user['id']})")
            print(f"Gültig {a.days} Tage, einmal. In FLEX3 Light unter Einstellungen, Server einfügen:")
            print(connection_code(key, a.url) if a.url else key)
        elif a.command == "users":
            for u in accounts.users():
                print(f"{u['id']}  {u['name']}")
        elif a.command == "devices":
            for d in accounts.devices():
                state = f"gesperrt {d['revoked'][:10]}" if d["revoked"] else f"zuletzt {(d['last_seen'] or '–')[:16]}"
                print(f"{d['id']}  {d['user_name']}  {d['name']}  {state}")
        elif a.command == "revoke":
            print("gesperrt" if accounts.revoke(a.device_id) else "kein aktives Gerät mit dieser Kennung")
    finally:
        accounts.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
