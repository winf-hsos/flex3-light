"""Profiles per lecturer: the modules they record and their preferences, merged entry by entry.

Every module is an entry with a stable id, an updated_at time and a deleted flag.
Two devices that changed the profile offline are merged entry by entry: the newer
change of an entry wins, and a deletion stays as a tombstone so it is not brought
back by a device that still has the old entry. The same merge runs on the server
and in the app (lecturer_profile.py), so both always end up with the same list.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

LANGUAGE = re.compile(r"^[a-z]{2}$")
CODE = re.compile(r"^[a-z0-9-]{0,40}$")
TEXT_FIELDS = {"name": 200, "ilias_course_id": 100, "opencast_series_id": 100}
MAX_TERMS, TERM_LIMIT = 300, 80     # the module's technical terms, for transcription and its correction


def clean(entry: dict) -> dict:
    """A valid entry with the known fields only; raises ValueError otherwise."""
    if not isinstance(entry, dict):
        raise ValueError("entry is not an object")
    eid, updated = entry.get("id"), entry.get("updated_at")
    if not isinstance(eid, str) or not 0 < len(eid) <= 64:
        raise ValueError("invalid id")
    if not isinstance(updated, (int, float)) or isinstance(updated, bool):
        raise ValueError(f"invalid updated_at in {eid}")
    out = {"id": eid, "updated_at": float(updated), "deleted": bool(entry.get("deleted", False))}
    for field, limit in TEXT_FIELDS.items():
        value = entry.get(field, "")
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError(f"invalid {field} in {eid}")
        out[field] = value.strip()
    code, language = entry.get("code", ""), entry.get("language", "de")
    if not isinstance(code, str) or not CODE.match(code):
        raise ValueError(f"invalid code in {eid}")
    if not isinstance(language, str) or not LANGUAGE.match(language):
        raise ValueError(f"invalid language in {eid}")
    out["code"], out["language"] = code, language
    terms = entry.get("terms", [])
    if not isinstance(terms, list) or len(terms) > MAX_TERMS or any(
            not isinstance(t, str) or not t.strip() or len(t) > TERM_LIMIT for t in terms):
        raise ValueError(f"invalid terms in {eid}")
    out["terms"] = [t.strip() for t in terms]
    if not out["deleted"] and not out["name"]:
        raise ValueError(f"module {eid} has no name")
    return out


def newer(a: dict, b: dict) -> dict:
    """The entry that wins; equal times are decided by content, so every side picks the same."""
    if a["updated_at"] != b["updated_at"]:
        return a if a["updated_at"] > b["updated_at"] else b
    return a if json.dumps(a, sort_keys=True) >= json.dumps(b, sort_keys=True) else b


def merge(ours: dict[str, dict], theirs: list[dict]) -> dict[str, dict]:
    """Merge entries into a dict id -> entry; neither input is changed."""
    result = dict(ours)
    for entry in theirs:
        mine = result.get(entry["id"])
        result[entry["id"]] = entry if mine is None else newer(mine, entry)
    return result


# ---------------------------------------------------------------- preferences
# Everything else that belongs to the lecturer and not to a device: the cut settings, what
# starts after a recording, the default language (decision of 2026-11-06). Each preference
# is one key with a value and its own updated_at, merged key by key like the modules, so two
# devices that changed different settings offline both keep their change. Keys are flat,
# a group is a prefix ("cut.pause_min"); the server keeps any key the app sends within limits
# and leaves the meaning to whoever reads it.

PREF_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}(\.[a-z][a-z0-9_]{0,39})?$")
MAX_PREFERENCES, PREFERENCE_BYTES = 200, 8000


def clean_preferences(prefs) -> dict[str, dict]:
    """Valid preferences {key: {"value", "updated_at"}}; raises ValueError otherwise."""
    if prefs is None:
        return {}
    if not isinstance(prefs, dict) or len(prefs) > MAX_PREFERENCES:
        raise ValueError("invalid preferences")
    out = {}
    for key, item in prefs.items():
        if not isinstance(key, str) or not PREF_KEY.match(key):
            raise ValueError(f"invalid preference key {key!r}")
        updated = item.get("updated_at") if isinstance(item, dict) else None
        if not isinstance(updated, (int, float)) or isinstance(updated, bool):
            raise ValueError(f"invalid updated_at of {key}")
        value = item.get("value")
        try:
            size = len(json.dumps(value))
        except (TypeError, ValueError):
            raise ValueError(f"invalid value of {key}") from None
        if size > PREFERENCE_BYTES:
            raise ValueError(f"value of {key} is too large")
        out[key] = {"value": value, "updated_at": float(updated)}
    return out


def merge_preferences(ours: dict[str, dict], theirs: dict[str, dict]) -> dict[str, dict]:
    """Merge key by key with the same rule as the modules; neither input is changed."""
    result = dict(ours)
    for key, item in theirs.items():
        result[key] = item if key not in result else newer(result[key], item)
    return result


class Store:
    """The profiles on disk, one JSON file per lecturer, named by a hash of their id."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)

    def file(self, user_id: str) -> Path:
        return self.folder / f"{hashlib.sha256(user_id.encode()).hexdigest()[:32]}.json"

    def load(self, user_id: str) -> tuple[dict[str, dict], dict[str, dict]]:
        """(modules by id, preferences by key) of a lecturer; empty for a new one."""
        try:
            data = json.loads(self.file(user_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}, {}
        return {e["id"]: e for e in data["modules"]}, data.get("preferences", {})

    def save(self, user_id: str, modules: dict[str, dict], preferences: dict[str, dict]) -> None:
        f = self.file(user_id)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"user_id": user_id, "modules": list(modules.values()), "preferences": preferences},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, f)

    def values(self, user_id: str) -> dict:
        """The lecturer's preferences as {key: value}."""
        return {k: v["value"] for k, v in self.load(user_id)[1].items()}

    def module(self, user_id: str, name: str) -> dict | None:
        """The lecturer's module of this name (a recording's course_name), if not deleted."""
        norm = " ".join(name.casefold().split())
        return next((m for m in self.load(user_id)[0].values()
                     if not m.get("deleted") and " ".join(m.get("name", "").casefold().split()) == norm), None)
