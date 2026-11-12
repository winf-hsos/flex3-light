"""Stages A and B of the cut: screening the recording and the safety net for privacy, without a model.

Stage A writes overview.json: the recording in blocks of a minute, each with what was said,
how loud, how long without words, how often the picture changed, the slide titles at each
change, the programs in front and whether the privacy key was pressed. Later stages read
the lecture from it instead of from the whole transcript (plan of 2026-11-10).

Stage B writes privacy.json, the locks and candidates of locks.py, from fixed rules that do
not depend on any model:
- the picture: every GRID seconds and at every clear picture change, the frame of the
  program track (what the video shows) is read by the OCR built into Windows and searched
  for mail addresses, keys and passwords, student numbers, lists of numbers and of names;
- the sound: the whole recording again, loudness-normalised and with speaker labels
  (AssemblyAI), so that quiet voices from the room come out; spans of another speaker than
  the lecturer become locks;
- the activity: spans marked with the privacy key, and mailboxes in front (the window
  titles of the lecturer's title list).
Without Windows the picture is not read, without the AssemblyAI key the sound is not
listened to; privacy.json says so, and the approval shows it.

Frames and sound stay on this machine, apart from the sound sent to AssemblyAI as for the
transcription.
"""
from __future__ import annotations

import datetime as dt
import re
import shutil
import subprocess
import tempfile
import wave
from collections import Counter
from pathlib import Path
from typing import Callable

import numpy as np

from . import audio, binaries, cut_rules, files, locks, ocr

GRID = 20.0                  # seconds between frames read in any case
SCENE = 0.08                 # picture change worth a look (ffmpeg's scene score; slides on white change little)
BLOCK = 60.0                 # seconds per block of the overview
PAUSE = 1.5                  # seconds without words that count as a pause
VOICE_GAP = 2.0              # words of the room closer than this form one span
VOICE_WORDS = 3              # a span of the room with at least this many words is a lock, shorter a candidate
QUIETER = 6.0                # dB below the lecturer: a voice from the room, not the lecturer misattributed

# a mail address ends in a country code or a common ending; "x@data.numeric" in R code is no address
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+ ?@ ?[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\."
                   r"(?:[A-Za-z]{2}|com|org|net|edu|gov|info|biz|app|dev|online|email|mail)\b", re.IGNORECASE)
SECRETS = [re.compile(p) for p in (
    r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}", r"\bgh[pousr]_[A-Za-z0-9]{30,}", r"\bgithub_pat_[A-Za-z0-9_]{30,}",
    r"\bAKIA[0-9A-Z]{16}\b", r"\bxox[abpr]-[A-Za-z0-9-]{10,}", r"\bAIza[0-9A-Za-z_-]{30,}",
    r"(?i)\b(?:password|passwort|kennwort|api[_ -]?key|secret|token)\s*[:=]\s*\S{6,}")]
LONG_TOKEN = re.compile(r"\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[a-z])(?=[A-Za-z0-9_-]*[A-Z])[A-Za-z0-9_-]{32,}\b")
NUMBER = re.compile(r"(?<![\d.,/:-])\d{6,9}(?![\d.,/:-])")
NAME_LINE = re.compile(r"^[A-ZÄÖÜ][a-zäöüß]+(?:-[A-ZÄÖÜ][a-zäöüß]+)?,\s+[A-ZÄÖÜ][a-zäöüß]{2,}")


# ---------------------------------------------------------------- the picture

def sample(video: Path, work: Path, grid: float = GRID) -> list[dict]:
    """Frames every grid seconds and at every clear change, in one pass, as JPEG files in work.
    Returns [{"t", "path", "change"}]; change says the frame was taken for a picture change."""
    every = max(1, round(grid * 2))
    pattern = work / "f_%05d.jpg"
    r = subprocess.run([binaries.ffmpeg(), "-hide_banner", "-nostats", "-i", str(video), "-an",
                        "-vf", f"fps=2,select='gt(scene,{SCENE})+not(mod(n,{every}))',"
                               f"scale='min(iw*4/3,{ocr.MAX_SIDE})':-2:flags=lanczos,showinfo",
                        "-fps_mode", "vfr", "-q:v", "3", str(pattern)],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=binaries.flags(low=True))
    if r.returncode != 0:
        raise RuntimeError("ffmpeg: " + r.stderr[-400:])
    times = [float(x) for x in re.findall(r"pts_time:([\d.]+)", r.stderr)]
    frames = sorted(work.glob("f_*.jpg"))
    return [{"t": round(t, 2), "path": p, "change": abs(t / grid - round(t / grid)) * grid > 0.3}
            for t, p in zip(times, frames)]


def _region(read: dict, text: str) -> list[float] | None:
    hits = ocr.find(read, text)
    if not hits:
        return None
    return hits[0]["region"]


