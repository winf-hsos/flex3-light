"""The look of every video the server renders, kept in one place (phase 6 of the cut agent's plan).

A folder style/ in the server's data folder holds style.json and the files it names: a logo, fonts,
sounds, a short intro and outro video. What it leaves out keeps DEFAULT, which is the look the videos
had before there was a style. One style applies to the long version and to the clips, so a change of
style changes every video rendered after it, without touching a single edit list. The worker renders
again what was rendered in an earlier style and is not yet published (server worker.py); a published
video shows it in the window and is rendered again by hand.

style.json, every field optional:

    {"name": "Hochschule Osnabrück",
     "colors": {"background": "#009ee3", "accent": "#006e9f", "text": "#ffffff"},
     "font": {"regular": "segoeui.ttf", "semibold": "seguisb.ttf"},
     "logo": "logo.png",
     "cards": {"title": 4.0, "chapter": 2.0, "closing": 4.0, "fade": 0.25},
     "lower_third": {"seconds": 5.0},
     "sounds": {"title": "intro", "chapter": "swoosh", "closing": "outro", "volume": 0.7},
     "intro": "intro.mp4", "outro": "outro.mp4",
     "cut_fade_ms": 10,
     "captions": {"highlight": "#009ee3", "size": 4.5, "box": 0.6}}

Files are named relative to the style folder and must lie inside it. A font that is no file there is
looked up among the system's fonts. A sound is a file <name>.mp3 or <name>.wav in the style folder,
else one of the sounds made once with ElevenLabs (sounds.py). The intro plays before the title card,
the outro after the closing card, both scaled to the size of the recording. "captions" is the look of
captions burned into the picture (languages.py): the color of the word being spoken, the size of the
type in percent of the picture's height, and how opaque the dark box behind the lines is.

Since 2026-11-13 the window changes the name, the colors, the captions and the logo (save(),
set_logo()); who may do that is the server's business (my_recordings.py).
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

DEFAULT = {
    "name": "FLEX3 Light",
    "colors": {"background": "#009ee3", "accent": "#006e9f", "text": "#ffffff"},
    "font": {"regular": "segoeui.ttf", "semibold": "seguisb.ttf"},
    "logo": "",                          # empty: the FLEX3 mark, a rounded square with a dot
    "cards": {"title": 4.0, "chapter": 2.0, "closing": 4.0, "fade": 0.25},
    "lower_third": {"seconds": 5.0},     # how long a chapter title stays when chapters are lower thirds
    "sounds": {"title": "intro", "chapter": "swoosh", "closing": "outro", "volume": 0.7},
    "intro": "",
    "outro": "",
    "cut_fade_ms": 10,                   # the sound fades in and out this long at every cut, so it does not click
    "captions": {"highlight": "#009ee3", "size": 4.5, "box": 0.6},   # burned-in captions (languages.py)
}
LIMITS = {("cards", "title"): (1.0, 15.0), ("cards", "chapter"): (0.5, 10.0), ("cards", "closing"): (1.0, 15.0),
          ("cards", "fade"): (0.0, 1.0), ("lower_third", "seconds"): (1.0, 20.0), ("sounds", "volume"): (0.0, 1.5),
          ("cut_fade_ms",): (0.0, 50.0), ("captions", "size"): (2.5, 8.0), ("captions", "box"): (0.0, 1.0)}
FILE = "style.json"
NOT_IN_FINGERPRINT = ("captions",)      # only the burned-in captions use it, and they have their own signature
LOGO_TYPES = {"png": bytes.fromhex("89504e470d0a1a0a"), "jpg": bytes.fromhex("ffd8ff")}
LOGO_BYTES = 2_000_000


def _color(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value) is not None


def inside(folder: Path | None, name: str) -> Path | None:
    """A file named in the style, if it exists inside the style folder."""
    if not folder or not name:
        return None
    path = (Path(folder) / name).resolve()
    root = Path(folder).resolve()
    return path if path.is_file() and (path == root or root in path.parents) else None


def load(folder: Path | None) -> dict:
    """The style of a server: style.json in folder over DEFAULT, checked. "folder" says where its files
    are, "problems" what was left at its default and why (German, for the person who keeps the style)."""
    s = copy.deepcopy(DEFAULT)
    problems: list[str] = []
    raw: dict = {}
    if folder and (Path(folder) / FILE).exists():
        try:
            raw = json.loads((Path(folder) / FILE).read_text(encoding="utf-8-sig"))
            if not isinstance(raw, dict):
                raise ValueError("kein Objekt")
        except ValueError as ex:
            problems.append(f"{FILE} nicht lesbar ({ex}); es gilt der Grundstil")
            raw = {}
    for key, value in raw.items():
        if key not in DEFAULT:
            problems.append(f"unbekanntes Feld „{key}“")
        elif isinstance(DEFAULT[key], dict):
            if not isinstance(value, dict):
                problems.append(f"„{key}“ ist kein Objekt")
                continue
            for k, v in value.items():
                if k not in DEFAULT[key]:
                    problems.append(f"unbekanntes Feld „{key}.{k}“")
                else:
                    s[key][k] = v
        else:
            s[key] = value
    for k, v in s["colors"].items():
        if not _color(v):
            problems.append(f"Farbe „{k}“ ist kein #rrggbb: {v!r}")
            s["colors"][k] = DEFAULT["colors"][k]
    if not _color(s["captions"]["highlight"]):
        problems.append(f"Farbe „captions.highlight“ ist kein #rrggbb: {s['captions']['highlight']!r}")
        s["captions"]["highlight"] = DEFAULT["captions"]["highlight"]
    for path, (lo, hi) in LIMITS.items():
        parent = s if len(path) == 1 else s[path[0]]
        default = DEFAULT[path[0]] if len(path) == 1 else DEFAULT[path[0]][path[1]]
        try:
            value = float(parent[path[-1]])
        except (TypeError, ValueError):
            problems.append(f"„{'.'.join(path)}“ ist keine Zahl")
            value = default
        if not lo <= value <= hi:
            problems.append(f"„{'.'.join(path)}“ liegt nicht zwischen {lo:g} und {hi:g}")
            value = min(hi, max(lo, value))
        parent[path[-1]] = value
    for key in ("chapter", "title", "closing"):
        s["cards"]["fade"] = min(s["cards"]["fade"], s["cards"][key] / 2)
    for key in ("name", "logo", "intro", "outro"):
        s[key] = str(s[key] or "").strip()
    for key in ("regular", "semibold"):
        s["font"][key] = str(s["font"][key] or DEFAULT["font"][key]).strip()
    for key in ("title", "chapter", "closing"):
        s["sounds"][key] = str(s["sounds"][key] or "").strip()
    for key in ("logo", "intro", "outro"):
        if s[key] and inside(folder, s[key]) is None:
            problems.append(f"„{key}“: {s[key]} liegt nicht im Stilordner")
            s[key] = ""
    s["folder"] = str(folder) if folder else ""
    s["problems"] = problems
    return s


def fingerprint(s: dict) -> str:
    """Changes whenever the style or one of its files changes."""
    folder = Path(s["folder"]) if s.get("folder") else None
    named = [s["logo"], s["intro"], s["outro"], s["font"]["regular"], s["font"]["semibold"]]
    named += [f"{x}.{ext}" for x in s["sounds"].values() if isinstance(x, str) and x for ext in ("mp3", "wav")]
    stamps = []
    for name in sorted(set(n for n in named if n)):
        path = inside(folder, name)
        if path is not None:
            st = path.stat()
            stamps.append((name, st.st_size, st.st_mtime_ns))
    body = {k: s[k] for k in DEFAULT if k not in NOT_IN_FINGERPRINT}   # not where it lies, nor what render.py measured
    return hashlib.sha1(json.dumps([body, stamps], sort_keys=True).encode()).hexdigest()[:12]


def rgb(s: dict, key: str) -> tuple[int, int, int]:
    value = s["colors"][key]
    return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)


def font(s: dict, weight: str = "regular") -> str:
    """The font file for a weight: one in the style folder, else the name for the system to find."""
    name = s["font"]["semibold" if weight in ("semibold", "bold") else "regular"]
    path = inside(Path(s["folder"]) if s.get("folder") else None, name)
    return str(path) if path else name


def sound(s: dict, moment: str, sound_dir: Path | None) -> tuple[str | None, Path | None]:
    """(name, file) of the sound for a card ("title", "chapter", "closing"): the style's own file, else
    the generated one; file None if there is none (the card stays silent)."""
    name = s["sounds"].get(moment) or None
    if not name:
        return None, None
    folder = Path(s["folder"]) if s.get("folder") else None
    for ext in ("mp3", "wav"):
        own = inside(folder, f"{name}.{ext}")
        if own:
            return name, own
    made = Path(sound_dir) / f"{name}.mp3" if sound_dir else None
    return name, made if made and made.exists() else None


def asset(s: dict, key: str) -> Path | None:
    """The file of the style's logo, intro or outro, if it has one."""
    return inside(Path(s["folder"]) if s.get("folder") else None, s.get(key, ""))


