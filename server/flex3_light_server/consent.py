"""Consent in session.json: whether a recording may be published.

Decision of 2026-11-17: every recording arrives unreleased, whatever the device sends. The
lecturer looks at it after the cut and releases it on the server's page, recording by recording
("Veröffentlichen"). The source is "register": the release is the lecturer's, given outside the
device, and the contract needs no new value. Recording is allowed by starting it, a cloned
voice is never allowed here.
"""
from __future__ import annotations

from pathlib import Path

from .worker import ROOT  # noqa: F401  (puts pipeline/ on the path)
from pipeline import files  # noqa: E402


def unreleased() -> dict:
    """The consent fields of a newly arrived recording."""
    return {"consent": {"recording": True, "publication": False, "voice_clone": False}, "consent_source": "register"}


def release(folder: Path) -> bool:
    """The lecturer releases the recording for publication; returns whether session.json changed."""
    meta_file = Path(folder) / "session.json"
    meta = files.read_json(meta_file, {})
    want = {"consent": {"recording": True, "publication": True, "voice_clone": False}, "consent_source": "register"}
    if all(meta.get(k) == v for k, v in want.items()):
        return False
    files.write_json(meta_file, {**meta, **want})
    return True