def _union(regions: list) -> list[float] | None:
    regions = [r for r in regions if r]
    if not regions:
        return None
    x0 = min(r[0] for r in regions); y0 = min(r[1] for r in regions)
    x1 = max(r[0] + r[2] for r in regions); y1 = max(r[1] + r[3] for r in regions)
    return [round(x0, 4), round(y0, 4), round(x1 - x0, 4), round(y1 - y0, 4)]


def findings(read: dict, allow: list[str] | None = None) -> list[dict]:
    """What is private in one frame read by OCR: [{"kind", "text", "region", "lock"}]. lock False
    marks a candidate. allow: texts that are fine to show (e.g. the lecturer's own address)."""
    allowed = [a.lower() for a in (allow or []) if a]
    out = []

    def add(kind, text, region, lock):
        if any(a in text.lower() for a in allowed):
            return
        out.append({"kind": kind, "text": text, "region": region, "lock": lock})

    numbers = []
    names = []
    for line in read.get("lines", []):
        text = line.get("text", "")
        for m in EMAIL.finditer(text):
            add("email", m.group(0).replace(" ", ""), _region(read, m.group(0)), True)
        for pattern in SECRETS:
            for m in pattern.finditer(text):
                add("secret", m.group(0), _region(read, m.group(0)), True)
        for m in LONG_TOKEN.finditer(text):
            if not any(p.search(m.group(0)) for p in SECRETS):
                add("secret", m.group(0), _region(read, m.group(0)), False)
        found = NUMBER.findall(text)
        if found and "matrikel" in text.lower():
            add("student_number", " ".join(found), _region(read, found[0]), True)
        else:
            numbers += [(n, _region(read, n)) for n in found]
        if NAME_LINE.match(text.strip()):
            names.append((text.strip(), _region(read, text.strip())))
    distinct = {n for n, _ in numbers}
    if len(distinct) >= 3:
        add("number_list", f"{len(distinct)} Nummern", _union([r for _, r in numbers]), True)
    else:
        for n, r in numbers:
            add("number", n, r, False)
    if len(names) >= 4:
        add("name_list", f"{len(names)} Namen", _union([r for _, r in names]), False)
    return out


def slide_title(read: dict) -> str:
    """The title of a slide: the line in the upper third written largest; of lines about as large,
    the topmost (letters with descenders make a line of the same size look a little larger)."""
    height = float(read.get("height") or 1)
    lines = []
    for line in read.get("lines", []):
        words = line.get("words") or []
        text = line.get("text", "").strip()
        top = min((w["y"] for w in words), default=height)
        if words and top <= height / 3 and sum(ch.isalpha() for ch in text) >= 3:
            lines.append((sum(w["h"] for w in words) / len(words), top, text))
    if not lines:
        return ""
    largest = max(h for h, _, _ in lines)
    sizes = sorted(sum(w["h"] for w in line["words"]) / len(line["words"]) for line in read.get("lines", [])
                   if line.get("words"))
    if len(sizes) >= 4 and largest < 1.3 * sizes[len(sizes) // 2]:
        return ""                                    # nothing stands out: a program, not a slide
    return min((x for x in lines if x[0] >= 0.85 * largest), key=lambda x: x[1])[2][:120]


def _runs(samples: list[dict], hits: list[list[dict]], duration: float) -> list[dict]:
    """Findings of single frames joined over consecutive frames into spans. A span starts at the
    frame before the first hit, or at the hit itself if that frame was taken for a picture change,
    and ends at the frame after the last hit, so that the whole time the text may be seen is in it."""
    times = [s["t"] for s in samples]
    open_runs: dict[tuple, dict] = {}
    done = []
    for i, found in enumerate(hits):
        seen = set()
        for f in found:
            key = (f["kind"], f["text"].lower())
            seen.add(key)
            run = open_runs.get(key)
            if run is None:
                start = times[i] if samples[i]["change"] or i == 0 else times[i - 1]
                open_runs[key] = {**f, "start": start if i else 0.0, "first": i, "last": i, "regions": [f["region"]]}
            else:
                run["last"] = i
                run["regions"].append(f["region"])
        for key in [k for k in open_runs if k not in seen]:
            done.append(open_runs.pop(key))
    done += open_runs.values()
    out = []
    for run in done:
        j = run["last"] + 1
        end = times[j] if j < len(times) else duration
        out.append({"kind": run["kind"], "text": run["text"], "lock": run["lock"], "start": round(run["start"], 2),
                    "end": round(max(end, run["start"] + 0.5), 2), "region": _union(run["regions"]),
                    "frames": run["last"] - run["first"] + 1})
    return out


# ---------------------------------------------------------------- the sound

def _levels(wav: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(wav)) as w:
        rate = w.getframerate()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)      # int16: a lecture stays small
    return data, rate


