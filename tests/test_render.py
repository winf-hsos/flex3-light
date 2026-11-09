"""Rendering an approved edit list: the timeline, and one real ffmpeg pass on a short test video."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import edit, render, timeline  # noqa: E402


def _edit(duration: float = 60.0) -> dict:
    e = edit.new(duration)
    for x in [edit.item("cut", 0, 3.01, "Stille", "rule", 0.9, "accepted", "dead_time"),
              edit.item("pause", 20, 32, "Übung", "rule", 0.7, "accepted", "pause", mode="card"),
              edit.item("pause", 40, 46, "Warten", "rule", 0.7, "accepted", "pause", mode="shorten"),
              edit.item("cut", 50, 52, "verworfen", "agent", 0.3, "rejected", "other"),
              edit.item("mute", 10, 12, "Stimme aus dem Raum", "agent", 0.7, "accepted", "privacy"),
              edit.item("blur", 5, 9, "Mail", "agent", 0.8, "accepted", "privacy", region=[0.1, 0.1, 0.3, 0.1]),
              edit.item("zoom", 13, 16, "klein", "agent", 0.8, "accepted", region=[0.5, 0.5, 0.2, 0.2]),
              edit.item("freeze", 16, 18, "Fenster", "agent", 0.8, "accepted", "privacy"),
              edit.item("layout", 33, 38, "Kamera", "agent", 0.8, "accepted", camera="big"),
              edit.item("chapter", 0, None, "", "agent", 0.9, "accepted", title="Anfang"),
              edit.item("chapter", 25, None, "", "agent", 0.9, "accepted", title="Nach der Pause"),
              edit.item("chapter", 47, None, "", "agent", 0.9, "accepted", title="Schluss"),
              edit.item("clip", 33, 48, "", "agent", 0.9, "accepted", title="Ein Stück"),
              edit.item("thumbnail", 14, None, "", "agent", 0.9, "accepted", track="program")]:
        edit.add(e, x)
    return e


def test_plan_maps_times_and_chapters():
    p = timeline.plan(_edit(), fps=30)
    # cut 0-3, the pause keeps a 2.5 s card (20-22.5), the shortened pause keeps a second at either end
    assert p["removed"] == [(0.0, 3.0), (22.5, 32.0), (41.0, 45.0)]
    assert p["cards"] == [(20.0, 22.5, "Pause entfernt")]
    assert p["duration"] == 60 - 3 - 9.5 - 4
    assert timeline.map_time(2.0, p) is None and timeline.map_time(3.0, p) == 0.0
    assert timeline.map_time(33.0, p) == 33 - 3 - 9.5
    assert timeline.map_forward(25.0, p) == 22.5 - 3                        # inside the pause: where it goes on
    chs = timeline.chapters(_edit(), p)
    assert [c["title"] for c in chs] == ["Anfang", "Nach der Pause", "Schluss"] and chs[0]["start"] == 0.0
    assert timeline.youtube_chapters(chs).splitlines()[1] == "0:19 Nach der Pause"


def _video(path: Path, source: str, seconds: int = 60) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"{source}=s=640x360:r=30:d={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-shortest", "-c:v", "libx264",
                    "-preset", "ultrafast", "-c:a", "aac", "-ar", "48000", str(path)],
                   stdin=subprocess.DEVNULL, capture_output=True, check=True)


def _duration(path: Path, stream: str) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries", "stream=duration",
                        "-of", "csv=p=0", str(path)], stdin=subprocess.DEVNULL, capture_output=True, text=True)
    return float(r.stdout.strip())


def test_render_writes_the_long_version_clips_and_chapters(tmp_path):
    _video(tmp_path / "recording.mp4", "testsrc2")
    _video(tmp_path / "camera.mp4", "smptebars")
    words = [{"word": f"Wort{i}.", "start": i * 1.0 + 0.1, "end": i * 1.0 + 0.6} for i in range(60)]
    (tmp_path / "transcript.json").write_text(json.dumps({"language": "de", "words": words, "duration": 60}),
                                              encoding="utf-8")
    (tmp_path / "session.json").write_text(json.dumps({"session_id": "x", "language": "de"}), encoding="utf-8")
    e = _edit()
    e["approved"] = "2026-10-29T10:00:00+01:00"
    edit.save(tmp_path, e)
    messages = []
    report = render.run(tmp_path, progress=messages.append)
    expected = 60 - 3 - 9.5 - 4
    assert abs(_duration(tmp_path / "edited.mp4", "v:0") - expected) < 0.1
    assert abs(_duration(tmp_path / "edited.mp4", "a:0") - expected) < 0.1           # picture and sound together
    assert report["clips"] == 1 and (tmp_path / "clip-01.mp4").exists() and (tmp_path / "clip-01.de.vtt").exists()
    assert (tmp_path / "thumbnail.jpg").exists() and report["warnings"] == []
    vtt = (tmp_path / "edited.de.vtt").read_text(encoding="utf-8")
    assert "Wort1." not in vtt and "Wort3." in vtt and "Wort25." not in vtt          # cut words are gone
    chapters = json.loads((tmp_path / "chapters.json").read_text(encoding="utf-8"))
    assert [c["title"] for c in chapters["chapters"]] == ["Anfang", "Nach der Pause", "Schluss"]
    assert report["signature"] == render.signature(e) and any("%" in m for m in messages)


def test_runner_renders_after_the_approval_and_again_after_changes(tmp_path, monkeypatch):
    import library
    import pipeline_runner
    folder = tmp_path / "2026-10-29 - Test"
    folder.mkdir()
    (folder / "session.json").write_text(json.dumps({"session_id": "x"}), encoding="utf-8")
    (folder / "pipeline.json").write_text(json.dumps({"steps": {"transcribe": {"state": "done"}, "subtitles": {
        "state": "done"}, "cut": {"state": "done"}}}), encoding="utf-8")
    e = _edit()
    edit.save(folder, e)
    calls = []

    def fake(f, video_args, progress, sound_dir=None):
        calls.append(video_args)
        progress("Langfassung wird gerendert, 50 %")
        report = {"signature": render.signature(edit.load(f)), "files": ["edited.mp4"]}
        (f / "edited.mp4").write_bytes(b"video")
        (f / "render.json").write_text(json.dumps(report), encoding="utf-8")
        return report
    summaries = []

    def fake_summary(f, client, progress=None):
        summaries.append(f)
        (f / "summary.pdf").write_bytes(b"%PDF")
        report = {"created": "2026-10-29T11:00:00+01:00", "model": "m",
                  "signature": json.loads((f / "render.json").read_text(encoding="utf-8"))["signature"]}
        (f / "summary.json").write_text(json.dumps(report), encoding="utf-8")
        return report
    monkeypatch.setattr(pipeline_runner.render, "run", fake)
    monkeypatch.setattr(pipeline_runner.summary, "run", fake_summary)
    monkeypatch.setattr(pipeline_runner.services, "key", lambda service: "test-key")
    r = pipeline_runner.PipelineRunner(tmp_path / "q.json", lambda: True, lambda m: [], lambda f: {"auto": True},
                                       ["-c:v", "h264_nvenc"])
    r.enqueue(folder)
    r._process(folder)
    assert calls == [] and library.stations(folder)["cut"] == "review"      # not approved: nothing rendered
    assert library.stations(folder)["content"] == "soon"
    e["approved"] = "2026-10-29T10:00:00+01:00"
    edit.save(folder, e)
    assert library.stations(folder)["cut"] == "queued"
    assert r._process(folder) and calls == [["-c:v", "h264_nvenc"]]
    assert library.stations(folder)["cut"] == "done" and r.describe(folder) == "Geschnittenes Video fertig"
    assert library.stations(folder)["content"] == "queued"                  # the summary follows the render
    r._process(folder)
    assert len(calls) == 1 and len(summaries) == 1                          # the same list is not rendered twice
    assert library.stations(folder)["content"] == "done"
    r._process(folder)
    assert len(summaries) == 1 and not r.queued(folder)                    # nor summarised twice
    assert r.redo_summary(folder) and r._process(folder) and len(summaries) == 2
    (folder / "edited.mp4").unlink()                                        # deleted by hand
    assert library.stations(folder)["cut"] == "missing" and library.cut_view(folder)["render"]["missing"] == ["edited.mp4"]
    assert r.describe(folder) == "Dateien des geschnittenen Videos fehlen"
    r.enqueue(folder)
    r._process(folder)
    assert len(calls) == 1                                                  # never rendered again by itself
    assert r.redo_render(folder) and r._process(folder) and len(calls) == 2
    assert library.stations(folder)["cut"] == "done" and library.stations(folder)["content"] == "done"
    r._process(folder)
    assert len(calls) == 2 and len(summaries) == 2                          # same list: no new summary either
    e["items"][0]["end"] = 4.0
    edit.save(folder, e)
    assert library.stations(folder)["cut"] == "queued" and r._process(folder) and len(calls) == 3
    assert library.stations(folder)["content"] == "queued"                  # a new video needs a new summary


def test_cards_add_time_and_carry_sound(tmp_path):
    from pipeline import sounds
    _video(tmp_path / "recording.mp4", "testsrc2", 30)
    words = [{"word": f"Wort{i}.", "start": i * 1.0 + 0.1, "end": i * 1.0 + 0.6} for i in range(30)]
    (tmp_path / "transcript.json").write_text(json.dumps({"language": "de", "words": words, "duration": 30}),
                                              encoding="utf-8")
    (tmp_path / "session.json").write_text(json.dumps({"session_id": "x", "language": "de", "course_name": "Modul",
                                                       "recorded_at": "2026-10-29T10:00:00+01:00"}), encoding="utf-8")
    e = edit.new(30.0)
    for x in [edit.item("cut", 0, 2.0, "Stille", "rule", 0.9, "accepted", "dead_time"),
              edit.item("chapter", 0, None, "", "agent", 0.9, "accepted", title="Anfang"),
              edit.item("chapter", 12, None, "", "agent", 0.9, "accepted", title="Mitte"),
              edit.item("lower_third", 20, 24, "", "agent", 0.8, "accepted", text="Prof. Dr. Muster", kicker="Gast")]:
        edit.add(e, x)
    e["title"] = "Ein Test"
    e["cards"] = edit.default_cards(e, json.loads((tmp_path / "session.json").read_text(encoding="utf-8")))
    assert e["cards"]["title"]["footer"] == "29. Oktober 2026" and e["cards"]["chapters"] == "card"
    e["approved"] = "2026-10-29T10:00:00+01:00"
    edit.save(tmp_path, e)
    p = timeline.plan(e, 30)
    ins = timeline.inserts(e, p)
    assert [o["kind"] for o in ins] == ["title", "chapter", "closing"]           # no chapter card right after the title
    assert timeline.shift(0.0, ins) == 4.0 and timeline.shift(10.0, ins) == 16.0  # 10 s of the cut lies after both cards
    library = tmp_path / "sounds"
    library.mkdir()
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
                    str(sounds.path(library, "swoosh"))], stdin=subprocess.DEVNULL, capture_output=True, check=True)
    report = render.run(tmp_path, sound_dir=library)
    expected = 28 + 4 + 2 + 4
    assert abs(_duration(tmp_path / "edited.mp4", "v:0") - expected) < 0.15
    assert abs(_duration(tmp_path / "edited.mp4", "a:0") - expected) < 0.15
    assert any("intro" in w and "outro" in w for w in report["warnings"])       # no sounds for those: silent cards
    chapters = json.loads((tmp_path / "chapters.json").read_text(encoding="utf-8"))["chapters"]
    assert [c["start"] for c in chapters] == [0.0, 14.0]                        # the second starts with its card
    assert "00:00:04" in (tmp_path / "edited.de.vtt").read_text(encoding="utf-8")   # the words start after the title


def _gray(path: Path, t: float) -> bytes:
    """One frame at t as 640x360 gray bytes."""
    return subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                           "-f", "rawvideo", "-pix_fmt", "gray", "-"], stdin=subprocess.DEVNULL,
                          capture_output=True, check=True).stdout


def _diff(a: bytes, b: bytes, box: tuple[int, int, int, int]) -> float:
    x, y, w, h = box
    total = sum(abs(a[r * 640 + c] - b[r * 640 + c]) for r in range(y, y + h) for c in range(x, x + w))
    return total / (w * h)


def test_late_regions_are_read_for_their_span_and_keep_the_layout_around_them(tmp_path):
    # A region late in the lecture used to be a split-off branch of the main picture, which overlay
    # buffered from the start: a pixelated box at 2:42 h filled the memory (2026-11-04).
    _video(tmp_path / "recording.mp4", "testsrc2")
    _video(tmp_path / "screen.mp4", "smptebars")
    e = edit.new(60.0)
    for x in [edit.item("layout", 0, 60, "Kamera schwarz", "agent", 0.8, "accepted", camera="hidden"),
              edit.item("blur", 50, 52, "Mail", "agent", 0.8, "accepted", "privacy", region=[0.0, 0.0, 0.5, 0.5]),
              edit.item("zoom", 51, 55, "klein", "agent", 0.8, "accepted", region=[0.0, 0.0, 0.5, 0.5])]:
        edit.add(e, x)
    _, graph, _, warnings = render.build(tmp_path, e, tmp_path)
    assert "split" not in graph and "trim=" not in graph and warnings == []
    assert render._without(0, 60, [(50, 52)]) == [(0, 50), (52, 60)]
    e["approved"] = "2026-11-04T10:00:00+01:00"
    edit.save(tmp_path, e)
    report = render.run(tmp_path)
    assert report["warnings"] == [] and abs(_duration(tmp_path / "edited.mp4", "v:0") - 60) < 0.1
    out, rec, screen = tmp_path / "edited.mp4", tmp_path / "recording.mp4", tmp_path / "screen.mp4"
    whole, box, rest = (0, 0, 640, 360), (16, 16, 288, 148), (336, 196, 288, 148)
    assert _diff(_gray(out, 30), _gray(screen, 30), whole) < 8               # the screen alone outside the box's time
    assert _diff(_gray(out, 50.5), _gray(rec, 50.5), rest) < 8               # the program picture while it is pixelated
    assert _diff(_gray(out, 50.5), _gray(rec, 50.5), box) > 8                # and the box is pixelated


def test_sound_on_one_channel_is_put_on_both(tmp_path):
    # OBS records a stereo microphone input that only uses its first channel as left only.
    src = tmp_path / "left.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-filter_complex",
                    "[0:a][1:a]amerge=inputs=2,atrim=0:2[a]", "-map", "[a]", "-ar", "48000", str(src)],
                   stdin=subprocess.DEVNULL, capture_output=True, check=True)
    levels = render.channel_levels(src)
    assert levels[0] > -30 and levels[1] < -100
    assert render.both_ears(levels) == "pan=stereo|c0=c0|c1=c0,"
    assert render.both_ears([-30.0, -33.0]) == "" and render.both_ears([-30.0]) == ""
    p = {"source": str(src), "silent": [], "kept": [(0.0, 2.0)]}
    out = render.sound(p, tmp_path)
    left, right = render.channel_levels(out)
    assert abs(left - right) < 0.5
