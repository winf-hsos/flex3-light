"""Step "publish", first part: the finished files leave the session folder for the folder from
which the lecturer uploads them, named the way Opencast uploads are named (decision of
2026-11-05):

  <root>\\<term>\\<module>\\<date> - <module> - <title>.mp4                the long version
  <root>\\<term>\\<module>\\<date> - <module> - <title>.<language>.vtt     its subtitles
  <root>\\<term>\\<module>\\<date> - <module> - <title> - <clip>.mp4       each clip, with its .vtt
  <root>\\<term>\\<module>\\<date> - <module> - <title> - Zusammenfassung.pdf

The files are moved, not copied. publish.json in the session folder records where each one
went, so that the window still plays them and the next publication of a new render replaces
them instead of leaving old ones behind.
"""
from __future__ import annotations

import datetime as dt
import re
import shutil
from pathlib import Path
from typing import Callable

from . import files

SUMMARY_NAME = "Zusammenfassung"
_FORBIDDEN = re.compile(r'[<>"|?*\x00-\x1f]')


def clean(text: str) -> str:
    """Text as part of a Windows file name: no reserved characters, no ' - ' inside, which separates
    the parts, and no dot or space at the end."""
    t = (text or "").replace(":", " –").replace("/", "-").replace("\\", "-")
    t = _FORBIDDEN.sub("", t).replace(" - ", " – ")
    return re.sub(r"\s+", " ", t).strip().rstrip(". ")


def _render_signature(folder: Path) -> str | None:
    return files.read_json(folder / "render.json").get("signature")


def stem(folder: Path) -> str:
    """'<date> - <module> - <title>' of a session folder."""
    meta = files.read_json(folder / "session.json")
    e = files.read_json(folder / "edit.json")
    date = (meta.get("recorded_at") or "")[:10] or folder.name[:10]
    module = meta.get("course_name") or folder.parent.name
    topic = folder.name.split(" - ", 1)[1] if " - " in folder.name else folder.name
    title = ((e.get("cards") or {}).get("title") or {}).get("title") or meta.get("topic") or topic
    return " - ".join(clean(x) for x in (date, module, title) if clean(x))


def target_dir(folder: Path, root: Path) -> Path:
    """<root>\\<term>\\<module>, with term and module from the archive's own layout."""
    meta = files.read_json(folder / "session.json")
    return Path(root) / folder.parent.parent.name / clean(meta.get("course_name") or folder.parent.name)


def plan(folder: Path, root: Path) -> list[tuple[str, Path]]:
    """(file in the session folder, where it goes) for everything the last render made, plus the
    summary if it belongs to that render. Files already moved are planned from their old place."""
    folder = Path(folder)
    made = files.read_json(folder / "render.json")
    if not made:
        return []
    out_dir, base = target_dir(folder, root), stem(folder)
    moves: list[tuple[str, Path]] = []
    languages = []
    for name in made.get("files", []):
        m = re.fullmatch(r"edited(\.([a-z]{2})\.vtt|\.mp4)", name)
        if m:
            moves.append((name, out_dir / f"{base}{m.group(1)}"))
            if m.group(2):
                languages.append(m.group(2))
    used: set[str] = set()
    for clip in files.read_json(folder / "clips.json").get("clips", []):
        title = clean(clip.get("title", "")) or Path(clip["file"]).stem
        name, k = title, 2
        while name.lower() in used:                        # two clips with the same title
            name, k = f"{title} ({k})", k + 1
        used.add(name.lower())
        moves.append((clip["file"], out_dir / f"{base} - {name}.mp4"))
        for lang in languages:
            vtt = f"{Path(clip['file']).stem}.{lang}.vtt"
            if locate(folder, vtt):
                moves.append((vtt, out_dir / f"{base} - {name}.{lang}.vtt"))
    s = files.read_json(folder / "summary.json")
    if s.get("signature") and s.get("signature") == made.get("signature") and locate(folder, "summary.pdf"):
        moves.append(("summary.pdf", out_dir / f"{base} - {SUMMARY_NAME}.pdf"))
    return moves


def published(folder: Path) -> dict:
    return files.read_json(Path(folder) / "publish.json")


def locate(folder: Path, name: str) -> Path | None:
    """Where a file of the session folder is now: still there, or where publishing moved it."""
    here = Path(folder) / name
    if here.exists():
        return here
    gone = published(folder).get("files", {}).get(name)
    return Path(gone) if gone and Path(gone).exists() else None


def current(folder: Path) -> bool:
    """True if the video rendered last is published with everything that belongs to it (a summary
    finished after publishing makes it open again) and all files are where publish.json says."""
    p = published(folder)
    done = p.get("files", {})
    return bool(p) and p.get("signature") == _render_signature(Path(folder)) and \
        all(Path(t).exists() for t in done.values()) and \
        all(name in done for name, _ in plan(Path(folder), Path(p.get("folder") or ".")))


def run(folder: Path, root: Path, discard: Callable[[Path], None] | None = None) -> dict:
    """Move the files of the last render into the publish folder; returns publish.json.

    A file of an earlier publication that is no longer wanted (a clip that is gone) is given to
    discard (the app sends it to the recycle bin). A file at the target that FLEX3 Light did not
    put there is never overwritten."""
    folder = Path(folder)
    moves = plan(folder, root)
    if not any(name == "edited.mp4" for name, _ in moves):
        raise ValueError("Es gibt noch kein geschnittenes Video zum Veröffentlichen.")
    before = published(folder).get("files", {})
    ours = {str(Path(t)).lower() for t in before.values()}
    for name, target in moves:
        source = folder / name
        if not source.exists() and not locate(folder, name):
            raise FileNotFoundError(f"{name} fehlt im Ordner der Aufnahme.")
        if target.exists() and str(target).lower() not in ours and source.exists():
            raise FileExistsError(f"Im Ziel liegt schon eine andere Datei „{target.name}“. "
                                  "Benenne sie um oder lösche sie, dann veröffentliche erneut.")
    discard = discard or (lambda p: p.unlink(missing_ok=True))
    moved: dict[str, str] = {}

    def record() -> dict:
        keep = {k: v for k, v in before.items() if Path(v).exists() and v not in moved.values()}
        report = {"published": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                  "signature": _render_signature(folder), "folder": str(target_dir(folder, root)),
                  "files": {**keep, **moved}}
        files.write_json(folder / "publish.json", report)
        return report

    try:
        for name, target in moves:
            source = folder / name
            if source.exists():                       # new from the last render: replaces the old one
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    discard(target)
                files.retry(lambda: shutil.move(str(source), str(target)))
            else:                                     # moved before; follows a changed name
                where = locate(folder, name)
                if where != target:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    files.retry(lambda: shutil.move(str(where), str(target)))
            moved[name] = str(target)
    except Exception:
        record()                                      # the window still finds what moved so far
        raise
    for old in before.values():                       # files of an earlier render that are not wanted now
        if old not in moved.values() and Path(old).exists():
            discard(Path(old))
    before = {}
    return record()