def _db(data: np.ndarray, rate: int, a: float, b: float) -> float | None:
    chunk = data[int(max(0.0, a) * rate):int(max(0.0, b) * rate)].astype(np.float32) / 32768
    if not len(chunk):
        return None
    return float(20 * np.log10(np.sqrt(np.mean(chunk ** 2)) + 1e-9))


def voices(heard: list[dict], words: list[dict], level: Callable[[float, float], float | None],
           lecturer_level: float | None) -> list[dict]:
    """Spans of the room from a transcription with speaker labels: the lecturer is the speaker with
    the most words, everyone else is the room. A span with at least VOICE_WORDS words is a lock,
    unless it is as loud as the lecturer and the main transcript has the same words there (then the
    labels probably split the lecturer: a candidate)."""
    if not heard:
        return []
    lecturer = Counter(w.get("speaker", "?") for w in heard).most_common(1)[0][0]
    spans: list[list] = []
    for w in heard:
        if w.get("speaker", "?") == lecturer:
            continue
        if spans and w["start"] - spans[-1][1] <= VOICE_GAP and spans[-1][2] == w.get("speaker"):
            spans[-1][1] = w["end"]
            spans[-1][3].append(w["word"])
        else:
            spans.append([w["start"], w["end"], w.get("speaker"), [w["word"]]])
    out = []
    for a, b, speaker, said in spans:
        main = {re.sub(r"\W", "", x["word"].lower()) for x in words if x["end"] >= a - 0.3 and x["start"] <= b + 0.3}
        known = sum(1 for x in said if re.sub(r"\W", "", x.lower()) in main) / len(said)
        db = level(a, b)
        loud = lecturer_level is not None and db is not None and db > lecturer_level - QUIETER
        lock = len(said) >= VOICE_WORDS and not (loud and known >= 0.6)
        out.append({"kind": "room_voice", "text": " ".join(said[:12]), "lock": lock, "start": round(max(0.0, a - 0.2), 2),
                    "end": round(b + 0.2, 2), "region": None, "words": len(said),
                    "quieter": None if db is None or lecturer_level is None else round(lecturer_level - db, 1)})
    return out


# ---------------------------------------------------------------- the activity

def activity_locks(activity: list, private: list, titles: list[str]) -> list[dict]:
    out = [{"kind": "privacy_key", "text": "", "lock": True, "start": round(a, 2), "end": round(b, 2), "region": None,
            "sense": "both"} for a, b in private if b > a]
    words = [t.lower() for t in titles]
    spans: list[list] = []
    for start, end, _process, title in activity:
        hit = next((w for w in words if w and w in (title or "").lower()), None)
        if not hit or end - start < cut_rules.SHORTEST_SPAN:
            continue
        if spans and start - spans[-1][1] <= cut_rules.MERGE_GAP:
            spans[-1][1] = max(spans[-1][1], end)
        else:
            spans.append([start, end, hit])
    out += [{"kind": "mailbox", "text": hit, "lock": True, "start": round(a, 2), "end": round(b, 2), "region": None,
             "sense": "picture"} for a, b, hit in spans]
    return out


# ---------------------------------------------------------------- both stages

def _clock(t: float) -> str:
    h, rest = divmod(int(max(0.0, t)), 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60}:{rest % 60:02d}"


def overview(duration: float, words: list[dict], segments: list[dict], samples: list[dict], titles: list[str],
             activity: list, private: list, level: Callable[[float, float], float | None], room: list[dict]) -> dict:
    blocks = []
    t = 0.0
    while t < duration:
        b = min(duration, t + BLOCK)
        said = [w for w in words if t <= w["start"] < b]
        speech = sum(min(w["end"], b) - w["start"] for w in said)
        slides = [{"t": s["t"], "title": title} for s, title in zip(samples, titles)
                  if t <= s["t"] < b and s["change"] and title]
        db = level(t, b)
        blocks.append({
            "start": round(t, 1), "end": round(b, 1), "clock": _clock(t),
            "text": " ".join(s["text"] for s in segments if t <= s["start"] < b),
            "words": len(said), "speech": round(speech, 1), "silence": round(max(0.0, (b - t) - speech), 1),
            "level": None if db is None else round(db, 1),
            "changes": sum(1 for s in samples if t <= s["t"] < b and s["change"]), "slides": slides,
            "programs": sorted({p for a, e, p, _ in activity if e > t and a < b}),
            "private": any(e > t and a < b for a, e in private),
            "room_words": sum(r["words"] for r in room if t <= r["start"] < b)})
        t = b
    pauses = [[round(a["end"], 1), round(c["start"], 1)] for a, c in zip(words, words[1:]) if c["start"] - a["end"] >= PAUSE]
    deck, last = [], ""
    for s, title in zip(samples, titles):
        if title and title != last:
            deck.append({"t": s["t"], "title": title})
            last = title
    return {"duration": round(duration, 1), "blocks": blocks, "pauses": pauses,
            "changes": [s["t"] for s in samples if s["change"]], "slides": deck,
            "programs": cut_rules.program_summary(activity)}


