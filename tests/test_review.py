"""Unsure words of a transcript: which are offered, and what a correction changes."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import review  # noqa: E402

WORDS = [("Heute", 0.99), ("geht", 0.98), ("es", 0.55), ("um", 0.97), ("Explosion,", 0.41), ("und", 0.52),
         ("das", 0.45), ("Quato.", 0.58), ("Fertig.", 0.99)]


def _session(tmp_path: Path) -> Path:
    words = [{"word": w, "start": i * 0.5, "end": i * 0.5 + 0.4, "confidence": c} for i, (w, c) in enumerate(WORDS)]
    transcript = {"language": "de", "words": words, "text": " ".join(w for w, _ in WORDS),
                  "segments": [{"start": 0.0, "end": 3.9, "text": " ".join(w for w, _ in WORDS[:8])},
                               {"start": 4.0, "end": 4.4, "text": "Fertig."}]}
    for name in ("transcript.json", "transcript.raw.json"):
        (tmp_path / name).write_text(json.dumps(transcript), encoding="utf-8")
    return tmp_path


def test_offers_long_or_very_unsure_words_long_first(tmp_path):
    t = json.loads((_session(tmp_path) / "transcript.json").read_text(encoding="utf-8"))
    spots = review.uncertain(t)
    assert [s["word"] for s in spots] == ["Explosion,", "Quato.", "das"]   # "es" and "und" are short and not very unsure
    assert spots[0]["before"] == "Heute geht es um" and spots[0]["after"].startswith("und das Quato.")


def test_correction_changes_text_and_subtitles_but_not_the_raw_transcript(tmp_path):
    folder = _session(tmp_path)
    w = review.correct(folder, 4, "Exposé")
    assert w["word"] == "Exposé," and w["corrected_from"] == "Explosion," and w["checked"]   # comma kept
    t = json.loads((folder / "transcript.json").read_text(encoding="utf-8"))
    assert t["segments"][0]["text"] == "Heute geht es um Exposé, und das Quato." and "Exposé," in t["text"]
    assert "Exposé," in (folder / "subtitles.de.vtt").read_text(encoding="utf-8")
    assert "Explosion," in (folder / "transcript.raw.json").read_text(encoding="utf-8")
    review.confirm(folder, 6)
    assert [s["word"] for s in review.uncertain(json.loads((folder / "transcript.json").read_text(encoding="utf-8")))] \
        == ["Quato."]
    assert review.term("Quarto.") == "Quarto" and review.term("„OpenCode“,") == "OpenCode"


def test_app_corrects_and_adds_the_term_to_the_module(tmp_path):
    import threading
    from types import SimpleNamespace
    import core
    from lecturer_profile import Profile
    folder = _session(tmp_path)
    (folder / "session.json").write_text(json.dumps({"session_id": "s1", "course_name": "Planung"}), encoding="utf-8")
    (folder / "pipeline.json").write_text(json.dumps({"steps": {"transcribe": {"state": "done"},
                                                                "subtitles": {"state": "done"}}}), encoding="utf-8")
    c = core.Core.__new__(core.Core)                     # no OBS: only what review_word needs
    c._library = {"s1": folder}
    c.profile = Profile(tmp_path / "profile.json")
    module = c.profile.add("Planung", terms=["Forschungsfrage"])
    c.delivery = SimpleNamespace(wake=threading.Event())
    r = c.review_word("s1", 7, "Quarto", as_term=True)
    assert r["ok"] and "Quarto" in r["message"]
    assert c.profile.find("Planung")["terms"] == ["Forschungsfrage", "Quarto"] and c.delivery.wake.is_set()
    assert c.review_word("s1", 6, "das")["ok"]             # unchanged: confirmed
    t = json.loads((folder / "transcript.json").read_text(encoding="utf-8"))
    assert t["words"][7]["word"] == "Quarto." and t["words"][6]["checked"] and t["words"][6]["word"] == "das"
    assert module["id"] == c.profile.find("Planung")["id"]


def test_files_wait_while_windows_holds_the_file(tmp_path, monkeypatch):
    from pipeline import files
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("in use")
        return "ok"
    monkeypatch.setattr(files.time, "sleep", lambda s: None)
    assert files.retry(flaky) == "ok" and calls["n"] == 3
    files.write_json(tmp_path / "a.json", {"x": 1})
    assert files.read_json(tmp_path / "a.json") == {"x": 1} and files.read_json(tmp_path / "missing.json", []) == []
