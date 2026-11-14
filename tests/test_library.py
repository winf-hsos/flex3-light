"""The archive as the page "Meine Aufnahmen" sees it: session folders, stations, media."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import library  # noqa: E402


def _session(archive: Path, sid: str, name: str, steps: dict | None = None) -> Path:
    folder = archive / "2026-27 - WS" / "Digitalisierung und Programmierung" / name
    folder.mkdir(parents=True)
    (folder / "session.json").write_text(json.dumps({"session_id": sid, "course_name": "Digitalisierung und Programmierung",
                                                     "recorded_at": "2026-10-26T10:15:00", "duration_sec": 90,
                                                     "tracks": [{"kind": "camera"}]}), encoding="utf-8")
    if steps is not None:
        (folder / "pipeline.json").write_text(json.dumps({"steps": steps}), encoding="utf-8")
    return folder


def test_scan_summary_and_stations(tmp_path):
    a = _session(tmp_path, "s1", "2026-10-26 - Mikrocontroller und LiFi")
    b = _session(tmp_path, "s2", "2026-10-27 - Sensoren", {"transcribe": {"state": "waiting"}})
    c = _session(tmp_path, "s3", "2026-10-28 - Licht", {"transcribe": {"state": "done"}, "subtitles": {"state": "done"}})
    (tmp_path / "2026-27 - WS" / "Lose Datei.mp4").write_bytes(b"")        # older loose recordings are ignored
    assert library.scan(tmp_path) == {"s1": a, "s2": b, "s3": c}
    s = library.summary(a)
    assert s["title"] == "Mikrocontroller und LiFi" and s["camera"] and s["term"] == "2026-27 - WS"
    assert s["stations"] == {"recorded": "done", "transcript": "queued", "cut": "soon", "content": "soon", "publish": "soon",
                             "outline": "soon", "edit": "soon", "longversion": "soon", "topics": "soon", "languages": "soon"}
    assert set(library.STEPS) <= set(s["stations"])
    assert library.stations(b)["transcript"] == "waiting"
    assert library.stations(c)["transcript"] == "done"
    assert library.stations(a, queued=False)["transcript"] == "open"           # only filed: started by hand
    assert library.stations(a, queued=True)["transcript"] == "queued"


def test_detail_lists_only_media(tmp_path):
    folder = _session(tmp_path, "s1", "2026-10-26 - Thema", {"transcribe": {"state": "done"}, "subtitles": {"state": "running"}})
    for name in ("recording.mp4", "screen.mp4", "subtitles.de.vtt", "activity.json", "transcript.raw.json"):
        (folder / name).write_bytes(b"")
    (folder / "transcript.json").write_text(json.dumps({"segments": [{"start": 0.0, "end": 2.0, "text": "Hallo."}]}),
                                            encoding="utf-8")
    d = library.detail(folder)
    assert d["media"] == ["recording.mp4", "screen.mp4", "subtitles.de.vtt"]   # never activity.json
    assert d["segments"] == [{"start": 0.0, "end": 2.0, "text": "Hallo."}]
    assert d["stations"]["transcript"] == "running"


def test_delete_moves_the_folder_away_and_leaves_the_queue(tmp_path):
    from shelf import shelf
    folder = _session(tmp_path, "s1", "2026-10-26 - Thema", {"transcribe": {"state": "done"}})
    c = shelf(tmp_path, library.scan(tmp_path))           # deleting happens on the server since phase 5
    c.pipeline.enqueue(folder)
    c.pipeline.active = str(folder)
    assert "transkribiert" in c.delete_recording("s1") and folder.exists()   # not while it is worked on
    c.pipeline.active = None
    assert c.delete_recording("s1") == "" and not folder.exists()
    assert [f.name.startswith(folder.name) for f in (c.data_dir / "trash").iterdir()] == [True]   # into the trash
    assert c.pipeline.queue == [] and "s1" not in c._library
    assert c.delete_recording("s1") == "Die Aufnahme ist nicht mehr in der Ablage."


def test_the_eight_steps_follow_the_cut_the_render_and_the_topic_videos(tmp_path):
    from test_products import _folder
    from pipeline import edit, products, render
    folder = _folder(tmp_path, inputs=[])
    (folder / "pipeline.json").write_text(json.dumps({"steps": {s: {"state": "done"} for s in ("transcribe", "subtitles", "cut")}}),
                                          encoding="utf-8")
    steps = lambda: {k: library.stations(folder)[k] for k in ("outline", "edit", "longversion", "topics")}   # noqa: E731
    assert steps() == {"outline": "done", "edit": "done", "longversion": "queued", "topics": "soon"}
    e = edit.load(folder)
    e.pop("approved")
    edit.save(folder, e)
    assert steps() == {"outline": "done", "edit": "review", "longversion": "soon", "topics": "soon"}
    edit.approve(folder)
    render.run(folder)
    assert steps()["longversion"] == "done" and steps()["topics"] == "open"
    products.propose(folder, {})
    assert steps()["topics"] == "review"
    (folder / "outline.json").unlink()
    assert steps()["outline"] == "open"                    # cut before the outline existed: made anew later


def test_the_outline_is_open_together_with_the_cut_it_starts(tmp_path):
    folder = tmp_path / "2026-11-11_quarto_1000"
    folder.mkdir()
    (folder / "session.json").write_text(json.dumps({"session_id": folder.name}), encoding="utf-8")
    (folder / "pipeline.json").write_text(json.dumps({"steps": {s: {"state": "done"} for s in ("transcribe", "subtitles")}}),
                                          encoding="utf-8")
    s = library.stations(folder)
    assert (s["transcript"], s["outline"], s["edit"]) == ("done", "open", "open")