def run(folder: Path, listener=None, settings: dict | None = None,
        progress: Callable[[str], None] | None = None, privacy: bool = True) -> dict:
    """Write overview.json and privacy.json for a session folder; returns privacy.json. privacy=False
    leaves privacy.json alone, for an outline made anew after the cut was approved."""
    folder = Path(folder)
    say = progress or (lambda text: None)
    pol = cut_rules.policy(settings)
    transcript = files.read_json(folder / "transcript.json")
    meta = files.read_json(folder / "session.json")
    log = files.read_json(folder / "activity.json")
    activity, private = log.get("activity", []), log.get("private", [])
    words, segments = transcript.get("words", []), transcript.get("segments", [])
    language = transcript.get("language") or meta.get("language") or "de"
    video = next((folder / n for n in ("recording.mp4", "screen.mp4") if (folder / n).exists()), None)
    if video is None:
        raise RuntimeError("Keine Aufnahme im Sitzungsordner.")
    duration = float(transcript.get("duration") or meta.get("duration_sec") or audio.duration(video))
    work = Path(tempfile.mkdtemp(prefix="flex3-screen-"))
    checked: dict = {"grid": GRID}
    try:
        say("Sichtung: die Bilder der Aufnahme …")
        samples = sample(video, work)
        checked["frames"] = len(samples)
        reads: list = [None] * len(samples)
        try:
            say(f"Sichtung: liest den Text in {len(samples)} Bildern …")
            reads = ocr.recognize_many([s["path"] for s in samples], language)
            checked["picture"] = True
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            checked["picture"] = False
            checked["picture_note"] = str(e)[:200]
        allow = pol.get("privacy_allow") or []
        hits = [findings(r, allow) if r else [] for r in reads]
        titles = [slide_title(r) if r else "" for r in reads]
        wav = work / "audio.wav"
        audio.extract(video, wav)
        data, rate = _levels(wav)
        level = lambda a, b: _db(data, rate, a, b)                          # noqa: E731
        spoken = [_db(data, rate, w["start"], w["end"]) for w in words[::max(1, len(words) // 2000)]]
        spoken = sorted(x for x in spoken if x is not None)
        lecturer_level = spoken[len(spoken) // 2] if spoken else None
        room: list[dict] = []
        if listener is not None:
            say("Sichtung: hört die ganze Aufnahme nach Stimmen aus dem Raum ab …")
            mp3 = work / "room.mp3"
            r = subprocess.run([binaries.ffmpeg(), "-hide_banner", "-v", "error", "-y", "-i", str(wav),
                                "-af", "dynaudnorm=f=150:g=15", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k", str(mp3)],
                               stdin=subprocess.DEVNULL, capture_output=True, creationflags=binaries.flags(low=True))
            if r.returncode != 0:
                raise RuntimeError("ffmpeg: " + r.stderr.decode("utf-8", "replace")[-300:])
            heard = listener.transcribe_file(mp3, language, [], speakers=True)["words"]
            room = voices(heard, words, level, lecturer_level)
            checked["sound"] = True
            checked["speakers"] = len({w.get("speaker") for w in heard})
        else:
            checked["sound"] = False
            checked["sound_note"] = "ohne AssemblyAI-Schlüssel nicht abgehört"
        say("Sichtung: fasst zusammen …")
        found = _runs(samples, hits, duration)
        for f in found:
            f["sense"] = "picture"
        for v in room:
            v["sense"] = "sound"
        found += room + activity_locks(activity, private, pol.get("cut_titles") or [])
        found.sort(key=lambda x: (x["start"], x["kind"]))
        result = {"version": 1, "created": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                  "checked": checked, "lecturer_level": None if lecturer_level is None else round(lecturer_level, 1),
                  "locks": [], "candidates": []}
        for x in found:
            entry = {"kind": x["kind"], "sense": x["sense"], "start": x["start"], "end": x["end"],
                     "region": x.get("region"),
                     "text": x["text"] if x["kind"] == "room_voice" else locks.mask(x["text"], x["kind"]),
                     **{k: x[k] for k in ("frames", "words", "quieter") if x.get(k) is not None}}
            target = result["locks"] if x["lock"] else result["candidates"]
            entry["id"] = ("S" if x["lock"] else "K") + str(len(target) + 1)
            target.append(entry)
        files.write_json(folder / "overview.json", overview(duration, words, segments, samples, titles, activity,
                                                             private, level, room))
        if privacy:
            files.write_json(folder / "privacy.json", result)
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)
