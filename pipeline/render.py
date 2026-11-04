"""Step "render": the approved edit list becomes videos, in one ffmpeg pass for the long version.

Into the session folder (names proposed for the FLEX3 contract, docs/pipeline.md):
  edited.mp4                the long version
  edited.<language>.vtt     its subtitles, from the word times moved along with the cut
  chapters.json             chapters in the edited video, with the lines for a YouTube description
  clip-01.mp4 ...           the accepted clips, with clip-01.<language>.vtt, listed in clips.json
  thumbnail.jpg             the accepted thumbnail, 1280 x 720
  render.json               what was rendered from which list, and what could not be done

Cards that add time (title, chapters, closing; timeline.inserts()) come in a second pass:
the cut video is read piece by piece between them and joined with the cards and their
sounds (sounds.py). Lower thirds, also for chapters if the lecturer prefers them to cards,
are laid over the picture in the first pass.

The pass works on the program picture (recording.mp4) in the times of the recording: held
pictures, pixelated regions, zooms, the camera big (camera.mp4) or the screen alone
(screen.mp4) and cards over removed pauses are laid over it, and only then are the removed
spans taken out of the picture with select, which keeps memory flat however long the
lecture is. Every overlay only costs where it applies: other tracks are read just for
their span (-ss/-t), still pictures last as long as their span, and the graphics card
decodes where it can (binaries.hwaccel()). Regions to pixelate or zoom are read the same
way, as a second reading of the recording for their span with the held pictures (and, for a
zoom, the pixelation) laid over it again. They must not be a split-off branch of the main
picture: overlay waits for that branch's first frame and keeps every frame before it, so a
region late in a long lecture filled the memory (2026-11-04, a region at 2:42 h). The sound is cut to the sample
outside ffmpeg: its aselect lets every packet through in version 8.0.1 (found on
2026-10-29), so the sound with its silenced spans is written once as WAV and the kept
spans are copied from it. Frame-aligned bounds (timeline.py) keep picture and sound together.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Callable

from . import binaries, cards, edit, files, sounds, timeline

NO_WINDOW, BELOW_NORMAL_PRIORITY = 0x08000000, 0x00004000
X264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
EPS = 0.0005                     # half a millisecond, so that a frame exactly on a bound is decided as intended


def signature(e: dict) -> str:
    """Changes whenever what would be rendered changes."""
    accepted = sorted((x for x in e["items"] if x["state"] == "accepted"), key=lambda x: (x["start"], x["id"]))
    keep = [{k: x.get(k) for k in ("kind", "start", "end", "params")} for x in accepted]
    return hashlib.sha1(json.dumps([e["duration"], keep, e.get("cards")], sort_keys=True).encode()).hexdigest()[:16]


def probe(video: Path) -> dict:
    return binaries.video_info(video)


def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def _box(region, W: int, H: int) -> tuple[int, int, int, int]:
    """[x, y, w, h] as fractions -> even pixel box inside the frame."""
    x, y, w, h = [float(v) for v in region]
    px, py = _even(max(0.0, x) * W), _even(max(0.0, y) * H)
    pw, ph = min(_even(w * W), W - px), min(_even(h * H), H - py)
    return px, py, max(2, pw), max(2, ph)


def _zoom_box(region, W: int, H: int) -> tuple[int, int, int, int]:
    """The region widened to the frame's aspect ratio, so that it can be scaled to the full frame."""
    x, y, w, h = _box(region, W, H)
    if w / h < W / H:
        w = _even(h * W / H)
    else:
        h = _even(w * H / W)
    w, h = min(w, W), min(h, H)
    cx, cy = x + _box(region, W, H)[2] / 2, y + _box(region, W, H)[3] / 2
    return _even(min(max(0, cx - w / 2), W - w)), _even(min(max(0, cy - h / 2), H - h)), w, h


