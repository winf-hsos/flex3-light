"""Privacy locks: what the safety net found, and whether the edit list hides it.

The safety net (screening.py) writes privacy.json with locks and candidates. A lock is a
span of the recording in which something private is visible or audible: a mail address, a
key or password, a student number, a voice from the room, a span the lecturer marked with
the privacy key, a mailbox in front. Locks are hard (decision of the project lead,
2026-11-10): an edit list is not approved, and nothing is rendered, while a lock is still
visible or audible in what stays, unless the lecturer lifts it by hand at the approval.
Candidates are weaker hints; they never block.

A lock is hidden when every part of its span that stays in the video (timeline.plan, so a
pause's card, silent and showing only the card, or its shortened ends count right) is
- for sound: under an accepted "mute";
- for the picture: under an accepted "blur" whose region contains the lock's region (the
  whole frame if the lock has none).
Removing the span hides both.
"""
from __future__ import annotations

from pathlib import Path

from . import edit, files, timeline

KINDS = {
    "email": "Mailadresse im Bild",
    "secret": "Schlüssel oder Passwort im Bild",
    "student_number": "Matrikelnummer im Bild",
    "number_list": "Liste mit Nummern im Bild",
    "name_list": "Namensliste im Bild",
    "room_voice": "Stimme aus dem Raum",
    "privacy_key": "Mit der Privat-Taste markiert",
    "mailbox": "Postfach im Vordergrund",
    "number": "Lange Nummer im Bild",
}
SENSES = ("picture", "sound", "both")
SLACK = 0.25                     # seconds of a lock that may stay uncovered at its edges (frame rounding)
EDGE = 0.01                      # how far a blur may fall short of a lock's region


def load(folder: Path) -> dict:
    """privacy.json, or an empty one for a recording the safety net has not seen."""
    return files.read_json(Path(folder) / "privacy.json", {}) or {"locks": [], "candidates": []}


def mask(text: str, kind: str = "") -> str:
    """What a lock shows of the text it found: enough to recognise it, not enough to read it. Of a
    key or password only its label stays ("Token: La…")."""
    out = []
    for word in text.split():
        if kind == "secret" and not word.endswith((":", "=")):
            label, sep, value = word.partition("=")
            out.append(f"{label}={value[:2]}…" if sep and value else word[:2] + "…")
        elif "@" in word:
            name, _, domain = word.partition("@")
            head, _, tld = domain.rpartition(".")
            out.append(f"{name[:1]}…@{head[:1]}….{tld}" if head else f"{name[:1]}…@…")
        elif sum(ch.isdigit() for ch in word) >= 4:
            out.append(word[:2] + "…" + word[-1:])
        elif len(word) > 12:
            out.append(word[:4] + "…")
        else:
            out.append(word)
    return " ".join(out)


def _uncovered(a: float, b: float, cover: list[tuple[float, float]]) -> list[tuple[float, float]]:
    rest, t = [], a
    for c0, c1 in sorted(cover):
        if c1 <= t:
            continue
        if c0 > t:
            rest.append((t, min(c0, b)))
        t = max(t, c1)
        if t >= b:
            break
    if t < b:
        rest.append((t, b))
    return [(x, y) for x, y in rest if y - x > 1e-6]


def _contains(outer, inner) -> bool:
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    return ox <= ix + EDGE and oy <= iy + EDGE and ox + ow >= ix + iw - EDGE and oy + oh >= iy + ih - EDGE


def visible(lock: dict, e: dict) -> list[tuple[float, float]]:
    """The parts of a lock's span that stay in the video without being hidden."""
    a, b = lock["start"], lock["end"]
    plan = timeline.plan(e)
    stays = [(max(a, k0), min(b, k1)) for k0, k1 in plan["kept"] if k1 > a and k0 < b]
    cards = [(c0, c1) for c0, c1, _text in plan["cards"]]          # a pause's card: silent, and only the card is seen
    stays = [p for s0, s1 in stays for p in _uncovered(s0, s1, cards)]
    accepted = [x for x in e["items"] if x["state"] == "accepted" and x["end"] is not None]
    region = lock.get("region") or [0.0, 0.0, 1.0, 1.0]
    hides = []
    if lock["sense"] in ("sound", "both"):
        hides.append([(x["start"], x["end"]) for x in accepted if x["kind"] == "mute"])
    if lock["sense"] in ("picture", "both"):
        hides.append([(x["start"], x["end"]) for x in accepted
                      if x["kind"] == "blur" and x["params"].get("region") and _contains(x["params"]["region"], region)])
    left: list[tuple[float, float]] = []         # audible or visible: what any sense leaves open
    for cover in hides:
        left += [p for s0, s1 in stays for p in _uncovered(s0, s1, cover)]
    merged: list[list[float]] = []
    for x, y in sorted(left):
        if merged and x <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], y)
        else:
            merged.append([x, y])
    total = sum(y - x for x, y in merged)
    return [] if total <= SLACK else [(x, y) for x, y in merged]


def state(folder: Path, e: dict | None = None) -> list[dict]:
    """Every lock with its state: "hidden", "lifted" (by the lecturer) or "open"."""
    e = e if e is not None else edit.load(folder)
    lifted = (e or {}).get("lifted", {})
    out = []
    for lock in load(folder).get("locks", []):
        if lock["id"] in lifted:
            status = "lifted"
        elif e is not None and not visible(lock, e):
            status = "hidden"
        else:
            status = "open"
        out.append({**lock, "label": KINDS.get(lock["kind"], lock["kind"]), "status": status})
    return out


def open_locks(folder: Path, e: dict | None = None) -> list[dict]:
    return [x for x in state(folder, e) if x["status"] == "open"]


def _outward(t: float, words: list[dict], later: bool) -> float:
    """A bound moved out of a word so that the whole word is inside the span."""
    for w in words:
        if w["start"] < t < w["end"]:
            return round(w["end"] + 0.05 if later else max(0.0, w["start"] - 0.05), 3)
    return round(t, 3)


def cover_item(lock: dict, words: list[dict], source: str = "lecturer") -> dict:
    """The item that hides a lock: mute for a voice, blur for text in the picture, a cut for the rest."""
    a = _outward(lock["start"], words, later=False)
    b = _outward(lock["end"], words, later=True)
    reason = KINDS.get(lock["kind"], "Datenschutz")
    if lock["sense"] == "sound":
        return edit.item("mute", a, b, reason, source, 0.9, "accepted", "privacy", lock=lock["id"])
    if lock["sense"] == "picture" and lock.get("region"):
        x, y, w, h = lock["region"]
        pad = 0.01
        region = [round(max(0.0, x - pad), 4), round(max(0.0, y - pad), 4),
                  round(min(1.0 - max(0.0, x - pad), w + 2 * pad), 4), round(min(1.0 - max(0.0, y - pad), h + 2 * pad), 4)]
        return edit.item("blur", a, b, reason, source, 0.9, "accepted", "privacy", region=region, lock=lock["id"])
    return edit.item("cut", a, b, reason, source, 0.9, "accepted", "privacy", lock=lock["id"])


def lines(folder: Path, e: dict | None = None) -> list[str]:
    """The locks for the cut agent, one line each."""
    out = []
    for x in state(folder, e):
        where = f" region {x['region']}" if x.get("region") else ""
        out.append(f"{x['id']} {x['kind']} ({x['sense']}) {x['start']:.1f}-{x['end']:.1f}{where} [{x['status']}]"
                   + (f" {x['text']}" if x.get("text") else ""))
    return out
