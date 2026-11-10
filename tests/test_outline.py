"""Stage C: the outline of a recording from its overview (pipeline/outline.py)."""
import json
from pathlib import Path

import library
from pipeline import cut_agent, cut_tools, edit, llm, outline

DURATION = 1800.0
SEGMENTS = [{"start": float(t), "end": float(t) + 25.0, "text": f"Satz bei {t}."} for t in range(10, 1790, 30)]


def _overview() -> dict:
    blocks = [{"start": float(t), "end": float(t + 60), "clock": f"{t // 60}:00", "text": f"Gesagt ab {t}.",
               "words": 120, "speech": 40.0, "silence": 20.0, "level": -36.5, "changes": 1 if t == 300 else 0,
               "slides": [{"t": 300.0, "title": "Was ist digital?"}] if t == 300 else [],
               "programs": ["chrome.exe"], "private": False, "room_words": 4 if t == 600 else 0}
              for t in range(0, 1800, 60)]
    return {"duration": DURATION, "blocks": blocks, "pauses": [[900.0, 960.0]], "changes": [300.0],
            "slides": [{"t": 300.0, "title": "Was ist digital?"}, {"t": 420.0, "title": "Analog und digital"},
                       {"t": 1500.0, "title": "Zum Schluss"}],
            "programs": [{"program": "chrome.exe", "seconds": 1800.0}]}


def _folder(tmp_path: Path) -> Path:
    f = tmp_path / "s"
    f.mkdir()
    (f / "overview.json").write_text(json.dumps(_overview()), encoding="utf-8")
    (f / "transcript.json").write_text(json.dumps({"language": "de", "duration": DURATION, "words": [],
                                                   "segments": SEGMENTS}), encoding="utf-8")
    (f / "session.json").write_text(json.dumps({"course_name": "DuP", "duration_sec": DURATION}), encoding="utf-8")
    return f


GOOD = {"phases": [{"start": 3, "kind": "idle", "title": "Ankommen"},
                   {"start": 290, "kind": "input", "title": "Folien zu digital"},
                   {"start": 292, "kind": "input", "title": "noch einmal"},            # same kind: continues
                   {"start": 905, "kind": "exercise", "title": "Übung"},
                   {"start": 1500, "kind": "input", "title": "Abschluss"}],
        "topics": [{"start": 295, "end": 600, "title": "Was ist digital?", "summary": "Begriff."},
                   {"start": 600, "end": 896, "title": "Analog und digital", "summary": "Vergleich."},
                   {"start": 1495, "end": 1790, "title": "Zum Schluss", "summary": "Rückblick."}],
        "inputs": [{"start": 295, "end": 896, "title": "Digital und analog", "summary": "Der Input."}],
        "summary": "Erst Folien, dann eine Übung."}


def test_check_snaps_to_sentences_and_covers_the_recording():
    result, errors = outline.check(GOOD, DURATION, SEGMENTS)
    assert errors == []
    phases = result["phases"]
    assert [p["kind"] for p in phases] == ["idle", "input", "exercise", "input"]
    assert phases[0]["start"] == 0 and phases[-1]["end"] == DURATION
    assert all(p["end"] == q["start"] for p, q in zip(phases, phases[1:]))      # no gaps
    assert phases[1]["start"] == 280.0                                          # snapped to a sentence start
    assert result["topics"][0]["start"] == 280.0 and result["topics"][1]["end"] == 905.0   # to a sentence end
    assert result["inputs"][0]["title"] == "Digital und analog"


def test_touching_topics_that_snap_past_each_other_are_no_overlap():
    segments = [{"start": 100.0, "end": 212.0, "text": "a"}, {"start": 205.0, "end": 300.0, "text": "b"}]
    raw = {"phases": [{"start": 0, "kind": "input", "title": "x"}], "inputs": [], "summary": "",
           "topics": [{"start": 100, "end": 208, "title": "A", "summary": ""},       # end snaps to 212
                      {"start": 208, "end": 300, "title": "B", "summary": ""},       # start snaps to 205
                      {"start": 300, "end": 400, "title": "C", "summary": ""}]}
    result, errors = outline.check(raw, 400.0, segments)
    assert errors == [] and result["topics"][1]["start"] == result["topics"][0]["end"] == 212.0


