"""Subtitles in more languages, captions burned into the picture, the name on the title card and the look of
the videos kept from the window (2026-11-13)."""
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import binaries, cut_rules, edit, languages, llm, publish, render  # noqa: E402
from pipeline import style as styles  # noqa: E402
from test_products import _folder  # noqa: E402


class _Translator:
    """Answers every request of languages.translate() with "EN <text>" per cue."""
    model = "fake"

    def __init__(self):
        self.calls = 0

    def complete(self, instructions, messages, tools):
        self.calls += 1
        body = messages[0].parts[0].text
        given = json.loads(body[body.index("Cues:\n") + 6:].split("\n\nSaid right after")[0])
        text = json.dumps({"cues": [{"n": c["n"], "text": "EN " + c["text"]} for c in given]})
        return llm.Reply(text, [], llm.Usage(100, 0, 20, 0))


def _frame(video: Path, t: float) -> bytes:
    """The bottom fifth of one frame as raw pixels, small."""
    return subprocess.run([binaries.ffmpeg(), "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                           "-vf", "crop=iw:ih/5:0:ih*4/5,scale=64:8", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                          capture_output=True, stdin=subprocess.DEVNULL, creationflags=binaries.flags()).stdout


def _streams(video: Path) -> str:
    return subprocess.run([binaries.ffmpeg(), "-hide_banner", "-i", str(video)], capture_output=True, stdin=subprocess.DEVNULL, text=True,
                          encoding="utf-8", errors="replace", creationflags=binaries.flags()).stderr


def test_the_settings_carry_the_name_and_the_languages():
    prefs = cut_rules.cut_preferences({}, {"lecturer_name": "  Prof. Dr.   Muster ", "subtitle_languages": ["en", "xx", "en"],
                                           "burned_captions": 1})
    assert prefs["cut.lecturer_name"] == "Prof. Dr. Muster"
    assert prefs["cut.subtitle_languages"] == ["en"] and prefs["cut.burned_captions"] is True
    s = cut_rules.settings_from({})
    assert s["lecturer_name"] == "" and s["subtitle_languages"] == [] and s["burned_captions"] is False


def test_the_title_card_carries_the_name_from_the_settings():
    meta = {"instructor_name": "nimeseth", "recorded_at": "2026-11-12T10:00:00+01:00", "course_name": "Modul"}
    e = edit.new(60.0)
    assert edit.default_cards(e, meta)["title"]["footer"] == "12. November 2026   nimeseth"
    named = {"lecturer_name": "Prof. Dr. Muster"}
    assert edit.default_cards(e, meta, named)["title"]["footer"] == "12. November 2026   Prof. Dr. Muster"
    old = edit.default_cards(e, meta)
    assert edit.renamed(old, meta, named)["title"]["footer"] == "12. November 2026   Prof. Dr. Muster"
    assert edit.renamed(old, meta, {}) is None                           # no name set: nothing changes
    changed = {**old, "title": {**old["title"], "footer": "Gastvortrag"}}
    assert edit.renamed(changed, meta, named) is None                    # the lecturer's own footer stays


def test_the_look_of_the_videos_is_saved_from_the_window_without_touching_the_fingerprint(tmp_path):
    folder = tmp_path / "style"
    before = styles.fingerprint(styles.load(folder))
    s = styles.save(folder, {"captions": {"highlight": "#ffcc00", "size": 5}})
    assert s["captions"] == {"highlight": "#ffcc00", "size": 5.0, "box": 0.6} and not s["problems"]
    assert styles.fingerprint(s) == before                               # captions alone render nothing anew
    s = styles.save(folder, {"name": "Hochschule", "colors": {"background": "#123456"}})
    assert s["name"] == "Hochschule" and s["colors"]["background"] == "#123456" and styles.fingerprint(s) != before
    for wrong in ({"colors": {"background": "blau"}}, {"captions": {"size": 20}}, {"colors": {"logo": "#000000"}}):
        try:
            styles.save(folder, wrong)
            raise AssertionError(wrong)
        except ValueError:
            pass
    png = bytes.fromhex("89504e470d0a1a0a") + b"rest"
    s = styles.set_logo(folder, png, "png")
    assert s["logo"] == "logo.png" and (folder / "logo.png").read_bytes() == png
    try:
        styles.set_logo(folder, b"GIF89a", "png")
        raise AssertionError("not a PNG")
    except ValueError:
        pass
    s = styles.set_logo(folder, None)
    assert s["logo"] == "" and not (folder / "logo.png").exists() and s["name"] == "Hochschule"


def test_captions_highlight_the_word_being_spoken():
    style = styles.load(None)
    timed = [{"word": "Eins", "start": 1.0, "end": 1.4, "i": 0}, {"word": "zwei", "start": 1.5, "end": 1.9, "i": 1},
             {"word": "drei.", "start": 2.0, "end": 2.6, "i": 2}]
    text = languages.ass(timed, None, 1920, 1080, style, "Segoe UI Semibold")
    events = [x for x in text.splitlines() if x.startswith("Dialogue:")]
    assert len(events) == 3
    assert events[1].startswith("Dialogue: 0,0:00:01.50,0:00:02.00,Caption")
    assert "Eins {\\1c&HE39E00&}zwei{\\1c&HFFFFFF&} drei." in events[1]   # the style's blue, as BGR
    assert "PlayResY: 1080" in text and ",Segoe UI Semibold,49," in text
    translated = languages.ass(None, [(1.0, 2.6, "One two three.")], 1920, 1080, style, "Segoe UI Semibold")
    assert translated.count("Dialogue:") == 1 and "\\1c" not in translated.split("[Events]")[1]


def test_a_translated_cue_appears_where_its_words_remain():
    timed = [{"word": f"w{i}", "start": float(i), "end": i + 0.5, "i": i} for i in (0, 1, 2, 5)]
    translated = [{"from": 0, "to": 2, "words": 3, "text": "erste"}, {"from": 3, "to": 6, "words": 4, "text": "zweite"},
                  {"from": 5, "to": 5, "words": 1, "text": "dritte"}]
    assert languages.placed(timed, translated) == [(0.0, 2.5, "erste"), (5.0, 5.5, "dritte")]   # 1 of 4 words is too few


def test_the_languages_of_a_recording_from_the_settings_to_the_package(tmp_path):
    folder = _folder(tmp_path)
    render.run(folder)
    assert languages.load(folder) == {} and not languages.due(folder)
    settings = {"subtitle_languages": ["en"], "burned_captions": True}
    data = languages.fixed(folder, settings)
    assert data["defaults"] == {"languages": ["en"], "burned": ["de"]}
    assert languages.fixed(folder, {})["defaults"] == data["defaults"]  # fixed once, settings do not reach back
    assert languages.due(folder, styles.load(None))
    model = _Translator()
    languages.run(folder, None, settings, client=model)
    assert sorted(x["file"] for x in languages.finished(folder)) == ["edited.burned.de.mp4", "edited.en.vtt"]
    assert not languages.due(folder, styles.load(None)) and model.calls == 1
    tr = languages.translation(folder, "en")
    assert tr["cues"][0]["text"].startswith("EN Wort1") and tr["spoken"] == "de"
    vtt = (folder / "edited.en.vtt").read_text(encoding="utf-8")
    assert vtt.startswith("WEBVTT\n\n00:00:05.100 --> 00:00:11.600\nEN Wort1 ")    # after the title card of 4 s
    # the word in the picture: the bottom of a frame differs from the video without captions
    assert _frame(folder / "edited.burned.de.mp4", 8.0) != _frame(folder / "edited.mp4", 8.0)
    # another choice: captions in English instead, the German version goes, the translation stays
    assert languages.set_choices(folder, {"long": {"languages": ["en"], "burned": ["en"]}}) == ""
    assert languages.set_choices(folder, {"t9": {"languages": []}}) != ""
    languages.run(folder, None, settings, client=model)
    assert sorted(x["file"] for x in languages.finished(folder)) == ["edited.burned.en.mp4", "edited.en.vtt"]
    assert not (folder / "edited.burned.de.mp4").exists() and model.calls == 1
    view = languages.view(folder)
    assert view["videos"][0]["burned"] == ["en"] and all(f["ready"] for f in view["videos"][0]["files"])
    # the package: subtitles as files and inside the video, the captioned version as a video of its own
    names = dict(publish.package_names(folder))
    assert names["edited.en.vtt"].endswith("Signale.en.vtt")
    assert names["edited.burned.en.mp4"].endswith("Signale (Untertitel EN).mp4")
    publish.make_package(folder, tmp_path / "p.zip")
    with zipfile.ZipFile(tmp_path / "p.zip") as z:
        z.extract(names["edited.mp4"], tmp_path / "out")
    streams = _streams(tmp_path / "out" / names["edited.mp4"])
    assert len(re.findall(r"Subtitle: mov_text", streams)) == 2 and "(eng)" in streams and "(ger)" in streams