def _without(a: float, b: float, spans) -> list[tuple[float, float]]:
    """The parts of a..b outside the given spans, leaving out slivers shorter than a tenth of a second."""
    pieces, pos = [], a
    for sa, sb in sorted(spans):
        if sb <= pos or sa >= b:
            continue
        if sa > pos:
            pieces.append((pos, sa))
        pos = max(pos, sb)
    if pos < b:
        pieces.append((pos, b))
    return [(x, y) for x, y in pieces if y - x >= 0.1]


def _between(spans) -> str:
    return "+".join(f"between(t,{a:.4f},{b:.4f})" for a, b in spans) or "0"


def build(folder: Path, e: dict, work: Path) -> tuple[list[str], str, dict, list[str]]:
    """(input arguments, filter graph, plan, warnings) for the long version."""
    folder = Path(folder)
    source = folder / "recording.mp4"
    if not source.exists():
        source = folder / "screen.mp4"
    info = probe(source)
    W, H, fps = info["width"], info["height"], info["fps"]
    p = timeline.plan(e, fps)
    accepted = [x for x in e["items"] if x["state"] == "accepted" and x["end"] is not None]
    inputs, graph, warnings = [*binaries.hwaccel(), "-i", str(source)], [], []
    n = 1
    cur = "v0"
    graph.append(f"[0:v]setpts=PTS-STARTPTS,format=yuv420p[{cur}]")

    def laid(base: str, src_label: str, x: int, y: int, a: float, b: float) -> str:
        """base with a picture over it between a and b; base goes on alone after the picture ends."""
        nxt = f"v{len(graph)}"
        graph.append(f"[{base}][{src_label}]overlay={x}:{y}:eof_action=pass:enable='between(t,{a:.4f},{b:.4f})'[{nxt}]")
        return nxt

    def overlay(src_label: str, x: int, y: int, a: float, b: float) -> None:
        nonlocal cur
        cur = laid(cur, src_label, x, y, a, b)

    def span_input(args: list[str], a: float, b: float, label: str, chain: str) -> None:
        """An input read only from a to b, its picture placed at a on the recording's clock."""
        nonlocal n
        at = args.index("-i")                         # -t before -i limits the input, after it the whole output
        inputs.extend([*args[:at], "-t", f"{b - a:.4f}", *args[at:]])
        graph.append(f"[{n}:v]setpts=PTS-STARTPTS+{a:.4f}/TB,{chain}[{label}]")
        n += 1

    def still_input(path: Path, a: float, b: float, label: str, chain: str = "format=yuv420p") -> None:
        span_input(["-loop", "1", "-framerate", str(fps), "-i", str(path)], a, b, label, chain)

    def unique(prefix: str) -> str:
        return f"{prefix}{len(graph)}_{n}"

    stills: list[tuple[Path, float, float]] = []       # held pictures, also laid over the regions' readings
    regions: list[tuple[tuple[int, int, int, int], float, float]] = []   # pixelated boxes

    def pixelated(box: tuple[int, int, int, int], a: float, b: float) -> str:
        """The box from a to b, pixelated, from its own reading of the recording."""
        bx, by, bw, bh = box
        src = program(a, b, with_regions=False)
        label = unique("b")
        graph.append(f"[{src}]crop={bw}:{bh}:{bx}:{by},scale={max(2, bw // 24)}:{max(2, bh // 24)},"
                     f"scale={bw}:{bh}:flags=neighbor[{label}]")
        return label

    def program(a: float, b: float, with_regions: bool) -> str:
        """The program picture from a to b as the steps before have made it: a reading of the recording
        for this span only, with the held pictures and, if asked, the pixelated boxes laid over it."""
        label = unique("r")
        span_input([*binaries.hwaccel(), "-ss", f"{a:.4f}", "-i", str(source)], a, b, label, "format=yuv420p")
        for still, fa, fb in stills:
            if fa < b and a < fb:
                lo, hi = max(a, fa), min(b, fb)
                held = unique("h")
                still_input(still, lo, hi, held, f"scale={W}:{H},format=yuv420p")
                label = laid(label, held, 0, 0, lo, hi)
        if with_regions:
            for box, ra, rb in regions:
                if ra < b and a < rb:
                    lo, hi = max(a, ra), min(b, rb)
                    label = laid(label, pixelated(box, lo, hi), box[0], box[1], lo, hi)
        return label

    # 1. held pictures: the frame just before the span stays on screen
    for i, x in enumerate(x for x in accepted if x["kind"] == "freeze"):
        still = work / f"still-{i}.png"
        r = subprocess.run([binaries.ffmpeg(), "-y", "-v", "error", "-ss", f"{max(0.0, x['start'] - 0.05):.3f}", "-i", str(source),
                            "-frames:v", "1", str(still)], stdin=subprocess.DEVNULL, capture_output=True,
                           creationflags=NO_WINDOW)
        if r.returncode == 0 and still.exists():
            still_input(still, x["start"], x["end"], f"f{i}", f"scale={W}:{H},format=yuv420p")
            overlay(f"f{i}", 0, 0, x["start"], x["end"])
            stills.append((still, x["start"], x["end"]))
    # 2. pixelated regions
    for x in accepted:
        if x["kind"] != "blur":
            continue
        if not isinstance(x["params"].get("region"), list):
            warnings.append(f"{x['id']}: Verpixeln ohne Bereich ausgelassen")
            continue
        box = _box(x["params"]["region"], W, H)
        overlay(pixelated(box, x["start"], x["end"]), box[0], box[1], x["start"], x["end"])
        regions.append((box, x["start"], x["end"]))
    # 3. zooms into the screen
    for x in accepted:
        if x["kind"] != "zoom" or not isinstance(x["params"].get("region"), list):
            continue
        zx, zy, zw, zh = _zoom_box(x["params"]["region"], W, H)
        src, label = program(x["start"], x["end"], with_regions=True), unique("z")
        graph.append(f"[{src}]crop={zw}:{zh}:{zx}:{zy},scale={W}:{H}:flags=lanczos[{label}]")
        overlay(label, 0, 0, x["start"], x["end"])
    # 4. layout: the camera big, or the screen alone (not while something is pixelated: the region
    #    was found on the program picture and would not cover the same content on the screen track,
    #    so the program picture stays for those seconds and the screen alone goes on after them)
    blurs = [(a, b) for _, a, b in regions]
    for x in accepted:
        if x["kind"] != "layout":
            continue
        mode = x["params"].get("camera", "small")
        track = {"big": "camera.mp4", "hidden": "screen.mp4"}.get(mode)
        if track is None:
            continue
        if not (folder / track).exists():
            warnings.append(f"{x['id']}: Layout „{mode}“ braucht {track}, die es nicht gibt")
            continue
        spans = _without(x["start"], x["end"], blurs) if mode == "hidden" else [(x["start"], x["end"])]
        for a, b in spans:
            label = unique("l")
            span_input([*binaries.hwaccel(), "-ss", f"{a:.4f}", "-i", str(folder / track)], a, b, label,
                       f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,format=yuv420p")
            overlay(label, 0, 0, a, b)
    # 5. cards over removed pauses
    for i, (a, b, text) in enumerate(p["cards"]):
        card = cards.card(work / f"card-{i}.png", W, H, text)
        still_input(card, a, b, f"c{i}")
        overlay(f"c{i}", 0, 0, a, b)
    # 6. lower thirds: proposed ones, and chapter titles when chapters are shown that way
    thirds = [(x["start"], x["end"], x["params"].get("text", ""), x["params"].get("kicker", ""))
              for x in accepted if x["kind"] == "lower_third" and x["params"].get("text")]
    if (e.get("cards") or {}).get("chapters") == "lower_third":
        chapters = sorted((x for x in e["items"] if x["kind"] == "chapter" and x["state"] == "accepted"),
                          key=lambda x: x["start"])
        for k, x in enumerate(chapters, 1):
            if x["params"].get("title"):
                thirds.append((x["start"], min(x["start"] + 5.0, e["duration"]), x["params"]["title"], f"Kapitel {k}"))
    for i, (a, b, text, kicker) in enumerate(thirds):
        png = cards.lower_third(work / f"third-{i}.png", W, H, text, kicker)
        still_input(png, a, b, f"t{i}", "format=rgba")
        overlay(f"t{i}", 0, 0, a, b)
    # 7. the picture loses the removed spans; the sound is cut separately (sound())
    keep = "not(" + "+".join(f"gte(t,{a - EPS:.4f})*lt(t,{b - EPS:.4f})" for a, b in p["removed"]) + ")" \
        if p["removed"] else "1"
    graph.append(f"[{cur}]select='{keep}',setpts=N/({fps}*TB)[vout]")
    silent = [(x["start"], x["end"]) for x in accepted if x["kind"] == "mute"] + [(a, b) for a, b, _ in p["cards"]]
    return inputs, ";\n".join(graph), {**p, "width": W, "height": H, "fps": fps, "silent": silent,
                                       "source": str(source)}, warnings


RATE = 48000


def sound(p: dict, work: Path) -> Path:
    """The sound of the edited video as WAV: silenced spans are silent, removed spans are gone, to the sample."""
    full, cut = work / "sound-full.wav", work / "sound.wav"
    silence = f",volume=0:enable='{_between(p['silent'])}'" if p["silent"] else ""
    r = subprocess.run([binaries.ffmpeg(), "-y", "-v", "error", "-i", p["source"], "-vn",
                        "-af", f"aformat=sample_rates={RATE}:channel_layouts=stereo{silence}",
                        "-c:a", "pcm_s16le", str(full)], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                       creationflags=NO_WINDOW | BELOW_NORMAL_PRIORITY)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg: " + r.stderr[-400:])
    with wave.open(str(full)) as src, wave.open(str(cut), "wb") as dst:
        dst.setparams(src.getparams())
        frames, size = src.getnframes(), src.getsampwidth() * src.getnchannels()
        for a, b in p["kept"]:
            first, last = int(round(a * RATE)), min(frames, int(round(b * RATE)))
            if first >= last:
                continue
            src.setpos(first)
            left = last - first
            while left > 0:
                chunk = src.readframes(min(RATE * 10, left))
                if not chunk:
                    break
                dst.writeframes(chunk)
                left -= len(chunk) // size
    full.unlink(missing_ok=True)
    return cut


def _run(args: list[str], total: float, progress: Callable[[float], None] | None) -> None:
    """ffmpeg with its progress on stdout; progress(share) between 0 and 1."""
    proc = subprocess.Popen([binaries.ffmpeg(), "-hide_banner", "-v", "error", "-nostats", "-progress", "pipe:1",
                             "-y", *args],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", creationflags=NO_WINDOW | BELOW_NORMAL_PRIORITY)
    errors: list[str] = []

    import threading
    reader = threading.Thread(target=lambda: errors.extend(proc.stderr), daemon=True)
    reader.start()
    for line in proc.stdout:
        m = re.match(r"out_time_us=(\d+)", line)
        if m and progress and total > 0:
            progress(min(1.0, int(m.group(1)) / 1e6 / total))
    proc.wait()
    reader.join(timeout=5)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg: " + "".join(errors)[:600])         # the first errors name the cause


def compose(cut: Path, ins: list[dict], p: dict, out: Path, work: Path, sound_dir: Path | None,
            video_args: list[str], say: Callable[[str], None], warnings: list[str]) -> float:
    """The cut video with the cards between its pieces; returns the final length."""
    W, H, fps = p["width"], p["height"], p["fps"]
    inputs, chains, labels = [], [], []
    n, pos, missing = 0, 0.0, set()
    pieces = []                                  # ("video", a, b) and ("card", insert)
    for o in ins:
        if o["at"] > pos + 0.01:
            pieces.append(("video", pos, o["at"]))
        pieces.append(("card", o))
        pos = max(pos, o["at"])
    if p["duration"] > pos + 0.01:
        pieces.append(("video", pos, p["duration"]))
    for k, piece in enumerate(pieces):
        if piece[0] == "video":
            _, a, b = piece
            inputs += ["-ss", f"{a:.4f}", "-t", f"{b - a:.4f}", "-i", str(cut)]
            chains.append(f"[{n}:v]setpts=PTS-STARTPTS,format=yuv420p,setsar=1[v{k}]")
            chains.append(f"[{n}:a]aformat=sample_rates=48000:channel_layouts=stereo,asetpts=PTS-STARTPTS[a{k}]")
            n += 1
        else:
            o = piece[1]
            png = cards.card(work / f"insert-{k}.png", W, H, o["title"], o["kicker"], o["footer"])
            inputs += ["-loop", "1", "-framerate", str(fps), "-t", f"{o['seconds']:.3f}", "-i", str(png)]
            chains.append(f"[{n}:v]format=yuv420p,setsar=1,fade=t=in:d=0.25,fade=t=out:st={o['seconds'] - 0.25:.3f}:d=0.25[v{k}]")
            n += 1
            clip = sounds.path(sound_dir, o["sound"]) if sound_dir and o["sound"] else None
            if clip is not None and clip.exists():
                inputs += ["-i", str(clip)]
                chains.append(f"[{n}:a]aformat=sample_rates=48000:channel_layouts=stereo,volume=0.7,"
                              f"apad=whole_dur={o['seconds']:.3f},atrim=0:{o['seconds']:.3f},"
                              f"afade=t=out:st={max(0.0, o['seconds'] - 0.4):.3f}:d=0.4,asetpts=PTS-STARTPTS[a{k}]")
            else:
                if o["sound"]:
                    missing.add(o["sound"])
                inputs += ["-f", "lavfi", "-t", f"{o['seconds']:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
                chains.append(f"[{n}:a]asetpts=PTS-STARTPTS[a{k}]")
            n += 1
        labels.append(f"[v{k}][a{k}]")
    if missing:
        warnings.append("Ohne Klang gerendert (" + ", ".join(sorted(missing)) + "): die Klänge entstehen mit dem "
                        "ElevenLabs-Schlüssel aus den Einstellungen")
    graph = ";\n".join(chains) + ";\n" + "".join(labels) + f"concat=n={len(pieces)}:v=1:a=1[vout][aout]"
    script = work / "compose.txt"
    script.write_text(graph, encoding="utf-8")
    total = p["duration"] + sum(o["seconds"] for o in ins)
    _run([*inputs, "-/filter_complex", str(script), "-map", "[vout]", "-map", "[aout]", "-r", str(fps),
          *video_args, "-c:a", "aac", "-b:a", "160k", "-f", "mp4", str(out)], total,
         lambda share: say(f"Tafeln werden eingefügt, {share * 100:.0f} %"))
    return round(total, 3)


def run(folder: Path, video_args: list[str] | None = None,
        progress: Callable[[str], None] | None = None, sound_dir: Path | None = None) -> dict:
    """Render the approved edit list of a session folder; returns render.json."""
    folder = Path(folder)
    e = edit.load(folder)
    if not e or not e.get("approved"):
        raise ValueError("Der Schnitt ist nicht freigegeben.")
    say = progress or (lambda text: None)
    video_args = video_args or X264
    transcript = files.read_json(folder / "transcript.json")
    language = transcript.get("language") or files.read_json(folder / "session.json").get("language") or "de"
    work = Path(tempfile.mkdtemp(prefix="flex3-render-"))
    try:
        inputs, graph, p, warnings = build(folder, e, work)
        script = work / "graph.txt"
        script.write_text(graph, encoding="utf-8")
        long_tmp = folder / "edited.mp4.part"
        say("Ton wird geschnitten …")
        cut_sound = sound(p, work)
        sound_input = inputs.count("-i")                    # its index among the inputs
        ins = timeline.inserts(e, p)
        cut_file = work / "cut.mp4" if ins else long_tmp
        say("Langfassung wird gerendert …")
        _run([*inputs, "-i", str(cut_sound), "-/filter_complex", str(script), "-map", "[vout]",
              "-map", f"{sound_input}:a", "-shortest", "-r", str(p["fps"]),
              *video_args, "-c:a", "aac", "-b:a", "160k", "-f", "mp4", str(cut_file)], p["duration"],
             lambda share: say(f"Langfassung wird gerendert, {share * 100:.0f} %"))
        final_duration = p["duration"]
        if ins:
            final_duration = compose(cut_file, ins, p, long_tmp, work, sound_dir, video_args, say, warnings)
        os.replace(long_tmp, folder / "edited.mp4")
        written = ["edited.mp4"]
        if transcript:
            (folder / f"edited.{language}.vtt").write_text(timeline.vtt(timeline.words(transcript, p, ins=ins)),
                                                           encoding="utf-8")
            written.append(f"edited.{language}.vtt")
        chs = timeline.chapters(e, p, ins)
        files.write_json(folder / "chapters.json", {"chapters": chs, "youtube": timeline.youtube_chapters(chs)})
        written.append("chapters.json")
        # clips, cut from the long version, so that they carry everything done to it
        for old in folder.glob("clip-*.*"):
            old.unlink(missing_ok=True)
        clips = []
        accepted_clips = sorted((x for x in e["items"] if x["kind"] == "clip" and x["state"] == "accepted"),
                                key=lambda x: x["start"])
        for i, x in enumerate(accepted_clips, 1):
            a = timeline.shift(timeline.map_forward(x["start"], p), ins)
            b = timeline.shift(timeline.map_forward(x["end"], p), ins)
            if b - a < 3:
                warnings.append(f"{x['id']}: Clip nach dem Schnitt kürzer als drei Sekunden, ausgelassen")
                continue
            name = f"clip-{i:02d}"
            say(f"Teilvideo {i} von {len(accepted_clips)} wird gerendert …")
            _run(["-ss", f"{a:.3f}", "-i", str(folder / "edited.mp4"), "-t", f"{b - a:.3f}", *video_args,
                  "-c:a", "aac", "-b:a", "160k", str(folder / f"{name}.mp4")], b - a, None)
            if transcript:
                (folder / f"{name}.{language}.vtt").write_text(
                    timeline.vtt(timeline.words(transcript, p, a, b, ins)), encoding="utf-8")
            clips.append({"file": f"{name}.mp4", "title": x["params"].get("title", ""),
                          "summary": x["params"].get("summary", ""), "start": a, "end": b,
                          "source_start": x["start"], "source_end": x["end"]})
            written.append(f"{name}.mp4")
        files.write_json(folder / "clips.json", {"clips": clips})
        thumb = next((x for x in e["items"] if x["kind"] == "thumbnail" and x["state"] == "accepted"), None)
        if thumb:
            t = timeline.shift(timeline.map_forward(thumb["start"], p), ins)
            subprocess.run([binaries.ffmpeg(), "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(folder / "edited.mp4"),
                            "-frames:v", "1", "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,"
                            "pad=1280:720:(ow-iw)/2:(oh-ih)/2", "-q:v", "3", str(folder / "thumbnail.jpg")],
                           stdin=subprocess.DEVNULL, capture_output=True, creationflags=NO_WINDOW)
            if (folder / "thumbnail.jpg").exists():
                written.append("thumbnail.jpg")
        report = {"rendered": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                  "signature": signature(e), "approved": e.get("approved"), "duration": final_duration,
                  "cards": [{"kind": o["kind"], "title": o["title"]} for o in ins],
                  "source_duration": e["duration"], "files": written, "clips": len(clips),
                  "chapters": len(chs), "warnings": warnings, "encoder": video_args[1]}
        files.write_json(folder / "render.json", report)
        return report
    finally:
        shutil.rmtree(work, ignore_errors=True)
        (folder / "edited.mp4.part").unlink(missing_ok=True)