def test_check_names_what_the_model_has_to_fix():
    bad = {"phases": [{"start": 0, "kind": "lecture", "title": "?"}],
           "topics": [{"start": 100, "end": 400, "title": "A", "summary": ""},
                      {"start": 300, "end": 700, "title": "B", "summary": ""},     # overlaps A
                      {"start": 1000, "end": 1004, "title": "C", "summary": ""}],  # too short
           "inputs": [], "summary": ""}
    _, errors = outline.check(bad, DURATION, [])
    text = "\n".join(errors)
    assert "unknown kind 'lecture'" in text and "phases: none" in text
    assert "overlaps" in text and "shorter than 10 s" in text and "at least three" in text


def test_material_is_the_overview_as_text():
    text = outline.material(_overview(), {"course_name": "DuP"}, "de")
    assert "Module: DuP" in text and "chrome.exe 30 min" in text
    assert "Pauses of 30 s and more: 0:15:00–0:16:00 (60 s)" in text
    assert "## 0:05:00 (300–360 s)" in text and "„Was ist digital?“" in text
    assert "room voices 4 words" in text


class Model:
    """Answers like the Responses API: first an outline with two topics, then the corrected one."""
    model = "fake"

    def __init__(self, answers):
        self.answers, self.bodies = answers, []

    def create(self, body):
        self.bodies.append(body)
        n = len(self.bodies)
        args = self.answers[min(n, len(self.answers)) - 1]
        return {"id": f"r{n}", "usage": {"input_tokens": 1000, "output_tokens": 50},
                "output": [{"type": "function_call", "call_id": f"c{n}", "name": "set_outline",
                            "arguments": json.dumps(args)}]}


def _client(model):
    return llm.OpenAIResponses("k", "fake", post=lambda url, body, headers, timeout: model.create(body))


def test_run_corrects_and_writes_the_outline(tmp_path):
    folder = _folder(tmp_path)
    two = {**GOOD, "topics": GOOD["topics"][:2]}
    model = Model([two, GOOD])
    said = []
    result = outline.run(folder, None, {"ui_language": "de"}, said.append, client=_client(model))
    assert result["agent"]["rounds"] == 2 and result["agent"]["usage"]["input_tokens"] == 2000
    assert any("at least three" in e for e in result["agent"]["corrections"][0])
    assert len(result["topics"]) == 3 and result["problems"] == []
    assert [s["title"] for s in result["inputs"][0]["slides"]] == ["Was ist digital?", "Analog und digital"]
    second = model.bodies[1]
    assert second["previous_response_id"] == "r1"                       # only the correction goes up again
    assert "at least three" in second["input"][0]["output"]
    assert "Language of the lecture: de" in model.bodies[0]["input"][0]["content"][0]["text"]
    assert "German" in model.bodies[0]["instructions"]
    assert outline.current(folder) and said[0].startswith("Gliederung")
    text = outline.lines(folder)
    assert "610-905 Analog und digital: Vergleich." in text and "Topics:" in text and "Slide inputs" in text


def test_a_failing_model_leaves_an_error_and_the_next_proposal_tries_again(tmp_path):
    folder = _folder(tmp_path)

    class Broken:
        model = "fake"

        def complete(self, instructions, messages, tools):
            raise llm.ModelError("Das Sprachmodell antwortet mit Fehler 500")

    result = outline.run(folder, None, client=Broken())
    assert "Fehler 500" in result["error"] and not outline.current(folder)
    assert outline.lines(folder) == ""


def test_agent_and_approval_see_the_outline(tmp_path):
    from test_cut import _session
    folder = _session(tmp_path)
    (folder / "overview.json").write_text(json.dumps({**_overview(), "duration": 60.0}), encoding="utf-8")
    (folder / "outline.json").write_text(json.dumps({
        "duration": 60.0, "summary": "Kurz.", "phases": [{"start": 0, "end": 60, "kind": "input", "title": "Folien"}],
        "topics": [{"start": 5, "end": 50, "title": "Einstieg", "summary": "Worum es geht."}], "inputs": []}),
        encoding="utf-8")
    b = cut_tools.Workbench(folder)
    try:
        text = cut_agent.CutAgent(b, None)._first_message()
        assert "5-50 Einstieg: Worum es geht." in text and "0:00:00–0:01:00 0-60 input: Folien" in text
    finally:
        b.close()
    edit.save(folder, edit.new(60))
    view = library.cut_view(folder)
    assert view["outline"]["phases"][0]["label"] == "Input" and view["outline"]["topics"][0]["title"] == "Einstieg"
