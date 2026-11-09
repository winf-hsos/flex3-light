"""The lecturer's profile on this machine: modules and preferences, kept locally and synced with the server.

The app always works on this local copy, with or without a server. Changes are saved
at once and sent when the server can be reached; what other devices of the same
lecturer changed comes back the same way. Every module is an entry with a stable id,
an updated_at time and a deleted flag, merged entry by entry with the same rules as
server/flex3_light_server/profiles.py: the newer change wins, and a deletion stays
as a tombstone.

On the very first start the profile is seeded from what already exists: the module
folders of the current term in the archive and an optional Markdown list of modules.
Seeded entries are dated 0, so whatever the server already knows wins, and a seeded
entry whose name the server already has is dropped instead of becoming a duplicate.

Preferences are everything else that belongs to the lecturer and not to this computer
(decision of 2026-11-06): the cut settings ("cut.<field>"), what starts after a
recording, the default language. Each key carries its own updated_at and is merged on
its own, so a change on one device reaches the others without overwriting settings
they changed meanwhile. Settings that came from settings.json before the profile had
preferences are taken over dated 0, like seeded modules.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

FIELDS = ("name", "code", "language", "ilias_course_id", "opencast_series_id", "terms")
SYNC_EVERY = 300            # seconds between syncs when nothing changed locally


def _newer(a: dict, b: dict) -> dict:
    """Same rule as the server: the later change wins, equal times are decided by content."""
    if a["updated_at"] != b["updated_at"]:
        return a if a["updated_at"] > b["updated_at"] else b
    wire = lambda e: json.dumps({k: v for k, v in e.items() if k != "seed"}, sort_keys=True)  # noqa: E731
    return a if wire(a) >= wire(b) else b


def _norm(name: str) -> str:
    return " ".join(name.casefold().split())


def code_for(text: str) -> str:
    t = text.lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        t = t.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:40].strip("-")


def seed_entries(archive: Path, term: str, module_file: str | None) -> list[dict]:
    """Modules that already exist: folders of the term in the archive, then a Markdown list.

    In the Markdown list every module is a "## <name>" section; "kennung:" or "kuerzel:"
    gives the code, and "Englisch" or "Deutsch" in an "art:" line the language.
    """
    found: dict[str, dict] = {}
    if module_file and Path(module_file).exists():
        text = Path(module_file).read_text(encoding="utf-8")
        for m in re.finditer(r"^## (.+)$\n((?:(?!^## ).*\n?)*)", text, re.M):
            name, body = m.group(1).strip(), m.group(2)
            if name == "Wie ein Modul beschrieben wird":
                continue
            field = lambda key: (re.search(rf"^{key}:\s*(.+)$", body, re.M) or [None, ""])[1].strip()  # noqa: E731
            art = field("art").lower()
            found[_norm(name)] = {"name": name, "code": code_for(field("kennung") or field("kuerzel")),
                                  "language": "en" if "englisch" in art else "de"}
    folder = archive / term
    if folder.exists():
        for p in sorted(folder.iterdir()):
            if p.is_dir() and _norm(p.name) not in found:
                found[_norm(p.name)] = {"name": p.name, "code": code_for(p.name), "language": "de"}
    return sorted(found.values(), key=lambda e: _norm(e["name"]))


class Profile:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.data = {"user_id": "", "user_name": "", "modules": {}, "revision": 0, "synced_revision": -1,
                         "synced_at": 0}
        self.data.setdefault("preferences", {})
        self.status = "local"     # "local" (no server), "online", "offline" or "no-profile"

    @property
    def is_new(self) -> bool:
        return not self.data["modules"] and not self.data["synced_at"]

    def save(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)

    # ---------------------------------------------------------- reading

    def modules(self) -> list[dict]:
        with self.lock:
            return sorted((dict(e) for e in self.data["modules"].values() if not e["deleted"]),
                          key=lambda e: _norm(e["name"]))

    def find(self, name: str) -> dict | None:
        return next((e for e in self.modules() if _norm(e["name"]) == _norm(name)), None)

    def preference(self, key: str, default=None):
        with self.lock:
            item = self.data["preferences"].get(key)
            return default if item is None else item["value"]

    def preferences(self, prefix: str = "") -> dict:
        """The values of all preferences whose key starts with prefix, without the prefix."""
        with self.lock:
            return {k[len(prefix):]: v["value"] for k, v in self.data["preferences"].items() if k.startswith(prefix)}

    def user(self) -> dict:
        return {"user_id": self.data["user_id"], "user_name": self.data["user_name"]}

    def dirty(self) -> bool:
        return self.data["revision"] != self.data["synced_revision"]

    def due(self) -> bool:
        return self.dirty() or time.time() - self.data["synced_at"] > SYNC_EVERY

    # ---------------------------------------------------------- changes

    def _changed(self, entry: dict) -> dict:
        with self.lock:
            entry["updated_at"] = max(time.time(), entry.get("updated_at", 0) + 0.001)
            entry.pop("seed", None)           # once edited by hand, a seed is a real entry
            self.data["modules"][entry["id"]] = entry
            self.data["revision"] += 1
            self.save()
            return dict(entry)

    def add(self, name: str, **fields) -> dict:
        entry = {"id": uuid.uuid4().hex, "name": name.strip(), "code": "", "language": "de",
                 "ilias_course_id": "", "opencast_series_id": "", "terms": [], "deleted": False}
        entry.update({k: v for k, v in fields.items() if k in FIELDS})
        if not entry["code"]:
            entry["code"] = code_for(entry["name"])
        return self._changed(entry)

    def update(self, eid: str, **fields) -> dict:
        with self.lock:
            entry = dict(self.data["modules"][eid])
            entry.update({k: v.strip() if isinstance(v, str) else v for k, v in fields.items() if k in FIELDS})
            return self._changed(entry)

    def delete(self, eid: str) -> None:
        with self.lock:
            entry = dict(self.data["modules"][eid], deleted=True)
            self._changed(entry)

    def set_preferences(self, values: dict) -> bool:
        """Change preferences; only keys whose value really changes get a new time. True if any did."""
        with self.lock:
            prefs, changed = self.data["preferences"], False
            for key, value in values.items():
                old = prefs.get(key)
                if old is not None and json.dumps(old["value"], sort_keys=True) == json.dumps(value, sort_keys=True):
                    continue
                prefs[key] = {"value": value, "updated_at": max(time.time(), (old or {}).get("updated_at", 0) + 0.001)}
                changed = True
            if changed:
                self.data["revision"] += 1
                self.save()
            return changed

    def seed_preferences(self, values: dict) -> None:
        """Take over settings from before the profile had preferences, dated 0 so the server wins."""
        with self.lock:
            new = {k: v for k, v in values.items() if k not in self.data["preferences"]}
            for key, value in new.items():
                self.data["preferences"][key] = {"value": value, "updated_at": 0.0}
            if new:
                self.data["revision"] += 1
                self.save()

    def seed(self, entries: list[dict]) -> None:
        """First start only: take over what already exists, dated 0 so the server wins."""
        with self.lock:
            for e in entries:
                eid = uuid.uuid4().hex
                self.data["modules"][eid] = {"id": eid, "name": e["name"], "code": e.get("code", ""),
                                             "language": e.get("language", "de"), "ilias_course_id": "",
                                             "opencast_series_id": "", "terms": [], "deleted": False, "updated_at": 0.0,
                                             "seed": True}
            self.data["revision"] += 1
            self.save()

    # ---------------------------------------------------------- sync

    def sync(self, client) -> None:
        """Send all entries to the server and take over what comes back. Raises on network errors."""
        with self.lock:
            first = not self.data["synced_at"]
            revision = self.data["revision"]
        if first:
            self._apply(client.get_profile(), drop_seeds=True)
        with self.lock:
            wire = [{k: v for k, v in e.items() if k != "seed"} for e in self.data["modules"].values()]
            prefs = {k: dict(v) for k, v in self.data["preferences"].items()}
        self._apply(client.sync_profile(wire, prefs), drop_seeds=False, revision=revision)

    def _apply(self, answer: dict, drop_seeds: bool, revision: int | None = None) -> None:
        with self.lock:
            if self.data["user_id"] and answer["user_id"] != self.data["user_id"]:
                # another lecturer's token: keep the old profile next to it and start over
                os.replace(self.path, self.path.with_name(f"profile-{code_for(self.data['user_id'])}.json"))
                self.data.update(modules={}, preferences={}, revision=0, synced_revision=-1)
            self.data["user_id"], self.data["user_name"] = answer["user_id"], answer.get("user_name", "")
            mods = self.data["modules"]
            theirs = answer.get("modules", [])
            if drop_seeds:
                known = {_norm(e["name"]) for e in theirs if not e["deleted"]}
                for eid in [i for i, e in mods.items() if e.get("seed") and _norm(e["name"]) in known]:
                    del mods[eid]
            for e in theirs:
                mine = mods.get(e["id"])
                mods[e["id"]] = e if mine is None else _newer(mine, e)
            prefs = self.data["preferences"]
            for key, item in (answer.get("preferences") or {}).items():   # a server before 2026-11-09 sends none
                prefs[key] = item if key not in prefs else _newer(prefs[key], item)
            if revision is not None:
                self.data["synced_revision"] = revision
                self.data["synced_at"] = time.time()
                for e in mods.values():
                    e.pop("seed", None)           # the server has it now; it is an entry like any other
            self.save()
