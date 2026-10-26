"""Profiles per lecturer: the modules they record, merged entry by entry.

Every module is an entry with a stable id, an updated_at time and a deleted flag.
Two devices that changed the profile offline are merged entry by entry: the newer
change of an entry wins, and a deletion stays as a tombstone so it is not brought
back by a device that still has the old entry. The same merge runs on the server
and in the app (lecturer_profile.py), so both always end up with the same list.
"""
from __future__ import annotations

import json
import re

LANGUAGE = re.compile(r"^[a-z]{2}$")
CODE = re.compile(r"^[a-z0-9-]{0,40}$")
TEXT_FIELDS = {"name": 200, "ilias_course_id": 100, "opencast_series_id": 100}


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
