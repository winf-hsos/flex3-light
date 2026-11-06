"""Publishing, first part: the finished files move to the upload folder under Opencast names."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import files, publish, summary  # noqa: E402

BASE = "2026-11-04 - Digitalisierung und Programmierung - Algorithmen"


def session(tmp_path: Path, signature="r1", clips=("Einführung", "Sortieren"), with_summary=True) -> Path:
    folder = tmp_path / "archive" / "2026-27 - WS" / "Digitalisierung und Programmierung" / "2026-11-04 - Algorithmen"
    folder.mkdir(parents=True, exist_ok=True)
    files.write_json(folder / "session.json", {"recorded_at": "2026-11-04T10:15:00+01:00",
                                               "course_name": "Digitalisierung und Programmierung",
                                               "topic": "Algorithmen"})
    made = ["edited.mp4", "edited.de.vtt", "edited.en.vtt"]
    for name in made:
        (folder / name).write_text(f"{name} {signature}", encoding="utf-8")
    entries = []
    for k, title in enumerate(clips, 1):
        name = f"clip-{k:02d}.mp4"
        (folder / name).write_text(f"{name} {signature}", encoding="utf-8")
        (folder / f"clip-{k:02d}.de.vtt").write_text("WEBVTT", encoding="utf-8")
        entries.append({"file": name, "title": title})
    files.write_json(folder / "clips.json", {"clips": entries})
    files.write_json(folder / "render.json", {"signature": signature, "files": made + [c["file"] for c in entries]})
    if with_summary:
        (folder / "summary.pdf").write_text("pdf", encoding="utf-8")
        files.write_json(folder / "summary.json", {"signature": signature})
    return folder


def test_clean_keeps_the_parts_apart_and_windows_happy():
    assert publish.clean("Teil 1: Sortieren / Suchen?") == "Teil 1 – Sortieren – Suchen"
    assert publish.clean("Bäume - und Graphen. ") == "Bäume – und Graphen"
    assert publish.clean('a<b>"c|d*') == "abcd"


def test_names_follow_the_opencast_pattern(tmp_path):
    folder = session(tmp_path)
    root = tmp_path / "out"
    names = {n: t for n, t in publish.plan(folder, root)}
    target = root / "2026-27 - WS" / "Digitalisierung und Programmierung"
    assert names["edited.mp4"] == target / f"{BASE}.mp4"
    assert names["edited.en.vtt"] == target / f"{BASE}.en.vtt"
    assert names["clip-01.mp4"] == target / f"{BASE} - Einführung.mp4"
    assert names["clip-02.de.vtt"] == target / f"{BASE} - Sortieren.de.vtt"
    assert names["summary.pdf"] == target / f"{BASE} - Zusammenfassung.pdf"
    assert "clip-01.en.vtt" not in names                    # only subtitles that exist


def test_title_card_wins_and_equal_clip_titles_get_a_number(tmp_path):
    folder = session(tmp_path, clips=("Übung", "Übung"))
    files.write_json(folder / "edit.json", {"cards": {"title": {"title": "Suchen und Sortieren"}}})
    names = [t.name for _, t in publish.plan(folder, tmp_path / "out")]
    stem = "2026-11-04 - Digitalisierung und Programmierung - Suchen und Sortieren"
    assert f"{stem} - Übung.mp4" in names and f"{stem} - Übung (2).mp4" in names


def test_publishing_moves_and_the_window_still_finds_everything(tmp_path):
    folder = session(tmp_path)
    root = tmp_path / "out"
    report = publish.run(folder, root)
    target = root / "2026-27 - WS" / "Digitalisierung und Programmierung"
    assert not (folder / "edited.mp4").exists() and (target / f"{BASE}.mp4").exists()
    assert report["folder"] == str(target) and len(report["files"]) == 8
    assert publish.locate(folder, "clip-02.mp4") == target / f"{BASE} - Sortieren.mp4"
    assert publish.current(folder) and summary.current(folder)


def test_a_new_render_replaces_the_old_files_and_drops_lost_clips(tmp_path):
    folder = session(tmp_path)
    root = tmp_path / "out"
    publish.run(folder, root)
    session(tmp_path, signature="r2", clips=("Einführung",), with_summary=False)
    assert not publish.current(folder)
    gone = []
    publish.run(folder, root, discard=lambda p: (gone.append(p.name), p.unlink()))
    target = root / "2026-27 - WS" / "Digitalisierung und Programmierung"
    assert (target / f"{BASE}.mp4").read_text(encoding="utf-8") == "edited.mp4 r2"
    assert f"{BASE} - Sortieren.mp4" in gone and f"{BASE} - Zusammenfassung.pdf" in gone
    assert not (target / f"{BASE} - Sortieren.mp4").exists()
    assert publish.current(folder)


def test_a_summary_finished_later_reopens_publishing(tmp_path):
    folder = session(tmp_path, with_summary=False)
    publish.run(folder, tmp_path / "out")
    assert publish.current(folder)
    (folder / "summary.pdf").write_text("pdf", encoding="utf-8")
    files.write_json(folder / "summary.json", {"signature": "r1"})
    assert not publish.current(folder)
    publish.run(folder, tmp_path / "out")
    assert publish.current(folder) and not (folder / "summary.pdf").exists()


def test_a_foreign_file_at_the_target_is_never_overwritten(tmp_path):
    folder = session(tmp_path)
    target = tmp_path / "out" / "2026-27 - WS" / "Digitalisierung und Programmierung"
    target.mkdir(parents=True)
    (target / f"{BASE}.mp4").write_text("mine", encoding="utf-8")
    with pytest.raises(FileExistsError):
        publish.run(folder, tmp_path / "out")
    assert (target / f"{BASE}.mp4").read_text(encoding="utf-8") == "mine" and (folder / "edited.mp4").exists()
    assert not (folder / "publish.json").exists()


def test_a_missing_file_stops_before_anything_moves(tmp_path):
    folder = session(tmp_path)
    (folder / "clip-02.mp4").unlink()
    with pytest.raises(FileNotFoundError):
        publish.run(folder, tmp_path / "out")
    assert (folder / "edited.mp4").exists()


def test_rendering_again_into_the_folder_reopens_publishing(tmp_path):
    from pipeline import render
    folder = session(tmp_path)
    publish.run(folder, tmp_path / "out")
    target = tmp_path / "out" / "2026-27 - WS" / "Digitalisierung und Programmierung"
    (target / f"{BASE} - Sortieren.mp4").unlink()                    # deleted by hand
    assert render.missing(folder) == ["clip-02.mp4"] and not publish.current(folder)
    (folder / "clip-02.mp4").write_text("clip-02.mp4 r1", encoding="utf-8")   # what "Neu rendern" brings back
    (folder / "edited.mp4").write_text("edited.mp4 r1", encoding="utf-8")
    assert render.missing(folder) == [] and not publish.current(folder)
    publish.run(folder, tmp_path / "out")
    assert publish.current(folder) and not (folder / "edited.mp4").exists()
