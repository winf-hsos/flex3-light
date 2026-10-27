"""Keys in the Windows Credential Manager, and the key check without a usable key."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import credentials  # noqa: E402
import services  # noqa: E402

SERVICE = "pytest-selftest"


def test_store_replace_delete():
    credentials.delete(SERVICE)
    assert credentials.get(SERVICE) is None
    credentials.put(SERVICE, "geheim-äöü")
    assert credentials.get(SERVICE) == "geheim-äöü" and credentials.has(SERVICE)
    credentials.put(SERVICE, "neu")
    assert credentials.get(SERVICE) == "neu"
    credentials.delete(SERVICE)
    credentials.delete(SERVICE)               # deleting twice does no harm
    assert not credentials.has(SERVICE)


def test_check_reports_a_rejected_key():
    ok, message = services.check("openai", "sk-not-a-real-key")
    assert not ok and ("abgelehnt" in message or "nicht erreichbar" in message)
