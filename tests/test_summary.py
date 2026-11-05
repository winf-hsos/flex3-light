"""The one-page summary: what the model reads, and the two pages Typst sets from its answer."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import binaries, summary  # noqa: E402

VTT = """WEBVTT

00:00:01.000 --> 00:00:04.000
Today we look at analog and digital signals.

00:01:05.500 --> 00:01:09.000
Sampling picks points in time.

01:02:03.000 --> 01:02:05.000
Quantisation picks the levels.
"""


def test_material_puts_what_was_said_under_its_chapter():
    said = summary.cues(VTT)
    assert said == [(1.0, "Today we look at analog and digital signals."), (65.5, "Sampling picks points in time."),
                    (3723.0, "Quantisation picks the levels.")]
    text = summary.material([{"start": 0.0, "title": "Signals", "summary": "What a signal is."},
                             {"start": 60.0, "title": "Sampling"}], said)
    first, second = text.split("\n\n")
    assert first.startswith("## Chapter 1 at 0:00: Signals\nWhat a signal is.") and "analog and digital" in first
    assert second.startswith("## Chapter 2 at 1:00: Sampling") and "Quantisation" in second
    assert summary.clock(3723) == "1:02:03"


class FakeModel:
    model = "fake"

    def __init__(self, texts: dict):
        self.texts, self.bodies = texts, []

    def create(self, body: dict) -> dict:
        self.bodies.append(body)
        return {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(self.texts)}]}]}


def _texts(lang: str, words: int = 1) -> dict:
    filler = " ".join(["Inhalt" if lang == "de" else "content"] * words)
    return {"overview": f"Overview {lang}. {filler}", "points": [f"Point {i} {filler}" for i in range(7)],
            "terms": [{"term": f"Term {i}", "explanation": f"Explanation {filler}"} for i in range(8)],
            "notes": ["Aufgabe 2 ist bis zum 12. November abzugeben."] if lang == "de" else ["Hand in task 2."],
            "chapters": [f"{lang} chapter {i}" for i in range(20)]}


@pytest.mark.skipif(not shutil.which("typst"), reason="needs typst")
def test_run_writes_two_pages_that_fit(tmp_path):
    binaries.configure(typst=shutil.which("typst"))
    (tmp_path / "session.json").write_text(json.dumps({"course_name": "Modul", "recorded_at": "2026-11-04T09:50:00+01:00",
                                                       "topic": "Analog and Digital"}), encoding="utf-8")
    (tmp_path / "edit.json").write_text(json.dumps({"cards": {"title": {"title": "Analog und digital"}}}), encoding="utf-8")
    (tmp_path / "render.json").write_text(json.dumps({"signature": "abc", "duration": 7900.0,
                                                      "files": ["edited.mp4", "edited.en.vtt"]}), encoding="utf-8")
    (tmp_path / "chapters.json").write_text(json.dumps({"chapters": [
        {"start": i * 380.0, "title": f"Chapter {i}"} for i in range(20)]}), encoding="utf-8")
    (tmp_path / "edited.en.vtt").write_text(VTT, encoding="utf-8")
    model = FakeModel({"de": _texts("de", 12), "en": _texts("en", 12)})
    report = summary.run(tmp_path, model)
    assert "Sampling picks points in time." in model.bodies[0]["input"][0]["content"]
    assert model.bodies[0]["text"]["format"]["schema"] == summary.SCHEMA
    assert [f["page"] for f in report["fit"]] == [1, 2] and all(f["fits"] for f in report["fit"])
    pdf = (tmp_path / "summary.pdf").read_bytes()
    assert pdf.startswith(b"%PDF") and summary.current(tmp_path)
    assert report["de"]["notes"] and report["signature"] == "abc"
    (tmp_path / "render.json").write_text(json.dumps({"signature": "new"}), encoding="utf-8")
    assert not summary.current(tmp_path)                       # a new video wants a new summary


def test_run_needs_the_text_of_the_edited_video(tmp_path):
    (tmp_path / "render.json").write_text(json.dumps({"signature": "abc", "files": ["edited.mp4"]}), encoding="utf-8")
    with pytest.raises(ValueError):
        summary.run(tmp_path, FakeModel({}))
