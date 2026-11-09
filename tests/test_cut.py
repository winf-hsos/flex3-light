"""The cut: edit list, rules, the agent's workbench and its loop, without a network."""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import cut_agent, cut_rules, cut_tools, edit  # noqa: E402

WORDS = [{"word": w, "start": s, "end": s + 0.4} for w, s in
         [("Hallo,", 5.0), ("hört", 5.5), ("man", 6.0), ("mich?", 6.5), ("Gut.", 8.0), ("Heute", 9.0),
          ("geht", 9.5), ("es", 10.0), ("los.", 10.5), ("Weiter", 50.0), ("geht's.", 50.6)]]


def test_snap_moves_boundaries_out_of_words():
    assert edit.snap(5.2, WORDS) == 4.88                  # nearer the start of "Hallo," -> just before it
    assert edit.snap(6.35, WORDS) == 6.45                 # nearer the end of "man" -> into the short gap after it
    assert edit.snap(20.0, WORDS) == 20.0                 # in a pause: stays


def test_kept_problems_and_chapters():
    e = edit.new(60.0)
    edit.add(e, edit.item("cut", 0, 4.6, "Stille", "rule", 0.9, "accepted"))
    edit.add(e, edit.item("pause", 11.5, 49.5, "Übung", "rule", 0.7, "accepted", mode="card"))
    edit.add(e, edit.item("cut", 49.9, 50.0, "Rest", "agent", 0.3, "proposed"))
    assert edit.kept(e) == [(4.6, 11.5), (49.5, 60.0)]
    assert any("Rest von 0.4" in p for p in edit.problems(e))                 # between 49.5 and 49.9
    edit.add(e, edit.item("chapter", 3.0, None, "Einstieg", "agent", title="Einstieg"))
    assert any("0:00" in p for p in edit.problems(e)) and any("drei Kapiteln" in p for p in edit.problems(e))


def test_rules_edges_pauses_programs():
    activity = [[0, 12, "POWERPNT.EXE", "Folien"], [12, 20, "chrome.exe", "Moodle"],
                [20, 25, "chrome.exe", "Posteingang - Gmail"], [25, 25.5, "OUTLOOK.EXE", "x"],
                [26, 40, "OUTLOOK.EXE", "Posteingang"], [41, 45, "OUTLOOK.EXE", "Posteingang"]]
    items = cut_rules.propose(WORDS, 60.0, activity, {"pause_min": 30, "keep_programs": []})
    kinds = [(x["kind"], x["rule"] if "rule" in x else x["params"]["rule"], x["state"]) for x in items]
    assert ("cut", "edge", "accepted") in kinds                   # 5 s of silence at the start, 9 s at the end
    assert sum(1 for k in kinds if k[1] == "edge") == 2
    pause = next(x for x in items if x["kind"] == "pause")
    assert pause["start"] == 11.4 and pause["end"] == 49.5 and pause["params"]["mode"] == "card"
    programs = [x for x in items if x["params"].get("rule") == "program"]
    assert [(x["start"], x["end"]) for x in programs] == [(20, 25), (26, 45)]   # blip skipped, Outlook merged
    assert all(x["state"] == "proposed" for x in programs)
    always = cut_rules.programs(activity, cut_rules.policy({"cut_programs": ["outlook.exe"]}))
    assert any(x["state"] == "accepted" and x["start"] == 26 for x in always)
    kept = cut_rules.programs(activity, cut_rules.policy({"keep_programs": ["chrome.exe"]}))
    assert all(x["start"] != 20 for x in kept)                     # the browser stays
    assert cut_rules.program_summary(activity)[0] == {"program": "OUTLOOK.EXE", "seconds": 18.5}


def _session(folder: Path) -> Path:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=60",
                    "-f", "lavfi", "-i", "testsrc2=s=640x360:r=10:d=60", "-shortest", "-c:v", "libx264",
                    "-preset", "ultrafast", "-c:a", "aac", str(folder / "recording.mp4")],
                   stdin=subprocess.DEVNULL, capture_output=True, check=True)
    (folder / "session.json").write_text(json.dumps({"session_id": "x", "course_name": "M", "duration_sec": 60}),
                                         encoding="utf-8")
    (folder / "transcript.json").write_text(json.dumps({"language": "de", "duration": 60.0, "words": WORDS,
                                                        "segments": [{"start": 5.0, "end": 10.9, "text": "Hallo."}]}),
                                            encoding="utf-8")
    (folder / "activity.json").write_text(json.dumps({"activity": [[0, 60, "POWERPNT.EXE", "Folien"]], "private": []}),
                                          encoding="utf-8")
    return folder


