"""The style of all videos in one place (pipeline/style.py): read and checked, on every card, in the render,
and a change of it renders again what is approved and not published."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT))
from flex3_light_server.profiles import Store  # noqa: E402
from flex3_light_server.worker import Worker  # noqa: E402
from pipeline import cards, edit, render, timeline  # noqa: E402
from pipeline import style as styles  # noqa: E402
from test_render import _duration, _video  # noqa: E402


def _style(folder: Path, body: dict) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "style.json").write_text(json.dumps(body), encoding="utf-8")
    return folder


def test_a_style_falls_back_where_it_is_wrong_and_names_why(tmp_path):
    assert styles.load(None)["colors"]["background"] == "#009ee3" and styles.load(None)["problems"] == []
    (tmp_path / "logo.png").write_bytes(b"x")
    s = styles.load(_style(tmp_path, {"colors": {"background": "blau", "text": "#000000"},
                                      "cards": {"chapter": 30, "fade": "kurz"}, "logo": "../logo.png",
                                      "intro": "logo.png", "farbe": 1}))
    assert s["colors"] == {"background": "#009ee3", "accent": "#006e9f", "text": "#000000"}
    assert s["cards"]["chapter"] == 10.0 and s["cards"]["fade"] == 0.25
    assert s["logo"] == "" and s["intro"] == "logo.png"                      # a file outside the folder is refused
    assert len(s["problems"]) == 5
    before = styles.fingerprint(s)
    os.utime(tmp_path / "logo.png", ns=(1, 1))                                # the same style, another intro file
    assert styles.fingerprint(styles.load(tmp_path)) != before
    assert styles.fingerprint(styles.load(None)) == styles.fingerprint(styles.load(tmp_path / "empty"))


def test_cards_take_colors_and_logo_of_the_style(tmp_path):
    from PIL import Image
    Image.new("RGBA", (40, 40), (255, 0, 0, 255)).save(tmp_path / "logo.png")
    s = styles.load(_style(tmp_path, {"colors": {"background": "#102030", "accent": "#00ff00"}, "logo": "logo.png"}))
    with Image.open(cards.card(tmp_path / "c.png", 640, 360, "Titel", style=s)) as img:
        assert img.getpixel((5, 5)) == (16, 32, 48)
        assert img.getpixel((640 - 64 - 10, int(360 * 0.855) + 10)) == (255, 0, 0)    # the logo at the bottom right
    with Image.open(cards.lower_third(tmp_path / "t.png", 640, 360, "Text", style=s)) as img:
        assert img.getpixel((int(640 * 0.05) + 1, int(360 * 0.86) - 5))[:3] == (0, 255, 0)


def test_inserts_follow_the_style(tmp_path):
    e = edit.new(30.0)
    edit.add(e, edit.item("chapter", 10, None, "", "agent", 0.9, "accepted", title="Mitte"))
    e["cards"] = edit.default_cards(e, {})
    p = timeline.plan(e, 30)
    s = {**styles.load(_style(tmp_path, {"cards": {"title": 3, "chapter": 1.5}})), "intro_seconds": 2.0,
         "outro_seconds": 1.0}
    ins = timeline.inserts(e, p, s)
    assert [(o["kind"], o["seconds"]) for o in ins] == [("intro", 2.0), ("title", 3.0), ("chapter", 1.5),
                                                       ("closing", 4.0), ("outro", 1.0)]
    assert [o["sound"] for o in ins] == [None, "title", "chapter", "closing", None]
    assert timeline.shift(10.0, ins) == 16.5
    assert [o["seconds"] for o in timeline.inserts(e, p)] == [4.0, 2.0, 4.0]   # without a style: as before


def test_the_sound_fades_at_every_cut_but_not_at_the_ends(tmp_path):
    src = tmp_path / "tone.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=50:duration=3",
                    "-ar", "48000", "-ac", "2", str(src)], stdin=subprocess.DEVNULL, capture_output=True, check=True)
    p = {"source": str(src), "silent": [], "kept": [(0.0, 1.0), (2.0, 3.0)]}

    def samples(path):
        with wave.open(str(path)) as w:
            return np.abs(np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).reshape(-1, 2)[:, 0])

    hard, soft = samples(render.sound(p, tmp_path, 0)), samples(render.sound(p, tmp_path, 10))
    assert len(hard) == len(soft) == 96000
    at = 48000                                                   # the junction
    assert soft[at - 5:at + 5].max() < 0.05 * hard[at - 2000:at + 2000].max()
    assert soft[:100].max() == hard[:100].max() and soft[-100:].max() == hard[-100:].max()
    assert (soft[at - 2000:at - 480] == hard[at - 2000:at - 480]).all()           # untouched outside 10 ms


def test_render_in_a_style_with_intro_outro_and_own_sound(tmp_path):
    folder = tmp_path / "session"
    folder.mkdir()
    _video(folder / "recording.mp4", "testsrc2", 20)
    look = tmp_path / "style"
    look.mkdir()
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "smptebars=s=320x240:r=25:d=2",
                    "-c:v", "libx264", "-preset", "ultrafast", str(look / "intro.mp4")],
                   stdin=subprocess.DEVNULL, capture_output=True, check=True)            # no sound of its own
    _video(look / "outro.mp4", "testsrc", 1)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=880:duration=0.5",
                    str(look / "ping.wav")], stdin=subprocess.DEVNULL, capture_output=True, check=True)
    _style(look, {"name": "Test", "cards": {"title": 3, "chapter": 1.5, "closing": 2},
                  "sounds": {"title": "", "chapter": "ping", "closing": ""}, "intro": "intro.mp4", "outro": "outro.mp4"})
    (folder / "session.json").write_text(json.dumps({"session_id": "x", "language": "de"}), encoding="utf-8")
    e = edit.new(20.0)
    edit.add(e, edit.item("chapter", 0, None, "", "agent", 0.9, "accepted", title="Anfang"))
    edit.add(e, edit.item("chapter", 8, None, "", "agent", 0.9, "accepted", title="Mitte"))
    e["cards"] = edit.default_cards(e, {})
    e["approved"] = "2026-11-12T10:00:00+01:00"
    edit.save(folder, e)
    report = render.run(folder, style_dir=look)
    expected = 2 + 3 + 20 + 1.5 + 2 + 1
    assert abs(report["duration"] - expected) < 0.05 and report["warnings"] == []
    assert abs(_duration(folder / "edited.mp4", "v:0") - expected) < 0.15
    assert abs(_duration(folder / "edited.mp4", "a:0") - expected) < 0.15
    assert report["style"] == styles.fingerprint(styles.load(look)) and report["style_name"] == "Test"
    chapters = json.loads((folder / "chapters.json").read_text(encoding="utf-8"))["chapters"]
    assert chapters[-1]["start"] == 2 + 3 + 8                                  # its card, after intro and title


def _approved(data: Path, sid: str, made_style: str | None, published: bool = False) -> Path:
    folder = data / "sessions" / sid
    folder.mkdir(parents=True)
    (folder / "session.json").write_text(json.dumps({"session_id": sid}), encoding="utf-8")
    e = edit.new(10.0)
    e["approved"] = "2026-11-12T10:00:00+01:00"
    edit.save(folder, e)
    (folder / "render.json").write_text(json.dumps({"signature": render.signature(e), "style": made_style}),
                                        encoding="utf-8")
    if published:
        (folder / "publish.json").write_text(json.dumps({"signature": render.signature(e)}), encoding="utf-8")
    return folder


def test_a_change_of_style_renders_again_what_is_not_published(tmp_path):
    data = tmp_path / "data"
    worker = Worker(data, Store(data / "profiles"))
    old = styles.fingerprint(styles.load(None))
    _approved(data, "a", old)
    _approved(data, "b", None)                                  # rendered before there was a style
    _approved(data, "c", old, published=True)
    assert worker.restyle() == []                                # the first look only notes the style
    assert worker.restyle() == []                                # nothing changed
    _style(data / "style", {"name": "Neu", "colors": {"background": "#222222"}})
    assert worker.restyle() == ["a", "b"]                        # the published one keeps its style
    assert all(f in worker.runner.queue for f in (str(data / "sessions" / "a"), str(data / "sessions" / "b")))
    assert json.loads((data / "sessions" / "a" / "pipeline.json").read_text(encoding="utf-8"))["render_requested"]
    assert worker.restyle() == []
