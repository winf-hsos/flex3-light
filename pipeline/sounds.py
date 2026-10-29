"""The small library of sounds the cut uses, made once with ElevenLabs.

A swoosh for chapter cards, a short opening for the title card and a short ending for
the closing card. They are made once per installation with ElevenLabs' sound generation
(the key from the settings), kept in the app's folder and reused for every recording; a
sound that is missing simply stays silent. Each is kept short and quiet enough to sit
under a card, never under speech.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

URL = "https://api.elevenlabs.io/v1/sound-generation"
LIBRARY = {
    "swoosh": ("A soft, short airy swoosh transition sound, like a page sliding past, clean and modern, "
               "no music", 1.2),
    "intro": ("A short, friendly and calm opening jingle for a university lecture video: warm soft synth "
              "chord with a light bright bell, modern, unobtrusive, ends gently", 4.0),
    "outro": ("A short, calm closing jingle for a university lecture video: warm soft synth chord resolving, "
              "light bell, modern, fades out gently", 4.0),
}


class SoundError(RuntimeError):
    pass


def path(folder: Path, name: str) -> Path:
    return Path(folder) / f"{name}.mp3"


def available(folder: Path) -> dict[str, Path]:
    return {name: path(folder, name) for name in LIBRARY if path(folder, name).exists()}


def make(folder: Path, key: str, names: list[str] | None = None, timeout: float = 120) -> list[str]:
    """Generate the missing sounds (or the named ones again); returns what was made."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    made = []
    for name in names or [n for n in LIBRARY if not path(folder, n).exists()]:
        prompt, seconds = LIBRARY[name]
        body = json.dumps({"text": prompt, "duration_seconds": seconds, "prompt_influence": 0.6}).encode("utf-8")
        req = urllib.request.Request(URL, data=body, method="POST",
                                     headers={"xi-api-key": key, "Content-Type": "application/json",
                                              "Accept": "audio/mpeg"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise SoundError(f"ElevenLabs antwortet mit Fehler {e.code}: {detail}") from None
        if len(data) < 1000:
            raise SoundError(f"ElevenLabs hat für {name} keinen Klang geliefert")
        tmp = path(folder, name).with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path(folder, name))
        made.append(name)
    return made
