"""Lecturers, their devices and the keys that connect a device (decision of 2026-11-06).

Whoever runs the server creates a lecturer and an invitation key for them (admin.py). The
lecturer enters the key in FLEX3 Light, which exchanges it once for a device token
(POST /devices/register). From then on the token says who records on which device: one
token per person and device. A device can be revoked, for example a lost laptop.

Keys and tokens are stored only as SHA-256 hashes, so a copy of the database does not let
anyone connect. Everything lives in one SQLite file in the data folder, which moves along
with the server from this computer to the workstation and later to the data centre.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import secrets
import sqlite3
import threading
import uuid
from pathlib import Path

KEY_PREFIX = "FLX3"
KEY_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"   # no 0/O, 1/I/L, U: easy to read aloud and type
KEY_GROUPS, KEY_GROUP_LEN = 4, 4                     # 16 characters, about 78 bits
KEY_DAYS = 14
SEEN_EVERY = 60                                      # seconds between last_seen updates of a device

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS invites (
    key_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), created TEXT NOT NULL,
    expires TEXT NOT NULL, used TEXT, device_id TEXT);
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), name TEXT NOT NULL,
    token_hash TEXT UNIQUE NOT NULL, created TEXT NOT NULL, last_seen TEXT, revoked TEXT);
"""


class InvalidKey(ValueError):
    """The key is malformed, unknown, used or expired; deliberately one error for all of them."""


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _stamp(t: dt.datetime) -> str:
    return t.isoformat(timespec="seconds")


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def normalize_key(key: str) -> str:
    """'flx3-7k3m q9pd…' and 'FLX37K3MQ9PD…' are the same key; returns the canonical form."""
    raw = re.sub(r"[^A-Z0-9]", "", (key or "").upper())
    raw = raw.removeprefix(KEY_PREFIX)
    if len(raw) != KEY_GROUPS * KEY_GROUP_LEN or any(c not in KEY_ALPHABET for c in raw):
        raise InvalidKey("Der Schlüssel hat nicht die richtige Form.")
    groups = [raw[i:i + KEY_GROUP_LEN] for i in range(0, len(raw), KEY_GROUP_LEN)]
    return "-".join([KEY_PREFIX, *groups])


def new_key() -> str:
    return normalize_key("".join(secrets.choice(KEY_ALPHABET) for _ in range(KEY_GROUPS * KEY_GROUP_LEN)))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:30] or "device"


