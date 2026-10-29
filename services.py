"""The AI services the pipeline uses, their keys and a check whether a key works.

Keys come from the settings and live in the Windows Credential Manager (credentials.py),
not in environment variables. check() only asks the service who the key belongs to or
which models it may use; it sends no audio and no text.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import credentials

SERVICES = {"openai": "OpenAI", "elevenlabs": "ElevenLabs", "assemblyai": "AssemblyAI"}
# AssemblyAI keeps data in the US or, for accounts created there, in the EU; a key works in one region only.
ASSEMBLYAI_REGIONS = {"EU": "https://api.eu.assemblyai.com", "USA": "https://api.assemblyai.com"}
# Transcription models worth knowing about; whisper-1 is the only one with word timestamps
# and is shut down on 2027-02-26 (see docs/pipeline.md).
TRANSCRIBE_MODELS = ("gpt-transcribe", "whisper-1", "gpt-4o-transcribe", "gpt-4o-mini-transcribe")


def key(service: str) -> str | None:
    return credentials.get(service)


def status() -> dict[str, bool]:
    """Which services have a key; what the window may know."""
    return {s: credentials.has(s) for s in SERVICES}


def _get(url: str, headers: dict) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


_assemblyai_base: dict[str, str] = {}


def assemblyai_base(secret: str) -> str | None:
    """The address of the region that accepts this key (EU first), or None if none does."""
    if secret not in _assemblyai_base:
        check("assemblyai", secret)
    return _assemblyai_base.get(secret)


def check(service: str, secret: str | None = None) -> tuple[bool, str]:
    """(works, message for the window) for the given or the stored key."""
    secret = secret or key(service)
    if not secret:
        return False, "Kein Schlüssel hinterlegt."
    try:
        if service == "openai":
            code, body = _get("https://api.openai.com/v1/models", {"Authorization": f"Bearer {secret}"})
            if code == 200:
                ids = {m.get("id") for m in body.get("data", [])}
                found = [m for m in TRANSCRIBE_MODELS if m in ids]
                return True, ("Schlüssel gültig. Transkription: " + ", ".join(found)) if found else \
                    "Schlüssel gültig, aber kein Transkriptionsmodell freigegeben."
        elif service == "elevenlabs":
            code, body = _get("https://api.elevenlabs.io/v1/user", {"xi-api-key": secret})
            if code == 200:
                return True, "Schlüssel gültig."
            if "missing_permissions" in json.dumps(body):   # a restricted key: fine as long as it may make sounds
                return True, ("Schlüssel gültig und eingeschränkt. FLEX3 Light braucht nur das Recht „Sound Effects“; "
                              "fehlt es, meldet sich das beim Erzeugen der Klänge.")
        elif service == "assemblyai":
            for region, base in ASSEMBLYAI_REGIONS.items():
                code, _ = _get(f"{base}/v2/transcript?limit=1", {"Authorization": secret})
                if code == 200:
                    _assemblyai_base[secret] = base
                    return True, f"Schlüssel gültig, Daten bleiben in der Region {region}."
        else:
            raise ValueError(service)
    except OSError:
        return False, f"{SERVICES[service]} ist nicht erreichbar."
    if code == 401:
        return False, "Schlüssel abgelehnt: ungültig oder ohne Leserechte."
    if code == 403:
        return False, "Schlüssel gültig, aber für diese Abfrage nicht freigegeben."
    return False, f"{SERVICES[service]} antwortet mit Fehler {code}."