def test_workbench_pictures_sound_and_proposals(tmp_path):
    b = cut_tools.Workbench(_session(tmp_path))
    try:
        assert Image.open(io.BytesIO(b.frame(12.0, "program", 320))).size == (320, 180)
        sheet = Image.open(io.BytesIO(b.contact_sheet(0, 60, 6, "program")))
        assert sheet.size[0] > 1000
        loud = b.loudness(0, 10)
        assert len(loud["db"]) == 10 and loud["lecturer_level"] is not None
        x = b.propose("cut", 5.2, 6.35, "Mikrofontest", 0.6)      # bounds snapped out of words
        assert (x["start"], x["end"]) == (4.88, 6.45) and x["source"] == "agent"
        assert x["state"] == "accepted" and x["category"] == "other"            # the agent decides
        b.change(x["id"], reason="Mikrofontest am Anfang", category="technical")
        saved = json.loads((tmp_path / "edit.json").read_text(encoding="utf-8"))["items"][0]
        assert saved["reason"] == "Mikrofontest am Anfang" and saved["category"] == "technical"
        rule = b.add_rules()[0]                                    # the silence before the first word
        b.assess(rule["id"], "disagree", "Der Mikrofontest gehört mit dazu.")
        better = b.propose("cut", 0, 8.3, "Stille und Mikrofontest", 0.8, replaces=[rule["id"], "unknown"],
                           category="technical")
        assert better["replaces"] == [rule["id"]] and rule["state"] == "rejected" and rule["reason"].startswith("Stille")
        assert rule["assessment"] == {"verdict": "disagree", "reason": "Der Mikrofontest gehört mit dazu."}
        try:
            b.change(rule["id"], reason="überschrieben")
            raise AssertionError("a rule's proposal must not be changed by the agent")
        except ValueError:
            pass
        assert b.overview()["programs"][0]["program"] == "POWERPNT.EXE"
    finally:
        b.close()


class FakeModel:
    """Answers like the Responses API: first a contact sheet and a proposal, then finish."""
    model = "fake"

    def __init__(self):
        self.bodies = []

    def create(self, body):
        self.bodies.append(body)
        n = len(self.bodies)
        if n == 1:
            calls = [("get_contact_sheet", {"start": 0, "end": 12, "count": 4, "track": "program"}),
                     ("propose", {"kind": "cut", "start": 5.2, "end": 8.3, "reason": "Mikrofontest", "confidence": 0.7,
                                  "params": {}}),
                     ("propose", {"kind": "chapter", "start": 0, "end": None, "reason": "Beginn", "confidence": 0.8,
                                  "params": {"title": "Einstieg"}})]
        else:
            calls = [("check_edit", {}), ("finish", {"summary": "Mikrofontest am Anfang vorgeschlagen."})]
        return {"id": f"r{n}", "usage": {"input_tokens": 100, "output_tokens": 20},
                "output": [{"type": "function_call", "call_id": f"c{n}{i}", "name": name, "arguments": json.dumps(a)}
                           for i, (name, a) in enumerate(calls)]}


