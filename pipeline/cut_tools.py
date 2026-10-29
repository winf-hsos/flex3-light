"""The workbench of the cut agent: what it can perceive of a recording, and how it proposes.

Everything works on one session folder. Perceiving: the words with times, confidence
and speaker; a search in the transcript; a frame or a contact sheet of a track; text in a
frame with its exact position (Windows' own OCR, pipeline/ocr.py), and a region shown
enlarged, so that a blur or a zoom is placed on the pixel instead of by eye; scene and
slide changes; a loudness profile; listening to a span again (normalised and transcribed
with speaker labels, which brings out quiet voices from the room); the programs in front. Proposing: items of the edit list (edit.py), with every boundary of
a span snapped out of words, so that no word is clipped.

Pictures come back as JPEG bytes; the agent hands them to the model.
"""
from __future__ import annotations

import io
import json
import re
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import audio, binaries, cut_rules, edit, files, ocr

TRACKS = {"program": "recording.mp4", "screen": "screen.mp4", "camera": "camera.mp4"}
NO_WINDOW, BELOW_NORMAL_PRIORITY = 0x08000000, 0x00004000


def _clock(t: float) -> str:
    t = max(0.0, t)
    h, rest = divmod(int(t), 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60}:{rest % 60:02d}"


def _ffmpeg_bytes(*args: str) -> bytes:
    r = subprocess.run([binaries.ffmpeg(), "-hide_banner", "-nostats", "-v", "error", *args], stdin=subprocess.DEVNULL,
                       capture_output=True, creationflags=NO_WINDOW | BELOW_NORMAL_PRIORITY)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg: " + r.stderr.decode("utf-8", "replace")[-400:])
    return r.stdout


def _font(size: int):
    try:
        return ImageFont.truetype("segoeui.ttf", size)
    except OSError:
        return ImageFont.load_default(size=size)


