"""Manage lecturers, invitation keys and devices of a FLEX3 Light server.

    python -m flex3_light_server.admin --data DIR --url URL invite "Name"   lecturer (new or existing) + connection code
    python -m flex3_light_server.admin --data DIR users                     lecturers
    python -m flex3_light_server.admin --data DIR devices                   devices with last contact
    python -m flex3_light_server.admin --data DIR revoke DEVICE_ID          a device stops working (lost laptop)
    python -m flex3_light_server.admin --data DIR import FOLDER --user ID   a recording made before the server, for a lecturer
    python -m flex3_light_server.admin --data DIR style [--preview DIR]     the style of all videos, its cards as pictures

--data defaults to FLEX3_LIGHT_SERVER_DATA, --url to FLEX3_LIGHT_SERVER_URL: the address the
lecturer's FLEX3 Light reaches the server at. The connection code is printed once and is
valid for 14 days; it contains the key and the address, so the lecturer only pastes one line.

import takes over a session folder that FLEX3 Light filed locally, with the files of the
recording only (session.json, recording.mp4, screen.mp4, camera.mp4) and the activity log
without window titles, matched against the lecturer's title list as the app does on upload.
Results of a local pipeline stay behind; the server's pipeline is started on request
(POST /sessions/{id}/pipeline). The folder appears in sessions/ complete or not at all.

style reads the folder style/ in the data folder (pipeline/style.py) and says what of it holds and
what fell back to the default; --preview draws a title, chapter, pause and closing card and a lower
third in it, at 1920 x 1080, to look at before the server renders with it. The server notices a
change of style within a minute and renders again what is approved and not yet published.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

from .accounts import Accounts, connection_code

RECORDING_FILES = ("recording.mp4", "screen.mp4", "camera.mp4")


def import_session(data: Path, source: Path, user: dict) -> str:
    """Copy a locally filed session folder into the server's sessions for a lecturer; its id."""
    from . import app, profiles
    from .worker import cut_rules                    # pipeline/ at the repository root
    meta = json.loads((source / "session.json").read_text(encoding="utf-8"))
    sid = meta.get("session_id", "")
    if not app.SESSION_ID.match(sid):
        raise SystemExit(f"keine gültige session_id in {source / 'session.json'}")
    target = data / "sessions" / sid
    if target.exists():
        raise SystemExit(f"Die Aufnahme {sid} ist schon auf dem Server.")
    if not (source / "recording.mp4").exists():
        raise SystemExit("recording.mp4 fehlt")
    tmp = data / "sessions" / f".import-{sid}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        for name in RECORDING_FILES:
            if (source / name).exists():
                shutil.copy2(source / name, tmp / name)
        meta.update(instructor_id=user["id"], instructor_name=user["name"])
        (tmp / "session.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        if (source / "activity.json").exists():
            log = json.loads((source / "activity.json").read_text(encoding="utf-8"))
            prefs = profiles.Store(data / "profiles").values(user["id"])
            titles = cut_rules.settings_from(prefs)["cut_titles"]
            clean = app.clean_activity({"activity": cut_rules.without_titles(log.get("activity", []), titles),
                                        "private": log.get("private", [])})
            (tmp / "activity.json").write_text(json.dumps(
                {"note": "Imported; window titles replaced by the word of the title list they contained.", **clean},
                ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, target)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return sid


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
    imp = sub.add_parser("import", help="a recording filed by FLEX3 Light before it had a server")
    imp.add_argument("folder")
    imp.add_argument("--user", required=True, help="name or id of the lecturer who recorded it")
    sty = sub.add_parser("style", help="the style of all videos: what holds, and its cards as pictures")
    sty.add_argument("--preview", help="folder for the pictures")
    a = ap.parse_args(argv)
    if a.command == "style":
        return show_style(Path(a.data), Path(a.preview) if a.preview else None)
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
        elif a.command == "import":
            user = accounts.find_user(a.user)
            if user is None:
                raise SystemExit(f"keine Lehrperson {a.user!r}")
            print(f"übernommen: {import_session(Path(a.data), Path(a.folder), user)}")
    finally:
        accounts.close()
    return 0


def show_style(data: Path, preview: Path | None) -> int:
    from .worker import ROOT  # noqa: F401 - puts the pipeline on the path
    from pipeline import cards, style

    s = style.load(data / "style")
    print(f"Stil: {s['name']} ({style.fingerprint(s)})")
    print(f"Ordner: {data / 'style'}" + ("" if (data / "style" / style.FILE).exists() else " (ohne style.json: Grundstil)"))
    print(f"Farben: Hintergrund {s['colors']['background']}, Akzent {s['colors']['accent']}, Schrift {s['colors']['text']}")
    print(f"Schrift: {style.font(s)} / {style.font(s, 'semibold')}")
    print(f"Logo: {s['logo'] or 'FLEX3-Zeichen'}; Intro: {s['intro'] or 'keins'}; Outro: {s['outro'] or 'keins'}")
    c = s["cards"]
    print(f"Tafeln: Titel {c['title']:g} s, Kapitel {c['chapter']:g} s, Ende {c['closing']:g} s, Blende {c['fade']:g} s; "
          f"Bauchbinde {s['lower_third']['seconds']:g} s; Blende am Schnitt {s['cut_fade_ms']:g} ms")
    for moment in ("title", "chapter", "closing"):
        name, path = style.sound(s, moment, data / "sounds")
        print(f"Klang {moment}: {name or 'keiner'}" + (f" ({path})" if path else " (fehlt: die Tafel bleibt still)" if name else ""))
    for problem in s["problems"]:
        print(f"Hinweis: {problem}")
    if preview:
        preview.mkdir(parents=True, exist_ok=True)
        W, H = 1920, 1080
        cards.card(preview / "title.png", W, H, "Einführung in die Programmierung", "Digitalisierung und Programmierung",
                   "12. November 2026   Prof. Dr. Muster", style=s)
        cards.card(preview / "chapter.png", W, H, "Schleifen und Bedingungen", "Kapitel 2", style=s)
        cards.card(preview / "pause.png", W, H, "Pause entfernt (12 min)", style=s)
        cards.card(preview / "closing.png", W, H, "Danke fürs Zuschauen", "Digitalisierung und Programmierung",
                   "Einführung in die Programmierung", style=s)
        lower = preview / "lower_third.png"
        cards.lower_third(lower, W, H, "Schleifen und Bedingungen", "Kapitel 2", style=s)
        print(f"Vorschau: {preview}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
