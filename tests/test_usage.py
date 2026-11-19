"""What a recording cost: every model call is counted for the recording the pipeline works on."""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import llm, usage_log  # noqa: E402


def reply(input_tokens=1000, cached=0, output=100):
    return {"id": "r1", "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            "usage": {"input_tokens": input_tokens, "input_tokens_details": {"cached_tokens": cached},
                      "output_tokens": output, "output_tokens_details": {"reasoning_tokens": 0}}}


def test_calls_are_counted_for_the_recording_in_work_in_every_thread(tmp_path):
    config = llm.stage_config("section")
    client = llm.make(config, lambda s: "key")
    client._post = lambda url, body, headers, timeout: reply(2_000_000, 1_000_000, 100_000)
    outside = llm.OpenAIResponses("key", "gpt-6-luna", stage="section_light",
                                  post=lambda *a: reply())
    outside.complete("x", [llm.Message("user", [llm.Text("hi")])], [])    # no recording in work: not counted
    with usage_log.working_on(tmp_path):
        t = threading.Thread(target=lambda: client.complete("x", [llm.Message("user", [llm.Text("hi")])], []))
        t.start(); t.join()                                              # a section runs in its own thread
        usage_log.note_audio("transcribe", "assemblyai/universal-3-pro", 3600, terms=True)
    lines = [json.loads(x) for x in (tmp_path / usage_log.FILE).read_text(encoding="utf-8").splitlines()]
    assert [(x["step"], x["model"]) for x in lines] == [("section", "gpt-6.1-sol"),
                                                       ("transcribe", "assemblyai/universal-3-pro")]
    v = usage_log.view(tmp_path)
    cut, audio = [r for r in v["rows"] if r["step"] == "cut"][0], [r for r in v["rows"] if r["step"] == "transcribe"][0]
    # 1 Mio. fresh at $2, 1 Mio. cached at $0.10, 0.1 Mio. out at $10
    assert cut["label"] == "Schnitt" and abs(cut["cost"] - 3.10) < 1e-6
    assert abs(audio["cost"] - 0.26) < 1e-6 and audio["seconds"] == 3600      # with the word list
    assert abs(v["total"] - 3.36) < 1e-6 and v["estimated"] == [] and v["unpriced"] == []


def test_old_recordings_are_rebuilt_from_their_files_without_counting_twice(tmp_path):
    (tmp_path / "transcript.json").write_text(json.dumps({"model": "openai/whisper-1", "duration": 1800, "terms": []}))
    (tmp_path / "outline.json").write_text(json.dumps({"agent": {"model": "gpt-6.1-sol", "usage": {
        "input_tokens": 50_000, "cached_tokens": 0, "output_tokens": 5000}}}))
    rounds = [{"event": "round", "stage": "cut", "model": "gpt-6.1-sol", "usage": {"input_tokens": 10_000}},
              {"event": "round", "stage": "section 2", "model": "gpt-6-luna", "usage": {"input_tokens": 10_000}},
              {"event": "tool", "name": "cut"}]
    (tmp_path / "edit.log.jsonl").write_text("\n".join(json.dumps(r) for r in rounds))
    (tmp_path / "summary.json").write_text("{}")
    v = usage_log.view(tmp_path)
    assert {(r["label"], r["model"]) for r in v["rows"]} == {
        ("Transkription", "openai/whisper-1"), ("Gliederung", "gpt-6.1-sol"), ("Schnitt", "gpt-6.1-sol"),
        ("Schnitt", "gpt-6-luna")}
    assert v["estimated"] == ["Gliederung", "Schnitt", "Transkription"] and v["missing"] == ["Zusammenfassung"]
    # once the cut is counted in the list, its rebuilt rounds no longer count
    with usage_log.working_on(tmp_path):
        usage_log.note_tokens("section", "gpt-6.1-sol", {"input_tokens": 1000})
    v = usage_log.view(tmp_path)
    cut = [r for r in v["rows"] if r["step"] == "cut"]
    assert [(r["model"], r["calls"], r["input"]) for r in cut] == [("gpt-6.1-sol", 1, 1000)]
    assert "Schnitt" not in v["estimated"]


def test_a_model_without_price_is_named_and_left_out_of_the_total(tmp_path, monkeypatch):
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({"local-llama": {"input": 0, "cached": 0, "output": 0}}))
    monkeypatch.setattr(usage_log, "price_file", prices)
    with usage_log.working_on(tmp_path):
        usage_log.note_tokens("topics", "made-up-model", {"input_tokens": 1000})
        usage_log.note_tokens("translate", "local-llama", {"input_tokens": 1000})
    v = usage_log.view(tmp_path)
    assert v["unpriced"] == ["made-up-model"] and v["total"] == 0
    assert [r["label"] for r in v["rows"]] == ["Themenvideos", "Sprachen"]


def test_nothing_spent_shows_nothing(tmp_path):
    assert usage_log.view(tmp_path) is None
