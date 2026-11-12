"""Version 2 of the cut: sections along the outline, each with a fresh context, and a closing round."""
from __future__ import annotations

import json

from pipeline import cut_stages, cut_tools, edit, llm
from test_cut import _session


def phase(a, b, kind, title=""):
    return {"start": float(a), "end": float(b), "kind": kind, "title": title}


def test_sections_follow_the_phases():
    plan = {"phases": [phase(0, 60, "idle"), phase(60, 400, "organisation"), phase(400, 3400, "input"),
                       phase(3400, 3500, "break"), phase(3500, 4100, "demo"), phase(4100, 4200, "idle")],
            "topics": [{"start": 400.0}, {"start": 1300.0}, {"start": 2350.0}, {"start": 3500.0}]}
    sections = cut_stages.plan_sections(plan, 4200.0, [])
    spans = [(s["start"], s["end"], s["light"]) for s in sections]
    assert spans[0] == (0.0, 400.0, True)                       # arriving and organisation: a small model's section
    assert spans[1] == (400.0, 1300.0, False)                   # a long input split at its topics
    assert spans[2] == (1300.0, 2350.0, False)
    assert spans[3][0] == 2350.0 and not spans[3][2]            # the short break goes with teaching
    assert spans[-1][1] == 4200.0 and all(s["end"] - s["start"] <= cut_stages.LONGEST + cut_stages.OWN for s in sections)
    assert [s["no"] for s in sections] == list(range(1, len(sections) + 1))
    whole = cut_stages.plan_sections({}, 2000.0, [{"start": float(t)} for t in range(0, 2000, 30)])
    assert len(whole) == 2 and whole[0]["end"] == 900.0 and not whole[0]["light"]   # no outline: even pieces


class Script:
    """Answers like the Responses API, one scripted reply after the other; keeps what it was sent."""

    def __init__(self, model, replies):
        self.model, self.replies, self.bodies = model, list(replies), []

    def client(self):
        return llm.OpenAIResponses("k", self.model, post=lambda url, body, headers, timeout: self.create(body))

    def create(self, body):
        self.bodies.append(body)
        calls = self.replies.pop(0)
        n = len(self.bodies)
        return {"id": f"{self.model}{n}", "usage": {"input_tokens": 100, "cached_tokens": 0, "output_tokens": 10},
                "output": [{"type": "function_call", "call_id": f"{self.model}{n}{i}", "name": name,
                            "arguments": json.dumps(a)} for i, (name, a) in enumerate(calls)]}


def test_staged_cut_works_section_by_section(tmp_path, monkeypatch):
    monkeypatch.setattr(cut_stages, "TARGET", 20.0)
    monkeypatch.setattr(cut_stages, "LONGEST", 40.0)
    monkeypatch.setattr(cut_stages, "OWN", 10.0)
    monkeypatch.setattr(cut_stages, "MARGIN", 5.0)
    folder = _session(tmp_path)
    (folder / "outline.json").write_text(json.dumps({
        "phases": [phase(0, 20, "idle", "Ankommen"), phase(20, 60, "input", "Folien")],
        "topics": [{"start": 20.0, "end": 60.0, "title": "Einstieg ins Thema", "summary": "Worum es geht."}],
        "inputs": [{"start": 20.0, "end": 60.0, "title": "Einstieg", "summary": "", "slides": []}]}), encoding="utf-8")
    light = Script("small", [[("propose", {"kind": "cut", "start": 0, "end": 4.5, "reason": "Stille", "confidence": 0.9,
                                           "params": {}, "category": "dead_time"}),
                              ("section_done", {"note": "Ende: der Einstieg beginnt", "check": []})]])
    strong = Script("big", [[("propose", {"kind": "cut", "start": 2, "end": 4, "reason": "zu früh", "confidence": 0.5,
                                          "params": {}}),                              # outside: refused
                             ("propose", {"kind": "mute", "start": 30, "end": 33, "reason": "Frage aus dem Raum",
                                          "confidence": 0.8, "params": {}, "category": "privacy"}),
                             ("section_done", {"note": "", "check": [{"id": "x", "why": "knapp"}]})]])
    closing = Script("close", [[("set_metadata", {"title": "Einstieg", "description": "Eine Probe."}),
                                ("set_summary", {"cuts": [{"category": "dead_time", "text": "Stille am Anfang."}],
                                                 "check": [], "notes": []}),
                                ("check_edit", {})],
                               [("finish", {"summary": "Fertig."})]])
    b = cut_tools.Workbench(folder)
    said = []
    try:
        clients = {"section": strong, "section_light": light, "closing": closing}
        summary = cut_stages.StagedCut(b, lambda stage: clients[stage].client(), said.append, workers=2).run()
    finally:
        b.close()
    e = edit.load(folder)
    agent = e["agent"]
    assert summary == "Fertig." and agent["version"] == 2 and e["title"] == "Einstieg"
    assert [(s["model"], s["light"], s["rounds"]) for s in agent["sections"]] == [("small", True, 1), ("big", False, 1)]
    assert agent["closing"]["rounds"] == 2 and agent["usage"]["input_tokens"] == 400
    chapter = next(x for x in e["items"] if x["kind"] == "chapter")
    assert chapter["start"] == 0.0 and chapter["params"]["title"] == "Einstieg ins Thema"   # from the outline, at 0:00
    mine = [(x["kind"], x["start"]) for x in e["items"] if x["source"] == "agent" and x["kind"] != "chapter"]
    assert ("mute", 30.0) in mine and ("cut", 2.0) not in mine
    second = strong.bodies[0]["input"][0]["content"][0]["text"]                # fresh context: its own first message
    assert "Your section: 2 of 2" in second and "Ende: der Einstieg beginnt" not in second   # cut at the same time
    assert "Hallo." not in second or "  [" in second                          # the first sentence lies outside
    log = (folder / "edit.log.jsonl").read_text(encoding="utf-8")
    assert "outside your section" in log
    first_close = closing.bodies[0]["input"][0]["content"][0]["text"]
    assert "20-60 lecture: Einstieg" in first_close and "x: knapp" in first_close   # an old outline: a talk
    assert "clip" not in closing.bodies[0]["instructions"].lower().replace("clipped", "")
    assert "1 (0:00:00–0:00:20): Ende: der Einstieg beginnt" in first_close                  # the notes go to the closing
    assert said[0] == "Feinschnitt: 2 Abschnitte, 2 gleichzeitig …" and any("2 von 2 Abschnitten fertig" in t for t in said)
    assert (folder / "edit.agent.json").exists()


def test_word_tools_answer_in_few_tokens():
    words = [{"t": 5.0, "end": 5.4, "word": "Hallo,", "conf": 0.95, "speaker": "A"},
             {"t": 5.5, "end": 5.9, "word": "hört", "conf": 0.4, "speaker": "A"},
             {"t": 6.0, "end": 6.4, "word": "ja", "conf": 0.9, "speaker": "B"}]
    assert cut_stages.words_text(words) == "[A] 5.00 Hallo, 5.50 hört? [B] 6.00 ja"
    assert cut_stages.heard_text(words) == "[A 5.0-5.5] Hallo, hört\n[B 6.0-6.0] ja"
    many = [{"t": float(i), "word": "w"} for i in range(cut_stages.WORDS_SHOWN + 5)]
    assert cut_stages.words_text(many).endswith("and 5 more words; ask for a shorter span")
    assert cut_stages.words_text([]) == "no words in this span" and cut_stages.heard_text([]) == "nothing heard"