def test_agent_loop_uses_tools_and_writes_the_list(tmp_path):
    b = cut_tools.Workbench(_session(tmp_path))
    model = FakeModel()
    try:
        summary = cut_agent.CutAgent(b, model).run()
    finally:
        b.close()
    assert summary == "Mikrofontest am Anfang vorgeschlagen."
    first = model.bodies[0]["input"][0]["content"][0]["text"]
    assert "Rule proposals" in first and "Stille vor dem ersten Wort" in first and "[0:05 5.0-10.9] Hallo." in first
    second = model.bodies[1]
    assert second["previous_response_id"] == "r1"
    assert [x["type"] for x in second["input"][:3]] == ["function_call_output"] * 3
    assert second["input"][3]["content"][1]["type"] == "input_image"          # the contact sheet follows as a picture
    saved = json.loads((tmp_path / "edit.json").read_text(encoding="utf-8"))
    assert {x["kind"] for x in saved["items"]} >= {"cut", "chapter"} and saved["agent"]["images"] == 1
    log = (tmp_path / "edit.log.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(log[0])["event"] == "start" and json.loads(log[-1])["event"] == "finish"


def test_runner_proposes_the_cut_after_the_subtitles(tmp_path, monkeypatch):
    import pipeline_runner
    folder = tmp_path / "2026-10-28 - Test"
    folder.mkdir()
    (folder / "session.json").write_text(json.dumps({"session_id": "x"}), encoding="utf-8")
    (folder / "pipeline.json").write_text(json.dumps({"steps": {"transcribe": {"state": "done"},
                                                                "subtitles": {"state": "done"}}}), encoding="utf-8")
    calls = []

    def fake_run(f, key, listener, settings, progress):
        calls.append((key, settings["pause_mode"]))
        progress("Schnitt-Agent: propose (Runde 1)")
        e = edit.new(60)
        e["agent"] = {"model": "fake"}
        edit.save(f, e)
        return e
    monkeypatch.setattr(pipeline_runner.cut_agent, "run", fake_run)
    monkeypatch.setattr(pipeline_runner.services, "key", lambda s: "sk" if s == "openai" else None)
    settings = {"auto": False, **cut_rules.policy(None)}
    r = pipeline_runner.PipelineRunner(tmp_path / "q.json", lambda: True, lambda m: [], lambda f: settings)
    r.enqueue(folder)
    assert r._process(folder) and calls == []                    # switched off: nothing proposed
    settings["auto"] = True
    r.enqueue(folder)
    assert r._process(folder) and calls == [("sk", "card")]
    assert pipeline_runner.read_state(folder)["steps"]["cut"]["state"] == "done"
    assert r.describe(folder) == "Schnitt vorgeschlagen, wartet auf deine Freigabe"
    assert r.redo_cut(folder) and (folder / "edit.previous.json").exists() and not (folder / "edit.json").exists()


def test_app_approves_moves_and_remembers_programs(tmp_path):
    import threading
    from types import SimpleNamespace
    import core
    import pipeline_runner
    import library
    folder = _session(tmp_path)
    (folder / "pipeline.json").write_text(json.dumps({"steps": {"transcribe": {"state": "done"},
                                                                "subtitles": {"state": "done"}}}), encoding="utf-8")
    (folder / "activity.json").write_text(json.dumps({"activity": [[0, 30, "POWERPNT.EXE", "Folien"],
                                                                   [30, 40, "OUTLOOK.EXE", "Posteingang"]]}),
                                          encoding="utf-8")
    b = cut_tools.Workbench(folder)
    b.add_rules()
    b.close()
    c = core.Core.__new__(core.Core)                      # no OBS: only what the approval needs
    c._library = {"x": folder}
    c.settings = {}
    c.save = lambda: None
    _profile_of(c, tmp_path)
    c.pipeline = pipeline_runner.PipelineRunner(tmp_path / "q.json", lambda: False, lambda m: [])
    assert library.stations(folder)["cut"] == "review"
    e = edit.load(folder)
    outlook = next(x for x in e["items"] if x["params"].get("rule") == "program")
    assert outlook["state"] == "proposed"
    assert "offen" in c.approve_cut("x")                  # open proposals block the approval
    assert c.set_program_rule("x", "OUTLOOK.EXE", "cut") == ""
    e = edit.load(folder)
    assert c.cut_settings()["cut_programs"] == ["outlook.exe"]
    outlook = next(x for x in e["items"] if x["params"].get("rule") == "program")
    assert outlook["state"] == "accepted"                 # remembered: now cut by itself
    pause = next(x for x in e["items"] if x["kind"] == "pause")
    assert c.change_cut_item("x", pause["id"], {"state": "rejected", "start": 12.0}) == ""
    assert c.add_cut_item("x", "cut", 5.2, 6.35) == ""    # snapped out of the words
    own = next(x for x in edit.load(folder)["items"] if x["source"] == "lecturer")
    assert (own["start"], own["end"], own["state"]) == (4.88, 6.45, "accepted")
    c.pipeline.enqueue = lambda f: None                   # rendering is tested in test_render.py
    assert c.approve_cut("x") == "" and library.stations(folder)["cut"] == "queued"   # approved, video follows
    assert c.change_cut_item("x", own["id"], {"end": 7.0}) == "" and library.stations(folder)["cut"] == "review"
    assert c.remove_cut_item("x", pause["id"]).startswith("Nur selbst")


def test_find_turns_words_into_regions():
    from pipeline import ocr
    result = {"width": 1000, "height": 500, "lines": [
        {"text": "Mail an max.muster@example.org senden", "words": [
            {"text": "Mail", "x": 100, "y": 50, "w": 40, "h": 20}, {"text": "an", "x": 150, "y": 50, "w": 20, "h": 20},
            {"text": "max.muster@example.org", "x": 180, "y": 50, "w": 220, "h": 20},
            {"text": "senden", "x": 410, "y": 50, "w": 60, "h": 20}]},
        {"text": "expose. qmd", "words": [{"text": "expose.", "x": 600, "y": 300, "w": 60, "h": 18},
                                          {"text": "qmd", "x": 665, "y": 300, "w": 30, "h": 18}]}]}
    hit = ocr.find(result, "MAX.MUSTER@example.org")
    assert len(hit) == 1 and hit[0]["text"] == "max.muster@example.org"
    x, y, w, h = hit[0]["region"]
    assert x < 0.18 < x + w and 0.4 < x + w < 0.41 and y < 0.1 < y + h       # covers the word, padded a little
    assert ocr.find(result, "expose.qmd")[0]["text"] == "expose. qmd"             # OCR's stray space is forgiven
    assert len(ocr.find(result, "")) == 2 and ocr.find(result, "nicht da") == []


def test_windows_ocr_reads_a_picture(tmp_path):
    import pytest
    from PIL import ImageDraw, ImageFont
    from pipeline import ocr
    img = Image.new("RGB", (900, 200), "white")
    ImageDraw.Draw(img).text((40, 70), "Matrikelnummer 4711 Exposé", fill="black", font=ImageFont.truetype("arial.ttf", 44))
    img.save(tmp_path / "a.png")
    try:
        result = ocr.recognize(tmp_path / "a.png", "de")
    except RuntimeError as e:                                                    # no OCR on this machine
        pytest.skip(str(e))
    hit = ocr.find(result, "4711")
    assert hit and 0.3 < hit[0]["region"][0] < 0.7 and "Exposé" in " ".join(l["text"] for l in result["lines"])


def test_agent_leaves_nothing_open_and_the_summary_has_one_shape(tmp_path):
    b = cut_tools.Workbench(_session(tmp_path))
    try:
        rules = b.add_rules()                                     # edges accepted, the pause waits
        pause = next(x for x in rules if x["kind"] == "pause")
        assert pause["state"] == "proposed" and any("Noch nicht entschieden" in p for p in b.check())
        b.propose("cut", 5.2, 8.3, "Mikrofontest", 0.9, category="technical")
        b.propose("blur", 20, 30, "Mailadresse im Browser", 0.8, category="privacy", region=[0.1, 0.1, 0.2, 0.05])
        b.propose("clip", 9, 50, "Steht für sich", 0.7, title="Wie man anfängt")
        b.propose("chapter", 0, None, "Beginn", 0.9, title="Einstieg")
        b.set_summary([{"category": "technical", "text": "Ein Mikrofontest am Anfang."},
                       {"category": "nonsense", "text": "x"}], [{"id": pause["id"], "why": "knapp"}],
                      ["Mikrofon war anfangs leise.", " "])
        assert b.decide_rest() == 1 and pause["state"] == "accepted" and pause["decided_by"] == "fallback"
        s = edit.summary(b.edit)
    finally:
        b.close()
    labels = [c["label"] for c in s["cuts"]]
    assert labels[0] == "Lange Pausen" and set(labels) == {"Lange Pausen", "Leerlauf", "Technik", "Datenschutz"}
    tech = next(c for c in s["cuts"] if c["category"] == "technical")
    assert tech["count"] == 1 and tech["text"] == "Ein Mikrofontest am Anfang."
    assert next(c for c in s["cuts"] if c["category"] == "privacy")["hidden"] == 1
    assert s["clips"] == ["Wie man anfängt"] and s["chapters"] == ["Einstieg"]
    assert s["check"] == [{"id": pause["id"], "why": "knapp"}] and s["notes"] == ["Mikrofon war anfangs leise."]
    assert s["kept"] < s["duration"]


def test_switched_off_kinds_are_not_proposed(tmp_path):
    off = {"kinds_off": ["blur", "zoom", "pause"]}
    rules = cut_rules.propose(WORDS, 60.0, [], off)
    assert rules and all(x["kind"] != "pause" for x in rules)                 # the edges stay, the pause does not
    tools = cut_agent.tools_for(set(off["kinds_off"]))
    kinds = next(t for t in tools if t["name"] == "propose")["parameters"]["properties"]["kind"]["enum"]
    assert "blur" not in kinds and "zoom" not in kinds and "cut" in kinds
    assert "blur" in next(t for t in cut_agent.TOOLS if t["name"] == "propose")["parameters"]["properties"]["kind"]["enum"]
    assert "switched off: blur, pause, zoom" in cut_agent.instructions_for(set(off["kinds_off"]))
    b = cut_tools.Workbench(_session(tmp_path), off)
    try:
        try:
            b.propose("blur", 5, 9, "Mail", 0.8, category="privacy", region=[0.1, 0.1, 0.2, 0.05])
            raise AssertionError("a switched-off kind must be refused")
        except ValueError as e:
            assert "switched blur off" in str(e)
        assert b.propose("cut", 5.2, 8.3, "Mikrofontest", 0.8, category="technical")["kind"] == "cut"
    finally:
        b.close()


def _profile_of(c, tmp_path):
    """A core without OBS gets a local profile, where the cut settings live since 2026-11-09."""
    import threading
    from types import SimpleNamespace

    import lecturer_profile
    c.profile = lecturer_profile.Profile(tmp_path / "profile.json")
    c.delivery = SimpleNamespace(wake=threading.Event())


def test_nothing_starts_after_a_recording_unless_set(tmp_path):
    import core
    import pipeline_runner
    c = core.Core.__new__(core.Core)
    c.settings, c.save = {}, lambda: None
    _profile_of(c, tmp_path)
    c.pipeline = pipeline_runner.PipelineRunner(tmp_path / "q.json", lambda: False, lambda m: [])
    folder = tmp_path / "rec"
    folder.mkdir()
    assert c.after_recording() == "nothing" and c.cut_settings()["auto"] is False
    c._archived(folder)
    assert not c.pipeline.queued(folder)                  # only filed
    c._library = {"x": folder}
    assert c.start_transcription("x") == "" and c.pipeline.queued(folder)
    c.set_after_recording("cut")
    assert c.after_recording() == "cut" and c.cut_settings()["auto"] is True
    c.profile.set_preferences({"cut.auto": True})        # an older stored switch does not count any more
    c.set_after_recording("transcribe")
    assert c.cut_settings()["auto"] is False
    c.set_after_recording("everything")                   # unknown values are ignored
    assert c.after_recording() == "transcribe"


def test_changing_one_cut_setting_keeps_the_default_title_rule(tmp_path, monkeypatch):
    import core
    import pipeline.cut_rules as rules
    c = core.Core.__new__(core.Core)
    _profile_of(c, tmp_path)
    c.set_cut_settings({"pause_min": 40})
    assert c.cut_settings()["cut_titles"] == sorted(rules.DEFAULT_POLICY["cut_titles"])
    monkeypatch.setattr(core, "save_settings", lambda s: None)
    settings = {"cut": {"cut_titles": [], "pause_min": 20}}     # written by the bug before 2026-11-09
    fresh = core.Core.__new__(core.Core)
    _profile_of(fresh, tmp_path / "other")
    core._move_preferences_to_profile(settings, fresh.profile)
    assert fresh.cut_settings()["cut_titles"] == rules.DEFAULT_POLICY["cut_titles"]


def test_a_lecture_is_worked_on_in_one_place_only(tmp_path):
    import core
    import pipeline_runner
    c = core.Core.__new__(core.Core)
    c.settings, c.save = {}, lambda: None
    _profile_of(c, tmp_path)
    c.pipeline = pipeline_runner.PipelineRunner(tmp_path / "q.json", lambda: False, lambda m: [])
    c.set_after_recording("transcribe")
    c.set_pipeline_place("server")
    folder = tmp_path / "rec"
    folder.mkdir()
    assert c.pipeline_place() == "device"                 # without a server it stays here
    c._archived(folder)
    assert c.pipeline.queued(folder)
    c.pipeline.forget(folder)
    c.delivery.client = object()                          # connected: the server does it
    assert c.pipeline_place() == "server"
    c._archived(folder)
    assert not c.pipeline.queued(folder)


def test_the_activity_goes_to_the_server_without_titles(tmp_path):
    from types import SimpleNamespace

    import delivery
    import lecturer_profile
    p = lecturer_profile.Profile(tmp_path / "profile.json")
    p.set_preferences({"cut.cut_titles": ["moodle"]})
    d = delivery.Delivery(tmp_path / "raw", None, profile=p)
    s = SimpleNamespace(data={"activity": [[0, 5, "chrome.exe", "Kurs - Moodle - Google Chrome"],
                                           [5, 9, "chrome.exe", "Gmail - Posteingang"]], "private": [[1, 2]]})
    assert d._activity_for_server(s) == {"activity": [[0, 5, "chrome.exe", "moodle"], [5, 9, "chrome.exe", ""]],
                                         "private": [[1, 2]]}
    assert d._activity_for_server(SimpleNamespace(data={})) is None
