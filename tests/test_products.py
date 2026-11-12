"""Topic videos (stage G, pipeline/products.py): proposed from the slide inputs, approved in a second
step, cut from the rendered long version with cards of their own, and part of the package."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pipeline import cut_rules, edit, products, publish, render, timeline  # noqa: E402
from test_render import _duration, _video  # noqa: E402


def _folder(tmp_path: Path, seconds: int = 40, inputs: list | None = None) -> Path:
    folder = tmp_path / "2026-11-12 - Test"
    folder.mkdir()
    _video(folder / "recording.mp4", "testsrc2", seconds)
    (folder / "session.json").write_text(json.dumps({"session_id": "x", "language": "de", "course_name": "Modul",
                                                     "recorded_at": "2026-11-12T10:00:00+01:00"}), encoding="utf-8")
    words = [{"word": f"Wort{k}", "start": k + 0.1, "end": k + 0.6} for k in range(1, seconds - 1)]
    (folder / "transcript.json").write_text(json.dumps({"language": "de", "words": words}), encoding="utf-8")
    (folder / "outline.json").write_text(json.dumps({"inputs": inputs if inputs is not None else [
        {"start": 10.0, "end": 30.0, "title": "Abtastung", "summary": "Wie aus einem Signal Zahlen werden."},
        {"start": 31.0, "end": 36.0, "title": "Zu kurz", "summary": ""}]}), encoding="utf-8")
    e = edit.new(float(seconds))
    edit.add(e, edit.item("chapter", 0, None, "", "agent", 0.9, "accepted", title="Anfang"))
    edit.add(e, edit.item("chapter", 20, None, "", "agent", 0.9, "accepted", title="Mitte"))
    edit.add(e, edit.item("cut", 14, 16, "Versprecher", "agent", 0.9, "accepted", "other"))
    e["title"] = "Signale"
    e["cards"] = edit.default_cards(e, json.loads((folder / "session.json").read_text(encoding="utf-8")),
                                    {"music": False})
    e["approved"] = "2026-11-12T11:00:00+01:00"
    edit.save(folder, e)
    return folder


def test_the_switch_is_a_cut_preference():
    assert cut_rules.settings_from({})["topic_videos"] == "propose"
    assert cut_rules.cut_preferences({}, {"topic_videos": "auto"})["cut.topic_videos"] == "auto"
    assert cut_rules.cut_preferences({}, {"topic_videos": "immer"})["cut.topic_videos"] == "propose"
    assert products.mode({"topic_videos": "off"}) == "off" and products.mode(None) == "propose"


def test_slide_inputs_become_proposals_and_auto_approves_them(tmp_path, monkeypatch):
    monkeypatch.setattr(products, "SHORTEST", 10.0)
    folder = _folder(tmp_path)
    data = products.propose(folder, {"topic_videos": "propose"})
    assert [(x["title"], x["spans"], x["state"]) for x in data["products"]] == [("Abtastung", [[10.0, 30.0]], "proposed")]
    assert data["approved"] is None and "Zu kurz" in data["notes"][0]
    assert products.accepted(data) == []                              # nothing renders before the approval
    view = products.view(folder)
    assert view["products"][0]["kept"] == 18.0                        # 20 s less the cut of 2 s
    data = products.propose(folder, {"topic_videos": "auto"})
    assert data["approved"] and [x["state"] for x in data["products"]] == ["accepted"]


def test_the_lecturer_changes_adds_and_approves(tmp_path, monkeypatch):
    monkeypatch.setattr(products, "SHORTEST", 10.0)
    folder = _folder(tmp_path)
    products.propose(folder, {})
    assert products.change(folder, "t1", {"title": " "}) == "Ein Themenvideo braucht einen Titel."
    assert products.change(folder, "t1", {"start": 10, "end": 15}) == "Ein Themenvideo muss mindestens zehn Sekunden lang sein."
    assert products.change(folder, "t9", {"title": "x"}) == "Dieses Themenvideo gibt es nicht mehr."
    assert products.add(folder, 20, 40, "Mitte") == ""
    assert products.approve(folder) == ""
    data = products.load(folder)
    assert [(x["id"], x["state"], x["source"]) for x in data["products"]] == [("t1", "accepted", "piece"),
                                                                            ("t2", "accepted", "lecturer")]
    assert products.change(folder, "t2", {"state": "rejected", "description": "Neu"}) == ""
    data = products.load(folder)
    assert data["approved"] is None and products.accepted(data) == []  # every change asks for a new approval


def test_a_topic_video_is_cut_from_the_long_version_with_cards_of_its_own(tmp_path, monkeypatch):
    monkeypatch.setattr(products, "SHORTEST", 10.0)
    folder = _folder(tmp_path)
    long = render.run(folder)
    products.propose(folder, {})
    products.add(folder, 20.0, 40.0, "Mitte bis Schluss")
    products.approve(folder)
    assert products.due(folder)
    report = products.render(folder)
    assert [v["name"] for v in report["videos"]] == ["topic-01", "topic-02"] and report["warnings"] == []
    e = edit.load(folder)
    p = timeline.plan(e, 30)
    ins = timeline.inserts(e, p)
    first, second = report["videos"]
    # 10..30 of the recording: without the cut 14..16 and without the chapter card at 20 of the long version?
    # The card at 20 lies inside the span and stays; the card at the start of the second video does not.
    assert abs(first["end"] - first["start"] - (18.0 + 2.0)) < 0.05
    assert abs(second["start"] - timeline.shift(timeline.map_forward(20.0, p), ins)) < 1e-6
    assert abs(second["end"] - long["duration"] + 4.0) < 0.05            # before the closing card of the long version
    expected = 4.0 + (second["end"] - second["start"]) + 4.0           # own title and closing card
    assert abs(second["duration"] - expected) < 0.05
    assert abs(_duration(folder / "topic-02.mp4", "v:0") - expected) < 0.15
    assert abs(_duration(folder / "topic-02.mp4", "a:0") - expected) < 0.15
    assert sorted(second["files"]) == ["topic-02.de.vtt", "topic-02.jpg", "topic-02.mp4", "topic-02.txt"]
    cues = (folder / "topic-02.de.vtt").read_text(encoding="utf-8")
    assert "Wort21" in cues and "Wort19" not in cues and "00:00:04" in cues    # after the title card
    assert [c["title"] for c in first["chapters"]] == ["Abtastung", "Mitte"]
    assert (folder / "topic-01.txt").read_text(encoding="utf-8").startswith("Abtastung\n\nWie aus einem Signal")
    assert not products.due(folder) and len(products.finished(folder)) == 2

    names = dict(publish.package_names(folder))
    assert names["topic-01.mp4"] == "2026-11-12 - Modul - Signale - Abtastung.mp4"
    assert names["topic-02.de.vtt"] == "2026-11-12 - Modul - Signale - Mitte bis Schluss.de.vtt"
    publish.make_package(folder, tmp_path / "p.zip")
    assert publish.current(folder)

    products.change(folder, "t2", {"state": "rejected"})               # no longer wanted
    products.approve(folder)
    assert products.due(folder)
    report = products.render(folder)
    assert [v["name"] for v in report["videos"]] == ["topic-01"] and not list(folder.glob("topic-02.*"))
    assert not publish.current(folder)                                 # the package has a video too many

    render.run(folder)                                                  # a new long version, e.g. a new style
    assert products.due(folder) and products.finished(folder) == []    # its topic videos follow it


def test_the_runner_proposes_after_the_render_and_renders_after_the_second_approval(tmp_path, monkeypatch):
    import pipeline_runner
    monkeypatch.setattr(products, "SHORTEST", 10.0)
    folder = _folder(tmp_path)
    (folder / "pipeline.json").write_text(json.dumps({"steps": {"transcribe": {"state": "done"}, "subtitles": {
        "state": "done"}, "cut": {"state": "done"}}}), encoding="utf-8")
    rendered = []
    monkeypatch.setattr(pipeline_runner.products, "render", lambda f, *a, **k: rendered.append(f) or
                        (f / products.MADE).write_text(json.dumps({"videos": [
                            {"id": x["id"], "name": "topic-01", "files": [], "signature": products.signature(f, x)}
                            for x in products.accepted(products.load(f))]}), encoding="utf-8"))
    monkeypatch.setattr(pipeline_runner.summary, "current", lambda f: True)
    settings = {"topic_videos": "propose"}
    r = pipeline_runner.PipelineRunner(tmp_path / "q.json", lambda: True, lambda m: [], lambda f: settings)
    r.enqueue(folder)
    assert r._process(folder) and (folder / "render.json").exists()   # the long version first
    assert r._process(folder) and products.load(folder)["products"]    # then the proposal
    assert r.describe(folder) == "Geschnittenes Video fertig"
    r._process(folder)
    assert rendered == [] and not r.queued(folder)                     # waits for the second approval
    products.approve(folder)
    r.enqueue(folder)
    assert r._process(folder) and rendered == [folder]
    r._process(folder)
    assert rendered == [folder] and not r.queued(folder)


def test_without_the_switch_nothing_is_proposed(tmp_path, monkeypatch):
    import pipeline_runner
    folder = _folder(tmp_path, inputs=[])
    (folder / "pipeline.json").write_text(json.dumps({"steps": {"transcribe": {"state": "done"}, "subtitles": {
        "state": "done"}, "cut": {"state": "done"}}}), encoding="utf-8")
    monkeypatch.setattr(pipeline_runner.summary, "current", lambda f: True)
    r = pipeline_runner.PipelineRunner(tmp_path / "q.json", lambda: True, lambda m: [], lambda f: {"topic_videos": "off"})
    r.enqueue(folder)
    while r.queued(folder):
        r._process(folder)
    assert not (folder / products.FILE).exists()
    data = products.propose(folder, {})                                 # by hand, from the window
    assert data["products"] == [] and "keine Themenstücke" in data["notes"][0]


def test_pieces_of_every_kind_and_the_clips_of_an_old_list_are_proposed(tmp_path, monkeypatch):
    monkeypatch.setattr(products, "SHORTEST", 10.0)
    folder = _folder(tmp_path, inputs=[])
    (folder / "outline.json").write_text(json.dumps({"pieces": [
        {"start": 2.0, "end": 14.0, "kind": "demo", "title": "Das Werkzeug", "summary": "Vorgeführt."},
        {"start": 26.0, "end": 38.0, "kind": "example", "title": "Ein Beispiel", "summary": ""}]}), encoding="utf-8")
    e = edit.load(folder)
    edit.add(e, edit.item("clip", 3, 13, "", "agent", 0.8, "accepted", title="Doppelt"))      # inside a piece
    edit.add(e, edit.item("clip", 15, 28, "", "agent", 0.8, "accepted", title="Eigener Clip"))
    e["approved"] = "2026-11-12T11:00:00+01:00"
    edit.save(folder, e)
    data = products.propose(folder, {})
    assert [(x["title"], x["kind"], x["source"]) for x in data["products"]] == [
        ("Das Werkzeug", "demo", "piece"), ("Eigener Clip", "clip", "clip"), ("Ein Beispiel", "example", "piece")]


def test_the_window_proposes_changes_and_approves_through_the_server(tmp_path, monkeypatch):
    from shelf import shelf
    monkeypatch.setattr(products, "SHORTEST", 10.0)
    folder = _folder(tmp_path)
    r = shelf(tmp_path, {"x": folder})
    e = edit.load(folder)
    e.pop("approved")
    edit.save(folder, e)
    assert r.propose_topics("x") == "Themenvideos entstehen aus der freigegebenen Langfassung."
    edit.approve(folder)
    assert r.propose_topics("x") == "" and products.load(folder)["products"][0]["state"] == "proposed"
    assert r.change_topic("x", "t1", {"title": "Abtastung und Quantisierung"}) == ""
    assert r.add_topic("x", 20, 40, "Mitte") == ""
    assert r.approve_topics("x") == "" and r.pipeline.queued(folder)
    view = r.recording("x")["cut"]
    assert "topics" not in view                                      # shown once the long version is rendered
    data = products.load(folder)
    assert data["approved"] and [x["title"] for x in data["products"]] == ["Abtastung und Quantisierung", "Mitte"]
    from flex3_light_server import my_recordings
    assert {"propose_topics", "change_topic", "add_topic", "approve_topics"} <= set(my_recordings.CALLS)


def test_a_block_is_a_video_of_its_own_above_the_videos_of_its_pieces(tmp_path, monkeypatch):
    monkeypatch.setattr(products, "SHORTEST", 5.0)
    folder = _folder(tmp_path, inputs=[])
    (folder / "outline.json").write_text(json.dumps({
        "pieces": [{"start": 2.0, "end": 12.0, "kind": "lecture", "title": "Teil eins", "summary": ""},
                   {"start": 12.0, "end": 24.0, "kind": "lecture", "title": "Teil zwei", "summary": ""},
                   {"start": 28.0, "end": 38.0, "kind": "demo", "title": "Für sich", "summary": ""}],
        "blocks": [{"start": 2.0, "end": 24.0, "title": "Der ganze Foliensatz", "summary": "Am Stück."}]}),
        encoding="utf-8")
    data = products.propose(folder, {})
    assert [(x["id"], x["title"], x["source"], x.get("parent")) for x in data["products"]] == [
        ("t1", "Der ganze Foliensatz", "block", None), ("t2", "Teil eins", "piece", "t1"),
        ("t3", "Teil zwei", "piece", "t1"), ("t4", "Für sich", "piece", None)]


def test_a_part_that_fills_its_block_is_not_proposed_twice(tmp_path, monkeypatch):
    monkeypatch.setattr(products, "SHORTEST", 5.0)
    folder = _folder(tmp_path, inputs=[])
    (folder / "outline.json").write_text(json.dumps({
        "pieces": [{"start": 3.0, "end": 20.0, "kind": "quiz", "title": "Quiz", "summary": ""},
                   {"start": 24.0, "end": 30.0, "kind": "lecture", "title": "Ein Teil", "summary": ""}],
        "blocks": [{"start": 2.0, "end": 21.0, "title": "Wiederholung", "summary": ""},
                   {"start": 22.0, "end": 38.0, "title": "A Map of Meaning", "summary": "Mit Experiment."}]}),
        encoding="utf-8")
    data = products.propose(folder, {})
    assert [(x["title"], x.get("parent")) for x in data["products"]] == [
        ("Wiederholung", None), ("A Map of Meaning", None), ("Ein Teil", "t2")]


class _Model:
    """Answers like the Responses API with the tool calls given, one list per round."""
    model = "fake"

    def __init__(self, rounds):
        self.rounds, self.bodies = rounds, []

    def create(self, body):
        self.bodies.append(body)
        calls = self.rounds[min(len(self.bodies), len(self.rounds)) - 1]
        return {"id": f"r{len(self.bodies)}", "usage": {"input_tokens": 100, "output_tokens": 10},
                "output": [{"type": "function_call", "call_id": f"c{len(self.bodies)}{k}", "name": n,
                            "arguments": json.dumps(a)} for k, (n, a) in enumerate(calls)]}


def _tree(tmp_path, monkeypatch):
    monkeypatch.setattr(products, "SHORTEST", 5.0)
    folder = _folder(tmp_path, inputs=[])
    (folder / "outline.json").write_text(json.dumps({
        "pieces": [{"start": 2.0, "end": 12.0, "kind": "lecture", "title": "Teil eins", "summary": ""},
                   {"start": 12.0, "end": 24.0, "kind": "lecture", "title": "Teil zwei", "summary": ""},
                   {"start": 28.0, "end": 38.0, "kind": "demo", "title": "Für sich", "summary": ""}],
        "blocks": [{"start": 2.0, "end": 26.0, "title": "Der ganze Foliensatz", "summary": ""}]}), encoding="utf-8")
    (folder / "transcript.json").write_text(json.dumps({"language": "de", "words": [], "segments": [
        {"start": 11.5, "end": 13.0, "text": "Jetzt zum Zweiten."}]}), encoding="utf-8")
    products.propose(folder, {})
    products.approve(folder)
    return folder


def test_feedback_in_words_merges_splits_and_asks_for_a_new_approval(tmp_path, monkeypatch):
    from pipeline import llm
    folder = _tree(tmp_path, monkeypatch)
    model = _Model([
        [("read_transcript", {"start": 8, "end": 16})],
        [("merge_videos", {"ids": ["t2", "t3"], "title": "Beide Teile"}), ("set_selected", {"id": "t4", "selected": False}),
         ("split_video", {"id": "t1", "at": 3, "titles": ["a", "b"]})],
        [("split_video", {"id": "t2", "at": 13.0, "titles": ["Erster", "Zweiter"]}),
         ("finish", {"reply": "Zusammengelegt und beim Zweiten wieder geteilt."})]])
    client = llm.OpenAIResponses("k", "fake", post=lambda url, body, headers, timeout: model.create(body))
    entry = products.revise(folder, "Lege Nr. 2 und 3 zusammen, teile bei 'Jetzt zum Zweiten', Nr. 4 brauche ich nicht",
                            None, client=client)
    data = products.load(folder)
    assert [(x["id"], x["title"], x["spans"][0], x.get("parent"), x["state"]) for x in data["products"]] == [
        ("t1", "Der ganze Foliensatz", [2.0, 26.0], None, "accepted"),
        ("t2", "Erster", [2.0, 13.0], "t1", "accepted"), ("t5", "Zweiter", [13.0, 24.0], "t1", "accepted"),
        ("t4", "Für sich", [28.0, 38.0], None, "rejected")]
    assert data["approved"] is None and data["retired"] == ["t3"] and data["feedback"][-1] == entry
    assert entry["reply"].startswith("Zusammengelegt") and len(entry["changes"]) == 3
    sent = json.dumps(model.bodies, ensure_ascii=False)
    assert "11.5–13.0 Jetzt zum Zweiten." in sent and "split t1 inside its span" in sent
    assert "Der ganze Foliensatz" in model.bodies[0]["input"][0]["content"][0]["text"]


def test_the_window_asks_for_the_outline_anew_and_for_feedback(tmp_path, monkeypatch):
    from shelf import shelf
    import pipeline_runner
    folder = _tree(tmp_path, monkeypatch)
    r = shelf(tmp_path, {"x": folder})
    assert r.revise_topics("x", "  ") == "Schreib zuerst, was sich ändern soll."
    assert r.revise_topics("x", "Nr. 4 weg") == "" and r.redo_outline("x") == ""
    state = pipeline_runner.read_state(folder)
    assert state["topics_feedback"] == "Nr. 4 weg" and state["outline_requested"] is True
    from flex3_light_server import my_recordings
    assert {"redo_outline", "revise_topics"} <= set(my_recordings.CALLS)


def test_the_runner_makes_the_outline_anew_without_touching_the_cut(tmp_path, monkeypatch):
    import pipeline_runner
    folder = _tree(tmp_path, monkeypatch)
    (folder / "pipeline.json").write_text(json.dumps({"steps": {"transcribe": {"state": "done"}, "subtitles": {
        "state": "done"}, "cut": {"state": "done"}}, "outline_requested": True}), encoding="utf-8")
    render.run(folder)
    before = (folder / "edit.json").read_text(encoding="utf-8")
    screened, outlined = [], []
    monkeypatch.setattr(pipeline_runner.screening, "run", lambda f, *a, **k: screened.append(k.get("privacy")) or
                        (f / "overview.json").write_text("{}", encoding="utf-8"))

    def outline_run(f, *a, **k):
        outlined.append(f)
        o = json.loads((f / "outline.json").read_text(encoding="utf-8"))
        o["blocks"] = []
        (f / "outline.json").write_text(json.dumps(o), encoding="utf-8")
        return o
    monkeypatch.setattr(pipeline_runner.outline, "run", outline_run)
    r = pipeline_runner.PipelineRunner(tmp_path / "q.json", lambda: True, lambda m: [], lambda f: {})
    assert r._process(folder)
    assert screened == [False] and outlined == [folder] and not (folder / "privacy.json").exists()
    assert [x["title"] for x in products.load(folder)["products"]] == ["Teil eins", "Teil zwei", "Für sich"]
    assert (folder / "edit.json").read_text(encoding="utf-8") == before
    assert "outline_requested" not in pipeline_runner.read_state(folder)
    assert pipeline_runner.read_state(folder)["steps"]["topics"]["message"].startswith("Gliederung neu, 3")