class Workbench:
    def __init__(self, folder: Path, settings: dict | None = None, listener=None):
        """listener: a transcribe.AssemblyAI for listen(); without one, listening is not offered."""
        self.folder = Path(folder)
        self.meta = files.read_json(self.folder / "session.json")
        self.transcript = files.read_json(self.folder / "transcript.json")
        self.words = self.transcript.get("words", [])
        self.segments = self.transcript.get("segments", [])
        log = files.read_json(self.folder / "activity.json")
        self.activity, self.private = log.get("activity", []), log.get("private", [])
        self.tracks = {k: self.folder / v for k, v in TRACKS.items() if (self.folder / v).exists()}
        self.duration = float(self.transcript.get("duration") or self.meta.get("duration_sec") or 0)
        self.settings, self.listener = settings or {}, listener
        self.edit = edit.load(self.folder) or edit.new(self.duration)
        self._work = Path(tempfile.mkdtemp(prefix="flex3-cut-"))
        self._wav: Path | None = None

    def close(self) -> None:
        for f in self._work.glob("*"):
            f.unlink(missing_ok=True)
        self._work.rmdir()

    # ---------------------------------------------------------- overview for the first message
    def overview(self) -> dict:
        return {"module": self.meta.get("course_name", ""), "language": self.transcript.get("language", "de"),
                "recorded_at": self.meta.get("recorded_at", ""), "duration": _clock(self.duration),
                "duration_seconds": round(self.duration, 1), "tracks": sorted(self.tracks),
                "terms": self.transcript.get("terms", []), "words": len(self.words),
                "programs": cut_rules.program_summary(self.activity),
                "privacy_key_spans": [[round(a, 1), round(b, 1)] for a, b in self.private]}

    def sentences(self) -> str:
        return "\n".join(f"[{_clock(s['start'])} {s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in self.segments)

    # ---------------------------------------------------------- text and activity
    def get_words(self, start: float, end: float) -> list[dict]:
        return [{"t": round(w["start"], 2), "end": round(w["end"], 2), "word": w["word"],
                 **({"conf": w["confidence"]} if "confidence" in w else {}),
                 **({"speaker": w["speaker"]} if "speaker" in w else {})}
                for w in self.words if w["end"] >= start and w["start"] <= end][:1500]

    def search(self, query: str) -> list[dict]:
        q = query.lower()
        return [{"start": s["start"], "end": s["end"], "text": s["text"]}
                for s in self.segments if q in s["text"].lower()][:50]

    def get_activity(self, start: float, end: float) -> dict:
        return {"programs": [{"start": a, "end": b, "program": p, "title": t}
                             for a, b, p, t in self.activity if b >= start and a <= end][:200],
                "privacy_key_spans": [[a, b] for a, b in self.private if b >= start and a <= end]}

    # ---------------------------------------------------------- pictures
    def _track(self, track: str) -> Path:
        if track not in self.tracks:
            raise ValueError(f"track {track!r} not in this recording; there are {', '.join(sorted(self.tracks))}")
        return self.tracks[track]

    def frame(self, t: float, track: str = "program", width: int = 1280) -> bytes:
        t = min(max(0.0, t), max(0.0, self.duration - 0.1))
        return _ffmpeg_bytes("-ss", f"{t:.3f}", "-i", str(self._track(track)), "-frames:v", "1",
                             "-vf", f"scale={int(width)}:-2", "-f", "image2", "-c:v", "mjpeg", "-q:v", "4", "pipe:1")

    def contact_sheet(self, start: float, end: float, count: int = 12, track: str = "program",
                      columns: int = 4, width: int = 360) -> bytes:
        count = max(2, min(int(count), 30))
        step = (end - start) / count
        times = [start + step * (i + 0.5) for i in range(count)]
        tiles = [Image.open(io.BytesIO(self.frame(t, track, width))).convert("RGB") for t in times]
        w, h = tiles[0].size
        rows = -(-len(tiles) // columns)
        sheet = Image.new("RGB", (columns * w + (columns + 1) * 6, rows * (h + 22) + (rows + 1) * 6), (24, 24, 24))
        draw, font = ImageDraw.Draw(sheet), _font(15)
        for i, (t, tile) in enumerate(zip(times, tiles)):
            x, y = 6 + (i % columns) * (w + 6), 6 + (i // columns) * (h + 28)
            sheet.paste(tile, (x, y + 22))
            draw.text((x + 2, y + 2), f"{_clock(t)}  ({t:.1f} s)", fill=(255, 255, 255), font=font)
        out = io.BytesIO()
        sheet.save(out, "JPEG", quality=80)
        return out.getvalue()

    def _picture(self, t: float, track: str) -> Image.Image:
        """The frame at full size, at most ocr.MAX_SIDE wide."""
        img = Image.open(io.BytesIO(self.frame(t, track, 3840))).convert("RGB")
        if img.width > ocr.MAX_SIDE:
            img = img.resize((ocr.MAX_SIDE, round(img.height * ocr.MAX_SIDE / img.width)), Image.LANCZOS)
        return img

    def _ocr(self, img: Image.Image) -> dict:
        path = self._work / "ocr.png"
        img.save(path)
        return ocr.recognize(path, self.transcript.get("language") or "de")

    def locate_text(self, t: float, query: str, track: str = "program") -> dict:
        """Where a text is in the frame: regions as fractions [x, y, w, h]. Small text is searched again
        in four enlarged tiles when the whole picture finds nothing. An empty query lists all text."""
        img = self._picture(t, track)
        found = ocr.find(self._ocr(img), query)
        if not found and query.strip():
            W, H = img.size
            for tx, ty in ((0, 0), (1, 0), (0, 1), (1, 1)):           # 2 x 2 tiles overlapping by a tenth
                x0, y0 = int(tx * W * 0.45), int(ty * H * 0.45)
                tile = img.crop((x0, y0, x0 + int(W * 0.55), y0 + int(H * 0.55)))
                scale = min(ocr.MAX_SIDE / tile.width, 2.5)
                big = tile.resize((round(tile.width * scale), round(tile.height * scale)), Image.LANCZOS)
                for hit in ocr.find(self._ocr(big), query):
                    x, y, w, h = hit["region"]
                    hit["region"] = [round((x0 + x * tile.width) / W, 4), round((y0 + y * tile.height) / H, 4),
                                     round(w * tile.width / W, 4), round(h * tile.height / H, 4)]
                    if all(abs(hit["region"][0] - f["region"][0]) > 0.01 or abs(hit["region"][1] - f["region"][1]) > 0.01
                           for f in found):
                        found.append(hit)
        return {"t": t, "track": track, "found": found,
                "note": "regions are [x, y, w, h] as fractions of the frame; use them as params.region"}

    def check_region(self, t: float, region: list[float], track: str = "program") -> bytes:
        """The region enlarged with some surroundings and its outline drawn in, to verify a blur or zoom."""
        x, y, w, h = [float(v) for v in region]
        img = self._picture(t, track)
        W, H = img.size
        mx, my = max(0.03, w * 0.35), max(0.03, h * 0.6)           # context around the region
        box = (max(0, int((x - mx) * W)), max(0, int((y - my) * H)),
               min(W, int((x + w + mx) * W)), min(H, int((y + h + my) * H)))
        crop = img.crop(box)
        draw = ImageDraw.Draw(crop)
        draw.rectangle((x * W - box[0], y * H - box[1], (x + w) * W - box[0], (y + h) * H - box[1]),
                       outline=(255, 40, 40), width=max(2, crop.width // 300))
        if crop.width < 900:                                      # small regions are shown large enough to judge
            f = 900 / crop.width
            crop = crop.resize((900, round(crop.height * f)), Image.LANCZOS)
        out = io.BytesIO()
        crop.save(out, "JPEG", quality=88)
        return out.getvalue()

    def scene_changes(self, start: float, end: float, track: str = "program", threshold: float = 0.25) -> list[float]:
        """Times of clear picture changes (slides, program switches), at most a few per second."""
        path = self._track(track)
        r = subprocess.run([binaries.ffmpeg(), "-hide_banner", "-nostats", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
                            "-i", str(path), "-vf", f"fps=4,scale=320:-2,select='gt(scene,{threshold})',showinfo",
                            "-an", "-f", "null", "-"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", creationflags=NO_WINDOW | BELOW_NORMAL_PRIORITY)
        return [round(start + float(x), 2) for x in re.findall(r"pts_time:([\d.]+)", r.stderr)][:300]

    # ---------------------------------------------------------- sound
    def _audio(self) -> Path:
        if self._wav is None:
            self._wav = self._work / "audio.wav"
            source = self.tracks.get("program") or next(iter(self.tracks.values()))
            audio.extract(source, self._wav)
        return self._wav

    def loudness(self, start: float, end: float, step: float = 1.0) -> dict:
        """Loudness in dBFS per step, and where it is far below the lecturer's usual level."""
        with wave.open(str(self._audio())) as w:
            rate = w.getframerate()
            w.setpos(int(max(0.0, start) * rate))
            data = np.frombuffer(w.readframes(int((end - start) * rate)), dtype=np.int16).astype(np.float32) / 32768
        n = max(1, int(step * rate))
        chunks = [data[i:i + n] for i in range(0, len(data), n) if len(data[i:i + n])]
        db = [round(float(20 * np.log10(np.sqrt(np.mean(c ** 2)) + 1e-9)), 1) for c in chunks]
        speech = sorted(x for x in db if x > -45)
        level = speech[len(speech) // 2] if speech else None
        return {"step": step, "start": start, "db": db[:2000], "lecturer_level": level,
                "note": "Sprache etwa 15 bis 30 dB unter dem Pegel der Lehrperson deutet auf Stimmen aus dem Raum."}

    def listen(self, start: float, end: float) -> dict:
        """The span again, loudness-normalised, transcribed with speaker labels."""
        if self.listener is None:
            raise ValueError("listening needs the AssemblyAI key")
        if end - start > 600:
            raise ValueError("listen to at most 10 minutes at a time")
        mp3 = self._work / "listen.mp3"
        _ffmpeg_bytes("-y", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(self._audio()),
                      "-af", "dynaudnorm=f=150:g=15", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k", str(mp3))
        r = self.listener.transcribe_file(mp3, self.transcript.get("language") or "de", [], speakers=True)
        mp3.unlink(missing_ok=True)
        return {"words": [{"t": round(start + w["start"], 2), "word": w["word"], "speaker": w.get("speaker", "?"),
                           "conf": w.get("confidence")} for w in r["words"]][:1500]}

    # ---------------------------------------------------------- proposing
    def propose(self, kind: str, start: float, end: float | None, reason: str, confidence: float = 0.5,
                replaces: list[str] | None = None, category: str | None = None, state: str = "accepted",
                **params) -> dict:
        """A decided proposal of the agent (accepted, or rejected to show an alternative it weighed).
        replaces: ids of rule proposals this one stands in for; category: why something is removed."""
        if kind not in edit.SPANLESS:
            start, end = edit.snap(start, self.words), edit.snap(end, self.words)
        start = max(0.0, start)
        if end is not None:
            end = min(self.duration, end)
        if kind in edit.REMOVING | edit.MARKING and category is None:
            category = "pause" if kind == "pause" else "other"
        entry = edit.item(kind, start, end, reason, "agent", confidence, state, category, **params)
        known = [i for i in (replaces or []) if edit.find(self.edit, i)]
        if known:
            entry["replaces"] = known
        edit.add(self.edit, entry)
        self.save()
        return entry

    def assess(self, item_id: str, verdict: str, reason: str) -> dict:
        """The agent's decision on a rule's proposal: agree accepts it, disagree rejects it. The rule's
        own reason stays; the agent's stands beside it."""
        x = edit.find(self.edit, item_id)
        if x is None or x["source"] != "rule":
            raise ValueError(f"no rule proposal {item_id}; assess only rule proposals")
        if verdict not in edit.VERDICTS:
            raise ValueError("verdict is agree or disagree")
        x["assessment"] = {"verdict": verdict, "reason": reason.strip()}
        x["state"] = "accepted" if verdict == "agree" else "rejected"
        self.save()
        return x

    def change(self, item_id: str, **fields) -> dict:
        """Change one of the agent's own proposals; a rule's proposal is decided with assess(), not rewritten."""
        x = edit.find(self.edit, item_id)
        if x is None:
            raise ValueError(f"no item {item_id}")
        if x["source"] != "agent":
            raise ValueError("only your own proposals can be changed; give your view of a rule with assess_rule")
        for k in ("start", "end"):
            if fields.get(k) is not None and x["kind"] not in edit.SPANLESS:
                fields[k] = edit.snap(float(fields[k]), self.words)
        if fields.get("state") in edit.STATES:
            x["state"] = fields["state"]
        if fields.get("category") in edit.CATEGORIES:
            x["category"] = fields["category"]
        for k in ("start", "end", "reason", "confidence"):
            if fields.get(k) is not None:
                x[k] = fields[k]
        x["params"].update(fields.get("params") or {})
        self.save()
        return x

    def withdraw(self, item_id: str) -> None:
        x = edit.find(self.edit, item_id)
        if x is None or x["source"] == "lecturer":
            raise ValueError(f"no item {item_id} that the agent may withdraw")
        self.edit["items"].remove(x)
        self.save()

    def set_metadata(self, title: str | None = None, description: str | None = None, notes: str | None = None) -> None:
        for k, v in (("title", title), ("description", description), ("notes", notes)):
            if v is not None:
                self.edit[k] = v.strip()
        self.save()

    def set_summary(self, cuts: list[dict], check: list[dict] | None = None, notes: list[str] | None = None) -> None:
        self.edit["summary"] = {
            "cuts": [{"category": c["category"], "text": c.get("text", "").strip()} for c in cuts
                     if c.get("category") in edit.CATEGORIES],
            "check": [{"id": c["id"], "why": c.get("why", "").strip()} for c in (check or [])
                      if edit.find(self.edit, c.get("id", ""))][:5],
            "notes": [n.strip() for n in (notes or []) if n.strip()][:3]}
        self.save()

    def undecided(self) -> list[dict]:
        return [x for x in self.edit["items"] if x["state"] == "proposed" and x["source"] != "lecturer"]

    def decide_rest(self) -> int:
        """What the agent left open is decided here: its own by confidence, a rule's accepted, as the rules
        come from the lecturer's settings. Marked, so the approval can show it."""
        rest = self.undecided()
        for x in rest:
            x["state"] = "accepted" if x["source"] == "rule" or x["confidence"] >= 0.5 else "rejected"
            x["decided_by"] = "fallback"
        if rest:
            self.save()
        return len(rest)

    def check(self) -> list[str]:
        open_items = self.undecided()
        return edit.problems(self.edit) + ([f"Noch nicht entschieden: {', '.join(x['id'] for x in open_items)}"]
                                           if open_items else [])

    def save(self) -> None:
        edit.save(self.folder, self.edit)

    def add_rules(self) -> list[dict]:
        """The rule proposals, once per edit list."""
        if any(x["source"] == "rule" for x in self.edit["items"]):
            return [x for x in self.edit["items"] if x["source"] == "rule"]
        found = cut_rules.propose(self.words, self.duration, self.activity, self.settings)
        for x in found:
            edit.add(self.edit, x)
        self.save()
        return found


def summary_line(entry: dict) -> str:
    span = _clock(entry["start"]) + ("" if entry["end"] is None else "–" + _clock(entry["end"]))
    view = entry.get("assessment")
    return (f"{entry['id']} {entry['source']} {entry['kind']} {span} [{entry['state']}] {entry['reason']}"
            + (f" (your view: {view['verdict']}, {view['reason']})" if view else ""))


def as_json(value) -> str:
    return json.dumps(value, ensure_ascii=False)