class Accounts:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ---------------------------------------------------------- lecturers

    def add_user(self, name: str, user_id: str | None = None) -> dict:
        """A lecturer; user_id only to take over an existing profile (e.g. from the old token file)."""
        name = (name or "").strip()
        if not name:
            raise ValueError("Ein Name ist nötig.")
        user = {"id": user_id or uuid.uuid4().hex[:12], "name": name, "created": _stamp(_now())}
        with self.lock:
            self.db.execute("INSERT INTO users (id, name, created) VALUES (:id, :name, :created)", user)
        return user

    def users(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM users ORDER BY name")]

    def find_user(self, text: str) -> dict | None:
        """By id, or by name if exactly one lecturer has it."""
        with self.lock:
            rows = self.db.execute("SELECT * FROM users WHERE id = ? OR lower(name) = lower(?)", (text, text)).fetchall()
        return dict(rows[0]) if len(rows) == 1 else None

    # ---------------------------------------------------------- keys and devices

    def invite(self, user_id: str, days: int = KEY_DAYS) -> str:
        """A new invitation key for the lecturer; shown once, stored only as a hash."""
        if not self.find_user(user_id):
            raise ValueError("Diese Lehrperson gibt es nicht.")
        key, now = new_key(), _now()
        with self.lock:
            self.db.execute("INSERT INTO invites (key_hash, user_id, created, expires) VALUES (?, ?, ?, ?)",
                            (_hash(key), user_id, _stamp(now), _stamp(now + dt.timedelta(days=days))))
        return key

    def register(self, key: str, device_name: str) -> tuple[str, dict]:
        """Exchange an invitation key for a device token; returns (token, identity).

        The same lecturer registering a device of the same name again (a new installation)
        keeps its device_id, so recordings that are still on their way stay its own; the old
        token stops working."""
        key_hash = _hash(normalize_key(key))
        name = (device_name or "").strip()[:60] or "Rechner"
        now = _now()
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                invite = self.db.execute("SELECT * FROM invites WHERE key_hash = ?", (key_hash,)).fetchone()
                if invite is None or invite["used"] or dt.datetime.fromisoformat(invite["expires"]) < now:
                    raise InvalidKey("Der Schlüssel ist unbekannt, schon benutzt oder abgelaufen.")
                user = self.db.execute("SELECT * FROM users WHERE id = ?", (invite["user_id"],)).fetchone()
                same = self.db.execute("SELECT id FROM devices WHERE user_id = ? AND lower(name) = lower(?)",
                                       (user["id"], name)).fetchone()
                if same:
                    device_id = same["id"]
                    self.db.execute("UPDATE devices SET token_hash = ?, revoked = NULL, last_seen = NULL WHERE id = ?",
                                    (_hash(token), device_id))
                else:
                    device_id, k = f"flex3-light-{_slug(name)}", 2
                    while self.db.execute("SELECT 1 FROM devices WHERE id = ?", (device_id,)).fetchone():
                        device_id, k = f"flex3-light-{_slug(name)}-{k}", k + 1
                    self.db.execute("INSERT INTO devices (id, user_id, name, token_hash, created) VALUES (?, ?, ?, ?, ?)",
                                    (device_id, user["id"], name, _hash(token), _stamp(now)))
                self.db.execute("UPDATE invites SET used = ?, device_id = ? WHERE key_hash = ?",
                                (_stamp(now), device_id, key_hash))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        return token, {"device_id": device_id, "user_id": user["id"], "user_name": user["name"]}

    def identity(self, token: str) -> dict | None:
        """Device and lecturer of a token, or None if it is unknown or revoked."""
        if not token:
            return None
        with self.lock:
            row = self.db.execute(
                "SELECT d.id AS device_id, d.user_id, u.name AS user_name, d.last_seen, d.revoked "
                "FROM devices d JOIN users u ON u.id = d.user_id WHERE d.token_hash = ?", (_hash(token),)).fetchone()
            if row is None or row["revoked"]:
                return None
            now = _now()
            if not row["last_seen"] or (now - dt.datetime.fromisoformat(row["last_seen"])).total_seconds() > SEEN_EVERY:
                self.db.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (_stamp(now), row["device_id"]))
        return {"device_id": row["device_id"], "user_id": row["user_id"], "user_name": row["user_name"]}

    def devices(self, user_id: str | None = None) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT d.id, d.name, d.user_id, u.name AS user_name, d.created, d.last_seen, d.revoked "
                "FROM devices d JOIN users u ON u.id = d.user_id WHERE ? IS NULL OR d.user_id = ? ORDER BY u.name, d.name",
                (user_id, user_id)).fetchall()
        return [dict(r) for r in rows]

    def revoke(self, device_id: str) -> bool:
        with self.lock:
            return self.db.execute("UPDATE devices SET revoked = ? WHERE id = ? AND revoked IS NULL",
                                   (_stamp(_now()), device_id)).rowcount == 1


def connection_code(key: str, url: str) -> str:
    """What the lecturer pastes into FLEX3 Light: the key and the server's address in one line."""
    return f"{normalize_key(key)}@{url.rstrip('/')}"


def split_code(code: str) -> tuple[str, str | None]:
    """(key, url or None) from a connection code or a bare key."""
    key, _, url = (code or "").strip().partition("@")
    return key.strip(), (url.strip().rstrip("/") or None)
