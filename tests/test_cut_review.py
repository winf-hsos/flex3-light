"""Stage E: the cut checked on its rendered result, without a network."""
from __future__ import annotations

import json
import wave

import numpy as np

from pipeline import cut_review, edit, files, locks, render, timeline
from test_cut import WORDS, _session, responses


class Ear:
    """Hears what it is told, at the start of the file sent to it."""

    def __init__(self, words):
        self.words, self.calls = words, 0

    def transcribe_file(self, mp3, language, terms, speakers=False):
        self.calls += 1
        assert mp3.exists() and mp3.stat().st_size > 0
        return {"text": "", "words": self.words, "segments": []}


class Reader:
    """A model that reports junction 1 as referring to something cut away."""
    model = "fake"

    def __init__(self):
        self.bodies = []

    def create(self, body):
        self.bodies.append(body)
        args = {"problems": [{"junction": 1, "kind": "refers_to_cut", "note": "Verweist auf die entfernte Demo."}]}
        return {"id": "r1", "usage": {"input_tokens": 50, "output_tokens": 10},
                "output": [{"type": "function_call", "call_id": "c1", "name": "report_transitions",
                            "arguments": json.dumps(args)}]}


def test_junctions_clipped_words_and_comparison():
    e = edit.new(60.0)
    edit.add(e, edit.item("cut", 6.2, 9.2, "Test", "agent", 0.8, "accepted"))    # runs through "man" and "Heute"
    p = timeline.plan(e)
    js = cut_review.junctions(p)
    assert len(js) == 1 and js[0]["left"] == timeline.plan(e)["kept"][0][1] and js[0]["out"] == js[0]["left"]
    starts = [w["start"] for w in WORDS]
    assert [w["word"] for w in cut_review.clipped(WORDS, starts, js[0])] == ["man", "Heute"]
    near = [{"word": "Hallo,", "out": 5.0}, {"word": "hört", "out": 5.5}, {"word": "geht", "out": 6.3},
            {"word": "es", "out": 6.8}]
    assert cut_review.compare(near, near, 6.2)[0] == 1.0
    ratio, expected, heard = cut_review.compare(near, [{"word": "Hallo", "out": 5.0}, {"word": "äh", "out": 6.2}], 6.2)
    assert ratio < cut_review.SIMILAR and heard == "Hallo äh"
    assert cut_review.compare(near[:1], [], 6.2) is None                        # too little to say anything
    split = [{"word": "auf", "out": 6.0}, {"word": "das", "out": 6.2}, {"word": "Ergebnis", "out": 6.6}]
    joined = [{"word": "aufs", "out": 6.0}, {"word": "Ergebnis", "out": 6.5}]
    assert cut_review.compare(split, joined, 6.2)[0] >= cut_review.SIMILAR    # another transcription, the same sound


def test_window_of_the_edit_list_for_a_part_of_the_render():
    e = edit.new(60.0)
    edit.add(e, edit.item("freeze", 8.0, 14.0, "Standbild", "agent", 0.8, "accepted"))
    edit.add(e, edit.item("cut", 30.0, 40.0, "weg", "agent", 0.8, "accepted"))
    w = render.window(e, 10.0, 5.0)
    assert w["duration"] == 5.0 and len(w["items"]) == 1
    frozen = w["items"][0]
    assert (frozen["start"], frozen["end"], frozen["source_start"]) == (0.0, 4.0, 8.0)   # holds the frame of 8 s


def test_frame_goes_through_the_render_graph(tmp_path):
    folder = _session(tmp_path)
    e = edit.new(60.0)
    edit.add(e, edit.item("blur", 10.0, 20.0, "Mail", "agent", 0.8, "accepted", "privacy", region=[0.1, 0.1, 0.3, 0.2]))
    work = tmp_path / "work"
    work.mkdir()
    png = cut_review.frame(folder, e, 15.0, work, "f0")
    assert png is not None and png.stat().st_size > 1000


def test_a_lock_heard_in_the_result_stays_open_until_its_items_change(tmp_path):
    folder = _session(tmp_path)
    files.write_json(folder / "privacy.json", {"checked": {"sound": True}, "candidates": [], "locks": [
        {"id": "S1", "kind": "room_voice", "sense": "sound", "start": 20.0, "end": 30.0, "text": "Frage", "region": None}]})
    e = edit.new(60.0)
    edit.add(e, edit.item("mute", 20.0, 29.8, "Stimme", "agent", 0.8, "accepted", "privacy"))  # 0.2 s short: within SLACK
    edit.add(e, edit.item("cut", 40.0, 45.0, "Demo weg", "agent", 0.8, "accepted"))
    edit.save(folder, e)
    assert locks.state(folder, e)[0]["status"] == "hidden"
    ear, reader = Ear([]), Reader()
    r = cut_review.run(folder, "k", ear, {}, client=responses(reader))
    kinds = [f["kind"] for f in r["findings"]]
    assert "lock_audible" in kinds and "refers_to_cut" in kinds and ear.calls == 1
    assert r["junctions"] == 1 and "S1" in r["seen"]
    assert "Before:" in reader.bodies[0]["input"][0]["content"][0]["text"]
    lock = locks.state(folder, e)[0]
    assert lock["status"] == "open" and lock["seen"]                          # blocks approval and render
    assert locks.open_locks(folder, e)
    edit.find(e, next(x["id"] for x in e["items"] if x["kind"] == "mute"))["end"] = 30.0
    assert locks.state(folder, e)[0]["status"] == "hidden"                    # the items changed: check again
    v = cut_review.view(folder, e)
    assert v["current"] is False and v["findings"]


def test_a_failing_check_leaves_the_proposal_alone(tmp_path):
    folder = _session(tmp_path)
    r = cut_review.check_and_fix(folder, "k", None, {})                        # no edit list: the check fails
    assert r["error"].startswith("Prüfung fehlgeschlagen")
    assert files.read_json(folder / "review.json")["error"]


def _wav(path, samples, rate=16000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())


def test_a_quiet_voice_counts_against_the_noise_floor(tmp_path):
    rate, rng = 16000, np.random.default_rng(1)
    noise = rng.normal(0, 10 ** (-70 / 20), 40 * rate)                   # a room at -70 dB
    noise[20 * rate:30 * rate] = 0.0                                        # muted
    voice = noise.copy()
    voice[29 * rate:int(29.3 * rate)] += 10 ** (-58 / 20) * np.sin(np.arange(int(0.3 * rate)) * 0.2)  # 0.3 s at -61 dB
    _wav(tmp_path / "quiet.wav", noise)
    _wav(tmp_path / "voice.wav", voice)
    assert cut_review.audible(tmp_path / "quiet.wav", 20.1, 29.9, 40.0) is None
    assert cut_review.audible(tmp_path / "quiet.wav", 30.5, 35.0, 40.0) is None   # the room itself is not a voice
    db = cut_review.audible(tmp_path / "voice.wav", 20.1, 29.9, 40.0)
    assert db is not None and db < cut_review.LOUD_DB                        # quieter than -45 dB, heard all the same


def test_a_fix_pass_counts_what_is_gone_by_place_not_by_junction_number():
    a = {"kind": "clipped_word", "at": 100.0, "junction": 3}
    assert cut_review._same(a, {"kind": "clipped_word", "at": 101.0, "junction": 2})
    assert not cut_review._same(a, {"kind": "clipped_word", "at": 140.0, "junction": 3})
    assert cut_review._same({"kind": "lock_visible", "at": 5.0, "lock": "S1"},
                            {"kind": "lock_visible", "at": 9.0, "lock": "S1"})
