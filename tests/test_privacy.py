"""Stages A and B of the cut (screening.py) and the hard privacy locks (locks.py), without a network."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline import cut_agent, cut_tools, edit, locks, render, screening  # noqa: E402

WORDS = [{"word": w, "start": s, "end": s + 0.4} for w, s in
         [("Hallo,", 1.0), ("heute", 1.5), ("geht", 2.0), ("es", 2.5), ("los.", 3.0), ("Weiter", 25.0), ("geht's.", 25.6)]]


def line(text, x, y, h=20):
    """An OCR line: words side by side, each as wide as its letters."""
    words, cx = [], x
    for w in text.split():
        words.append({"text": w, "x": cx, "y": y, "w": 10 * len(w), "h": h})
        cx += 10 * len(w) + 8
    return {"text": text, "words": words}


def read(*lines):
    return {"width": 1920, "height": 1080, "lines": list(lines)}


def test_the_rules_find_what_is_private_in_a_picture():
    found = screening.findings(read(
        line("Mail an max.muster@example.org senden", 100, 100),
        line("OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz123456", 100, 200),
        line("Matrikelnummer 1234567", 100, 300),
        line("Seite 3 von 12", 100, 400)))
    kinds = {(f["kind"], f["lock"]) for f in found}
    assert ("email", True) in kinds and ("secret", True) in kinds and ("student_number", True) in kinds
    mail = next(f for f in found if f["kind"] == "email")
    assert mail["text"] == "max.muster@example.org" and 0.08 < mail["region"][0] < 0.1
    numbers = screening.findings(read(line("4711001", 10, 10), line("4711002", 10, 40), line("4711003", 10, 70)))
    assert [(f["kind"], f["lock"]) for f in numbers] == [("number_list", True)]
    single = screening.findings(read(line("Bestellung 20261110", 10, 10)))
    assert [(f["kind"], f["lock"]) for f in single] == [("number", False)]                 # a hint only
    names = screening.findings(read(*[line(f"{n}, {v}", 10, 30 * i) for i, (n, v) in
                                      enumerate([("Meier", "Anna"), ("Schulz", "Ben"), ("Weber", "Clara"), ("Wolf", "Dana")])]))
    assert [(f["kind"], f["lock"]) for f in names] == [("name_list", False)]
    assert screening.findings(read(line("Kontakt: n.meseth@example.org", 10, 10)), allow=["n.meseth@"]) == []
    assert screening.findings(read(line("x <- obj@data.numeric", 10, 10))) == []           # R code, no address
    assert screening.findings(read(line("Mail: A.Muster@Uni-Example.DE", 10, 10)))[0]["kind"] == "email"


def test_a_slide_title_stands_out_and_a_program_has_none():
    slide = read(line("Datentransformation", 100, 80, h=60), line("Tabellen umformen", 100, 300), line("Spalten", 100, 340),
                 line("Zeilen", 100, 380), line("Werte", 100, 420))
    assert screening.slide_title(slide) == "Datentransformation"
    ide = read(*[line(t, 10, 5 + 25 * i) for i, t in enumerate(["File Edit Selection View", "library(tidyverse)",
                                                                   "df <- read_csv(x)", "summary(df)", "Console"])])
    assert screening.slide_title(ide) == ""


def test_findings_become_spans_over_the_frames():
    samples = [{"t": 0.0, "change": False}, {"t": 20.0, "change": False}, {"t": 31.5, "change": True},
               {"t": 40.0, "change": False}, {"t": 60.0, "change": False}]
    mail = {"kind": "email", "text": "a@b.de", "region": [0.1, 0.1, 0.1, 0.02], "lock": True}
    hits = [[], [], [mail], [{**mail, "region": [0.12, 0.1, 0.1, 0.02]}], []]
    [run] = screening._runs(samples, hits, 70.0)
    assert (run["start"], run["end"]) == (31.5, 60.0)               # from the picture change to the next frame without it
    assert run["region"] == [0.1, 0.1, 0.12, 0.02] and run["frames"] == 2
    hits = [[], [mail], [], [], []]
    [run] = screening._runs(samples, hits, 70.0)
    assert (run["start"], run["end"]) == (0.0, 31.5)                # appeared somewhere after the frame before


def test_voices_of_the_room_and_the_lecturer_split_by_the_labels():
    heard = ([{"word": w, "start": i * 0.5, "end": i * 0.5 + 0.4, "speaker": "A"} for i, w in enumerate("eins zwei drei vier fünf sechs".split())]
             + [{"word": w, "start": 10 + i * 0.4, "end": 10.3 + i * 0.4, "speaker": "B"} for i, w in enumerate("können Sie das wiederholen".split())]
             + [{"word": "ja", "start": 20.0, "end": 20.2, "speaker": "B"}])
    quiet = screening.voices(heard, [], lambda a, b: -40.0, -20.0)
    assert [(v["lock"], v["words"]) for v in quiet] == [(True, 4), (False, 1)]           # a question locks, "ja" is a hint
    assert quiet[0]["quieter"] == 20.0 and quiet[0]["text"] == "können Sie das wiederholen"
    main = [{"word": w, "start": 10 + i * 0.4, "end": 10.3 + i * 0.4} for i, w in enumerate("können Sie das wiederholen".split())]
    loud = screening.voices(heard, main, lambda a, b: -21.0, -20.0)
    assert loud[0]["lock"] is False                                  # as loud as the lecturer and in the transcript


def test_the_privacy_key_and_a_mailbox_lock_their_spans():
    found = screening.activity_locks([[0, 10, "POWERPNT.EXE", ""], [10, 14, "msedge.exe", "posteingang"],
                                      [14.5, 20, "msedge.exe", "posteingang"], [20, 20.5, "OUTLOOK.EXE", "inbox"]],
                                     [[30.0, 42.0]], ["posteingang", "inbox"])
    assert [(f["kind"], f["start"], f["end"], f["sense"]) for f in found] == \
        [("privacy_key", 30.0, 42.0, "both"), ("mailbox", 10, 20, "picture")]           # merged, the blip left out


def _folder(tmp_path, *found_locks, items=()):
    (tmp_path / "privacy.json").write_text(json.dumps({"checked": {"picture": True, "sound": True},
                                                       "locks": list(found_locks), "candidates": []}), encoding="utf-8")
    e = edit.new(60.0)
    for x in items:
        edit.add(e, x)
    edit.save(tmp_path, e)
    (tmp_path / "session.json").write_text("{}", encoding="utf-8")
    return e


VOICE = {"id": "S1", "kind": "room_voice", "sense": "sound", "start": 10.0, "end": 14.0, "region": None, "text": "eine Frage"}
MAIL = {"id": "S2", "kind": "email", "sense": "picture", "start": 20.0, "end": 30.0, "region": [0.2, 0.2, 0.1, 0.03],
        "text": "m…@e….org"}
KEY = {"id": "S3", "kind": "privacy_key", "sense": "both", "start": 40.0, "end": 45.0, "region": None, "text": ""}


def test_a_lock_is_hidden_only_where_nothing_of_it_stays():
    e = _folder(Path("."), VOICE) if False else None                    # (built below per case)
    accept = lambda kind, a, b, **p: edit.item(kind, a, b, "x", "agent", 0.9, "accepted", "privacy", **p)  # noqa: E731
    e = edit.new(60.0)
    assert locks.visible(VOICE, e) == [(10.0, 14.0)]
    e["items"] = [accept("mute", 9.8, 14.2)]
    assert locks.visible(VOICE, e) == []
    e["items"] = [accept("mute", 9.8, 12.0)]
    assert locks.visible(VOICE, e) == [(12.0, 14.0)]
    e["items"] = [accept("blur", 19, 31, region=[0.15, 0.15, 0.3, 0.2])]
    assert locks.visible(MAIL, e) == []
    e["items"] = [accept("blur", 19, 31, region=[0.25, 0.15, 0.3, 0.2])]                 # misses the left of the text
    assert locks.visible(MAIL, e) == [(20.0, 30.0)]
    e["items"] = [accept("mute", 40, 45)]                                                   # silent, but still seen
    assert locks.visible(KEY, e) == [(40.0, 45.0)]
    e["items"] = [accept("cut", 39.5, 45.5)]
    assert locks.visible(KEY, e) == []
    pause = edit.item("pause", 8.0, 16.0, "Pause", "rule", 0.7, "accepted", "pause", mode="shorten")
    e["items"] = [pause]                                                                    # its kept ends miss the voice
    assert locks.visible(VOICE, e) == []
    pause["params"]["mode"] = "card"
    e["items"] = [edit.item("pause", 9.0, 14.5, "Pause", "rule", 0.7, "accepted", "pause", mode="card")]
    assert locks.visible(VOICE, e) == []                                                    # the card covers its seconds


def test_the_approval_and_the_render_refuse_an_open_lock(tmp_path):
    _folder(tmp_path, VOICE, MAIL)
    error = edit.approve(tmp_path)
    assert "2 Datenschutz-Sperren" in error and "S1" in error
    e = edit.load(tmp_path)
    edit.add(e, locks.cover_item(VOICE, WORDS))
    e["lifted"] = {"S2": {"at": "now"}}
    edit.save(tmp_path, e)
    assert [x["status"] for x in locks.state(tmp_path)] == ["hidden", "lifted"]
    assert edit.approve(tmp_path) == ""
    e = edit.load(tmp_path)
    e.pop("lifted")
    edit.save(tmp_path, e)
    with pytest.raises(ValueError, match="S2"):
        render.run(tmp_path)                                                                 # the second barrier


def test_cover_items_hide_their_lock_and_never_clip_a_word():
    words = [{"word": "Frage", "start": 9.9, "end": 10.4}]
    mute = locks.cover_item(VOICE, words)
    assert mute["kind"] == "mute" and mute["start"] == 9.85 and mute["params"]["lock"] == "S1"
    blur = locks.cover_item(MAIL, [])
    assert blur["kind"] == "blur" and blur["params"]["region"] == [0.19, 0.19, 0.12, 0.05]
    cut = locks.cover_item(KEY, [])
    assert cut["kind"] == "cut" and cut["category"] == "privacy"
    for lock, item in ((VOICE, mute), (MAIL, blur), (KEY, cut)):
        assert locks.visible(lock, {"duration": 60.0, "items": [item]}) == []
    assert locks.mask("max.muster@example.org Matrikel 1234567") == "m…@e….org Matrikel 12…7"
    assert locks.mask("Token: Geheimwort", "secret") == "Token: Ge…"
    assert locks.mask("OPENAI_API_KEY=sk-proj-abc", "secret") == "OPENAI_API_KEY=sk…"


def test_the_lecturer_covers_or_lifts_a_lock_on_the_page(tmp_path):
    from shelf import shelf
    folder = tmp_path / "rec"
    folder.mkdir()
    _folder(folder, VOICE, MAIL)
    (folder / "transcript.json").write_text(json.dumps({"words": WORDS}), encoding="utf-8")
    c = shelf(tmp_path, {"x": folder})
    c.pipeline.enqueue = lambda f: None
    assert "Datenschutz-Sperren" in c.approve_cut("x")
    assert c.cover_lock("x", "S1") == "" and c.lift_lock("x", "S2") == ""
    view = __import__("library").cut_view(folder)
    assert [x["status"] for x in view["privacy"]["locks"]] == ["hidden", "lifted"]
    assert view["privacy"]["locks"][0]["label"] == "Stimme aus dem Raum"
    assert c.approve_cut("x") == ""
    assert c.lift_lock("x", "S2", False) == "" and "S2" in c.approve_cut("x")                # set again: blocked again
    assert c.lift_lock("x", "S9") == "Diese Sperre gibt es nicht mehr."


def test_the_agent_gets_the_locks_and_checks_them(tmp_path):
    from test_cut import _session
    folder = _session(tmp_path)
    (folder / "privacy.json").write_text(json.dumps({"checked": {"picture": True, "sound": False},
                                                     "locks": [VOICE], "candidates": [{**MAIL, "id": "K1"}]}),
                                         encoding="utf-8")
    b = cut_tools.Workbench(folder)
    try:
        text = cut_agent.CutAgent(b, None)._first_message()
        assert "S1 room_voice (sound) 10.0-14.0" in text and "[open]" in text and "K1 email" in text
        assert "the sound was not listened to" in text
        assert any("S1" in p for p in b.check())
    finally:
        b.close()
    assert "German" in cut_agent.instructions_for(set()) and "English" in cut_agent.instructions_for(set(), "en")


class Listener:
    """Answers like AssemblyAI with speaker labels: the lecturer, and a question from the room."""

    def transcribe_file(self, mp3, language, terms, speakers=False):
        assert speakers and mp3.stat().st_size > 0
        lecturer = [{"word": w, "start": 1.0 + i * 0.5, "end": 1.4 + i * 0.5, "speaker": "A"}
                    for i, w in enumerate("Hallo heute geht es los und weiter".split())]
        room = [{"word": w, "start": 12.0 + i * 0.4, "end": 12.3 + i * 0.4, "speaker": "B"}
                for i, w in enumerate("darf ich etwas fragen".split())]
        return {"words": lecturer + room}


def _slides(folder: Path) -> Path:
    """30 seconds: a slide, a mail client with an address from 10 to 20 s, the slide again."""
    font = ImageFont.truetype("arial.ttf", 46)
    pictures = []
    for name, lines, paper, ink in (("a", ["Datentransformation", "Heute: Tabellen umformen"], "white", "black"),
                                    ("b", ["Posteingang", "Von: max.muster@example.org", "Betreff: Abgabe"],
                                     (32, 36, 44), (235, 235, 235))):        # a mail client in dark mode
        img = Image.new("RGB", (1920, 1080), paper)
        d = ImageDraw.Draw(img)
        for i, text in enumerate(lines):
            d.text((160, 120 + i * 110), text, fill=ink, font=font)
        img.save(folder / f"{name}.png")
        pictures.append(folder / f"{name}.png")
    listing = folder / "list.txt"
    listing.write_text("".join(f"file '{p.name}'\nduration 10\n" for p in [pictures[0], pictures[1], pictures[0]])
                       + f"file '{pictures[0].name}'\n", encoding="utf-8")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                    "-f", "lavfi", "-i", "sine=frequency=300:duration=30", "-shortest", "-r", "10", "-pix_fmt", "yuv420p",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(folder / "recording.mp4")],
                   stdin=subprocess.DEVNULL, capture_output=True, check=True, cwd=folder)
    for p in pictures + [listing]:
        p.unlink()
    (folder / "session.json").write_text(json.dumps({"session_id": "x", "duration_sec": 30}), encoding="utf-8")
    (folder / "transcript.json").write_text(json.dumps({"language": "de", "duration": 30.0, "words": WORDS,
                                                        "segments": [{"start": 1.0, "end": 3.4, "text": "Hallo, heute geht es los."}]}),
                                            encoding="utf-8")
    (folder / "activity.json").write_text(json.dumps({"activity": [[0, 30, "POWERPNT.EXE", ""]], "private": [[26.0, 28.0]]}),
                                          encoding="utf-8")
    return folder


def test_screening_writes_the_overview_and_the_locks(tmp_path):
    folder = _slides(tmp_path)
    result = screening.run(folder, Listener(), {"cut_titles": ["posteingang"]})
    kinds = {x["kind"]: x for x in result["locks"]}
    assert "room_voice" in kinds and "privacy_key" in kinds
    voice = kinds["room_voice"]
    assert voice["sense"] == "sound" and 11.5 < voice["start"] < 12.0 and voice["text"] == "darf ich etwas fragen"
    over = json.loads((folder / "overview.json").read_text(encoding="utf-8"))
    assert over["blocks"][0]["words"] == 7 and over["blocks"][0]["private"] is True and over["blocks"][0]["room_words"] == 4
    assert result["checked"]["sound"] is True and result["checked"]["frames"] >= 3
    if not result["checked"].get("picture"):
        pytest.skip("no OCR on this machine: " + result["checked"].get("picture_note", ""))
    mail = kinds["email"]
    assert mail["start"] <= 10.0 and 20.0 <= mail["end"] <= 30.0 and mail["text"] == "m…@e….org"
    x, y, w, h = mail["region"]
    assert 0.05 < x < 0.4 and 0.1 < y < 0.4                                    # where the address is written
    assert any(s["title"] == "Datentransformation" for s in over["slides"])
    assert screening.run(folder, None)["checked"]["sound"] is False            # without the key: said, not hidden