# ---------------------------------------------------------------- changed from the window (2026-11-13)

EDITABLE = {"name": None, "colors": ("background", "accent", "text"), "captions": ("highlight", "size", "box")}


def _raw(folder: Path) -> dict:
    try:
        raw = json.loads((Path(folder) / FILE).read_text(encoding="utf-8-sig"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(folder: Path, raw: dict) -> None:
    Path(folder).mkdir(parents=True, exist_ok=True)
    tmp = Path(folder) / (FILE + ".tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(Path(folder) / FILE)


def save(folder: Path, changes: dict) -> dict:
    """Write the name, colors and captions of changes into style.json in folder, keeping every other field
    of it (fonts, sounds, intro, outro, which are kept by hand); returns the style. Raises ValueError for
    a value the style would not accept, so a wrong click never leaves a broken style behind."""
    raw = _raw(folder)
    for key, fields in EDITABLE.items():
        if key not in (changes or {}):
            continue
        value = changes[key]
        if fields is None:
            raw[key] = " ".join(str(value or "").split())[:80] or DEFAULT[key]
            continue
        if not isinstance(value, dict):
            raise ValueError(f"{key} ist kein Objekt")
        part = dict(raw.get(key) or {})
        for k, v in value.items():
            if k not in fields:
                raise ValueError(f"unbekanntes Feld {key}.{k}")
            if (key == "colors" or k == "highlight") and not _color(v):
                raise ValueError(f"{key}.{k} ist keine Farbe #rrggbb")
            if (key, k) in LIMITS:
                lo, hi = LIMITS[(key, k)]
                v = float(v)
                if not lo <= v <= hi:
                    raise ValueError(f"{key}.{k} liegt nicht zwischen {lo:g} und {hi:g}")
            part[k] = v
        raw[key] = part
    _write(folder, raw)
    return load(folder)


def set_logo(folder: Path, data: bytes | None, kind: str = "png") -> dict:
    """Put a logo (PNG or JPEG, at most LOGO_BYTES) into the style folder and name it in style.json; with no
    data the logo goes and the cards show the FLEX3 mark again. Returns the style."""
    raw = _raw(folder)
    old = inside(folder, str(raw.get("logo") or ""))
    if data:
        if kind not in LOGO_TYPES or not data.startswith(LOGO_TYPES[kind]):
            raise ValueError("Das Logo muss ein PNG- oder JPEG-Bild sein.")
        if len(data) > LOGO_BYTES:
            raise ValueError("Das Logo ist größer als 2 MB.")
        Path(folder).mkdir(parents=True, exist_ok=True)
        name = f"logo.{kind}"
        tmp = Path(folder) / (name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(Path(folder) / name)
        raw["logo"] = name
        if old is not None and old.name != name:
            old.unlink(missing_ok=True)
    else:
        raw.pop("logo", None)
        if old is not None and old.name.startswith("logo."):
            old.unlink(missing_ok=True)
    _write(folder, raw)
    return load(folder)
