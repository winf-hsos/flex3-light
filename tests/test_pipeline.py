"""The pipeline steps without a network: audio pieces, invented text, punctuation, subtitles."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import audio, subtitles, transcribe  # noqa: E402


def test_pieces_end_in_pauses():
    quiet = [(590.0, 591.0), (1190.0, 1190.6), (1700.0, 1701.0)]
    pieces = audio.plan(2000.0, quiet)
    assert pieces[0] == (0.0, 590.5) and pieces[1][1] == 1190.3 and pieces[-1][1] == 2000.0
    assert audio.plan(500.0, []) == [(0.0, 500.0)]
    assert audio.plan(2000.0, [])[0] == (0.0, 900.0)              # no pause: hard cut at the limit


def test_prompt_and_invented_text():
    p = transcribe.prompt_for(["LiFi", "Claude Code"], "Wir haben gestern über Sensoren gesprochen und heute " * 30)
    assert p.startswith("Fachbegriffe: LiFi, Claude Code.") and len(p) <= transcribe.PROMPT_LIMIT
    assert transcribe.invented({"text": "Thank you. Thank you."}, "")
    assert transcribe.invented({"text": "Untertitel im Auftrag des ZDF"}, "")
    assert not transcribe.invented({"text": "Heute geht es um LiFi und Licht."}, "")
    prompt = "Fachbegriffe: LiFi. und dann haben wir das Licht moduliert, um Daten zu senden"
    assert transcribe.invented({"text": "Text davor " + prompt.lower()}, prompt)


def test_punctuation_comes_from_the_text():
    words = [{"word": "heute", "start": 0, "end": 1}, {"word": "geht", "start": 1, "end": 2},
             {"word": "es", "start": 2, "end": 3}, {"word": "um", "start": 3, "end": 4},
             {"word": "LiFi", "start": 4, "end": 5}, {"word": "gut", "start": 6, "end": 7}]
    out = transcribe.punctuate(words, "Heute geht es um LiFi. Gut!")
    assert [w["word"] for w in out] == ["Heute", "geht", "es", "um", "LiFi.", "Gut!"]


def test_subtitle_cues():
    words = [{"word": f"Wort{i}{'.' if i == 5 else ''}", "start": i * 0.4, "end": i * 0.4 + 0.3} for i in range(30)]
    cues = subtitles.cues(words)
    assert cues[0][2].replace("\n", " ").endswith("Wort5.")       # a sentence end closes the cue
    assert all(len(line) <= subtitles.LINE for _, _, t in cues for line in t.split("\n"))
    assert all(end - start <= subtitles.LONGEST for start, end, _ in cues)


class FakeWhisper:
    """Pretends to transcribe: one sentence per piece, words spread over its length."""
    name = "fake"

    def __init__(self):
        self.calls = []

    def transcribe(self, mp3, language, prompt):
        self.calls.append(prompt)
        n = len(self.calls)
        if n == 1:
            return {"text": "Thank you.", "words": [], "segments": []}             # invented, asked again
        d = audio.duration(mp3)
        words = [{"word": w, "start": 1 + i * 0.4, "end": 1.3 + i * 0.4} for i, w in enumerate(["Das", "ist", "LiFi"])]
        return {"text": "Das ist LiFi.", "words": words, "segments": [{"start": 0, "end": d, "text": " Das ist LiFi."}]}


def test_run_writes_transcript_and_subtitles(tmp_path):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=12",
                    "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=12", "-shortest", "-c:v", "libx264",
                    "-preset", "ultrafast", "-c:a", "aac", str(tmp_path / "recording.mp4")],
                   stdin=subprocess.DEVNULL, capture_output=True, check=True)
    (tmp_path / "session.json").write_text(json.dumps({"session_id": "x", "language": "de"}), encoding="utf-8")
    fake = FakeWhisper()
    t = transcribe.run(tmp_path, fake, ["LiFi"])
    assert fake.calls[0].startswith("Fachbegriffe: LiFi") and fake.calls[1] == ""   # retried without prompt
    assert [w["word"] for w in t["words"]] == ["Das", "ist", "LiFi."]
    assert (tmp_path / "transcript.json").exists() and (tmp_path / "transcript.raw.json").exists()
    assert not (tmp_path / "work").exists()
    vtt = subtitles.run(tmp_path).read_text(encoding="utf-8")
    assert vtt.startswith("WEBVTT") and "Das ist LiFi." in vtt and " --> " in vtt


def test_runner_waits_for_key_and_for_the_recording(tmp_path, monkeypatch):
    import time as _time
    import pipeline_runner
    folder = tmp_path / "2026-10-27 - Test"
    folder.mkdir()
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=5",
                    "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=5", "-shortest", "-c:v", "libx264",
                    "-preset", "ultrafast", "-c:a", "aac", str(folder / "recording.mp4")],
                   stdin=subprocess.DEVNULL, capture_output=True, check=True)
    (folder / "session.json").write_text(json.dumps({"session_id": "x", "language": "de", "course_name": "M"}),
                                         encoding="utf-8")
    key = {"value": None}
    monkeypatch.setattr(pipeline_runner.services, "key", lambda service: key["value"] if service == "openai" else None)
    monkeypatch.setattr(pipeline_runner.transcribe, "OpenAIWhisper", lambda k: FakeWhisper())
    monkeypatch.setattr(pipeline_runner, "WAIT", 0.2)
    recording = {"on": True}
    r = pipeline_runner.PipelineRunner(tmp_path / "queue.json", lambda: not recording["on"], lambda meta: ["LiFi"])
    r.start()
    r.enqueue(folder)
    _time.sleep(1)
    assert r.describe(folder) == "Transkription folgt"            # nothing happens while recording
    recording["on"] = False
    r.wake.set()
    deadline = _time.time() + 20
    while "wartet" not in r.describe(folder) and _time.time() < deadline:
        _time.sleep(0.1)
    assert "wartet auf einen Schlüssel für AssemblyAI oder OpenAI" in r.describe(folder)
    key["value"] = "sk-test"
    r.wake.set()
    while r.describe(folder) != "Transkription und Untertitel fertig" and _time.time() < deadline + 20:
        _time.sleep(0.1)
    assert r.describe(folder) == "Transkription und Untertitel fertig"
    assert (folder / "subtitles.de.vtt").exists() and json.loads((tmp_path / "queue.json").read_text()) == []


def _video(path, seconds):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=300:duration={seconds}",
                    "-f", "lavfi", "-i", f"testsrc2=s=320x180:r=30:d={seconds}", "-shortest", "-c:v", "libx264",
                    "-preset", "ultrafast", "-c:a", "aac", str(path)],
                   stdin=subprocess.DEVNULL, capture_output=True, check=True)


class FakeAssemblyAI:
    """Takes the whole file; returns punctuated words and sentences, or fails if told to."""
    name = "fake-assemblyai"
    whole = True

    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def transcribe_file(self, mp3, language, terms):
        self.calls.append((language, terms))
        if self.fail:
            raise transcribe.TranscriptionError("AssemblyAI antwortet mit Fehler 500")
        words = [{"word": w, "start": 1 + i * 0.4, "end": 1.3 + i * 0.4} for i, w in enumerate(["Das", "ist", "LiFi."])]
        return {"text": "Das ist LiFi.", "words": words, "segments": [{"start": 1, "end": 2.1, "text": "Das ist LiFi."}]}


def test_whole_file_transcription(tmp_path):
    _video(tmp_path / "recording.mp4", 6)
    (tmp_path / "session.json").write_text(json.dumps({"session_id": "x", "language": "de"}), encoding="utf-8")
    fake = FakeAssemblyAI()
    t = transcribe.run(tmp_path, fake, ["LiFi"])
    assert fake.calls == [("de", ["LiFi"])]                       # one request, terms as they are
    assert t["model"] == "fake-assemblyai" and [w["word"] for w in t["words"]] == ["Das", "ist", "LiFi."]
    assert t["segments"] == [{"start": 1, "end": 2.1, "text": "Das ist LiFi."}] and not (tmp_path / "work").exists()


def test_assemblyai_adapter_converts_and_deletes(tmp_path, monkeypatch):
    calls = []
    answers = iter([{"upload_url": "https://cdn/x"}, {"id": "t1", "status": "queued"},
                    {"id": "t1", "status": "completed", "text": "Hallo Welt.",
                     "words": [{"text": "Hallo", "start": 100, "end": 400, "confidence": 0.99},
                               {"text": "Welt.", "start": 450, "end": 900, "confidence": 0.4321}]},
                    {"sentences": [{"start": 100, "end": 900, "text": "Hallo Welt."}]}, {}])
    a = transcribe.AssemblyAI("secret", "https://api.eu.assemblyai.com/")
    monkeypatch.setattr(a, "_call", lambda method, path, body=None, raw=None: calls.append((method, path, body))
                        or next(answers))
    monkeypatch.setattr(transcribe.AssemblyAI, "POLL", 0)
    mp3 = tmp_path / "a.mp3"
    mp3.write_bytes(b"x")
    r = a.transcribe_file(mp3, "de", ["Exposé", "a phrase with far more than six words in it"])
    assert calls[1][2]["speech_models"] == ["universal-3-pro"] and calls[1][2]["keyterms_prompt"] == ["Exposé"]
    assert r["words"][1] == {"word": "Welt.", "start": 0.45, "end": 0.9, "confidence": 0.432} and r["segments"][0]["text"] == "Hallo Welt."
    assert calls[-1][:2] == ("DELETE", "/v2/transcript/t1") and "secret" not in repr(a)


def test_runner_falls_back_to_whisper(tmp_path, monkeypatch):
    import pipeline_runner
    folder = tmp_path / "2026-10-27 - Test"
    folder.mkdir()
    _video(folder / "recording.mp4", 5)
    (folder / "session.json").write_text(json.dumps({"session_id": "x", "language": "de"}), encoding="utf-8")
    failing, whisper = FakeAssemblyAI(fail=True), FakeWhisper()
    r = pipeline_runner.PipelineRunner(tmp_path / "queue.json", lambda: True, lambda meta: ["LiFi"])
    monkeypatch.setattr(r, "_transcribers", lambda: [failing, whisper])
    r.enqueue(folder)
    assert r._process(folder)
    assert failing.calls and whisper.calls
    step = pipeline_runner.read_state(folder)["steps"]["transcribe"]
    assert step["state"] == "done" and step["model"] == "fake"


def test_redo_queues_the_transcription_again(tmp_path):
    import pipeline_runner
    folder = tmp_path / "2026-10-27 - Test"
    folder.mkdir()
    (folder / "pipeline.json").write_text(json.dumps({"steps": {"transcribe": {"state": "done"},
                                                                "subtitles": {"state": "done"}}}), encoding="utf-8")
    r = pipeline_runner.PipelineRunner(tmp_path / "queue.json", lambda: False, lambda meta: [])
    r.failed.add(str(folder))
    r.active = str(folder)
    assert not r.redo(folder)                                      # not while it is being worked on
    r.active = None
    assert r.redo(folder)
    assert pipeline_runner.read_state(folder)["steps"] == {} and str(folder) not in r.failed
    assert r.describe(folder) == "Transkription folgt"
