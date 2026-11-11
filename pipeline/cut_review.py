"""Stage E of the cut: the proposal checked on its rendered result (plan of 2026-11-10, phase 5).

The edit list only says what should happen; this looks at what comes out, without rendering
the whole lecture:
- the sound of the long version is made exactly as render.py makes it (render.sound(), about a
  minute for three hours); around every junction a few seconds of it are transcribed again
  (AssemblyAI, one request for all junctions) and compared with the words the transcript
  expects there, so that a clipped word, a remnant or a sound that does not belong shows up;
- a word the cut runs through is found from the word times alone;
- every privacy lock the edit list hides and that stays in the video is looked at in the result:
  its sound measured in the rendered sound (a mute must be silent), its picture rendered through
  the same filter graph as the long version (render.build with a window) at its start, middle
  and end, read by the OCR and searched with the safety net's own rules (screening.findings);
- a model reads the words on both sides of every junction and names transitions that refer to
  something cut away or do not sound like one thought.

review.json keeps the findings with the signature of the list they were found in. A lock seen
in the result counts as open (locks.state) as long as the items around it stay as they were, so
it blocks approval and rendering like any other lock; everything else is a warning. After an
agent's proposal, the sections with findings get one more conversation each (cut_stages) to fix
them, and the check runs again, at most PASSES times.
"""
from __future__ import annotations

import bisect
import datetime as dt
import difflib
import re
import shutil
import subprocess
import tempfile
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import numpy as np

from . import binaries, cut_rules, edit, files, llm, locks, ocr, outline, render, screening, timeline

VERSION = 1
AROUND = 4.0          # seconds of rendered sound on each side of a junction that are transcribed again
NEAR = 2.5            # seconds on each side compared word by word
GAP = 1.5             # silence between the junctions in the file sent for transcription
LOUD_DB = -45.0       # a part of a lock louder than this in the rendered sound is heard in any case
LOUDER = 6.0          # ... and so is one this many dB above the room's noise floor around it (a voice from the room
                      # sits 5 to 11 dB above it, measured on two lectures; -45 dB alone missed every one)
SILENT_DB = -100.0    # digital silence: a mute or a pause's card
CLIPPED = 0.06        # seconds of a word on both sides of a bound that make it clipped
SIMILAR = 0.75        # below this likeness of heard and expected letters, a junction sounds different
CONTEXT_WORDS = 30    # words on each side of a junction the model reads
PASSES = 2            # rounds of fixes after the agent's proposal
WORKERS = 4

KINDS = {
    "clipped_word": "Wort angeschnitten",
    "sounds_different": "Übergang klingt anders als erwartet",
    "refers_to_cut": "Verweis auf Herausgeschnittenes",
    "not_one_thought": "Übergang holpert",
    "transition": "Übergang prüfen",
    "lock_audible": "Gesperrte Stimme im Ergebnis hörbar",
    "lock_visible": "Gesperrter Text im Ergebnis sichtbar",
}
BLOCKING = {"lock_audible", "lock_visible"}
PICTURE_KINDS = {"email", "secret", "student_number", "number_list", "name_list"}   # what screening.findings reads

TOOL = llm.Tool("report_transitions", "The junctions whose transition is not right; leave out every junction that is fine.", {
    "type": "object", "additionalProperties": False, "required": ["problems"],
    "properties": {"problems": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["junction", "kind", "note"],
        "properties": {"junction": {"type": "integer"},
                       "kind": {"type": "string", "enum": ["refers_to_cut", "not_one_thought", "clipped_word", "other"]},
                       "note": {"type": "string"}}}}}})

INSTRUCTIONS = """You check the transitions of an edited lecture recording. At every junction a span was cut out;
you get the words before and after it as they now follow each other, what was cut and why, and, where available,
what a second transcription heard at the junction in the edited sound.

Report a junction only if a viewer would notice it:
- refers_to_cut: the words after it refer to something that is gone ("as you just saw", "as I said", an answer to
  a question that was cut, "that one" pointing at a removed demo);
- not_one_thought: the two pieces do not read as one thought (a sentence broken off, a new start in the middle);
- clipped_word: a word is cut in half or a fragment of a word is left;
- other: anything else a viewer would stumble over.
A cut between two complete sentences is fine even if the topic moves on. Say in one sentence in {ui_language} what
is wrong and what would fix it (move the cut to …, keep …). Call report_transitions once, with an empty list if all
is fine."""

FIX = """The check of the rendered result found these problems in your section. Fix what the edit list can fix:
move a bound out of a word, take a remnant out, keep a sentence the transition needs, mute instead of cut where the
lecturer's speech goes on, widen or extend a blur. Leave what is fine and say why in section_done. Do not change
anything else in the section. Where a privacy lock is still readable, the finding names the region (x, y, width,
height as fractions of the frame) in which it was read: the blur must cover that region."""


def _clock(t: float) -> str:
    return outline._clock(t)


def _source(folder: Path) -> Path:
    source = folder / "recording.mp4"
    return source if source.exists() else folder / "screen.mp4"


def plan(folder: Path, e: dict) -> dict:
    """timeline.plan of the list with what render.sound() needs: the source and the silent spans."""
    source = _source(folder)
    fps = render.probe(source)["fps"]
    p = timeline.plan(e, fps)
    accepted = [x for x in e["items"] if x["state"] == "accepted" and x["end"] is not None]
    p["silent"] = [(x["start"], x["end"]) for x in accepted if x["kind"] == "mute"] + [(a, b) for a, b, _ in p["cards"]]
    p["source"], p["fps"] = str(source), fps
    return p


def junctions(p: dict) -> list[dict]:
    """Where two kept spans meet: the end of the one before, the start of the one after (recording
    times) and where they meet in the edited video."""
    out, pos = [], 0.0
    kept = p["kept"]
    for i, (a, b) in enumerate(kept):
        pos += b - a
        if i + 1 < len(kept):
            out.append({"no": len(out) + 1, "left": b, "right": kept[i + 1][0], "out": round(pos, 3)})
    return out


def _token(word: str) -> str:
    return re.sub(r"[^\w]", "", word.lower())


def _kept_words(words: list[dict], p: dict, starts: list[float], a: float, b: float) -> list[dict]:
    """The transcript's words starting in a..b that stay whole in the video, with their time in it."""
    out = []
    for w in words[bisect.bisect_left(starts, a):bisect.bisect_right(starts, b)]:
        if any(k0 - 0.01 <= w["start"] and w["end"] <= k1 + 0.01 for k0, k1 in p["kept"]):
            out.append({**w, "out": timeline.map_forward(w["start"], p)})
    return out


def clipped(words: list[dict], starts: list[float], j: dict) -> list[dict]:
    """Words the junction runs through: part of them stays, part is cut away."""
    out = []
    for bound in (j["left"], j["right"]):
        lo = bisect.bisect_left(starts, bound - 5)
        for w in words[lo:bisect.bisect_right(starts, bound)]:
            if w["start"] + CLIPPED < bound < w["end"] - CLIPPED:
                out.append(w)
    return out


# ---------------------------------------------------------------- sound
def _read(wav_path: Path, a: float, b: float) -> np.ndarray:
    """Mono samples of the WAV from a to b seconds, as floats from -1 to 1."""
    with wave.open(str(wav_path)) as w:
        rate, ch = w.getframerate(), w.getnchannels()
        first = max(0, int(a * rate))
        last = min(w.getnframes(), int(b * rate))
        if last <= first:
            return np.zeros(0, dtype=np.float32)
        w.setpos(first)
        data = np.frombuffer(w.readframes(last - first), dtype=np.int16).astype(np.float32) / 32768.0
    return data.reshape(-1, ch).mean(axis=1) if ch > 1 else data


def levels(samples: np.ndarray, rate: int, window: float = 0.05) -> np.ndarray:
    """The level in dB of every window of 50 ms: a quarter second of a voice left over at the edge of a
    mute must not vanish in the silence of the rest (measured: the RMS of the whole span did)."""
    n = max(1, int(rate * window))
    k = samples.size // n
    if k == 0:
        return np.zeros(0)
    rms = np.sqrt(np.mean(samples[:k * n].reshape(k, n) ** 2, axis=1))
    return np.where(rms > 1e-10, 20 * np.log10(np.maximum(rms, 1e-10)), -200.0)


def audible(wav_path: Path, lo: float, hi: float, total: float) -> float | None:
    """The loudest level of lo..hi of the rendered sound (seconds of the edited video) when at least 0.1 s of
    it is heard: louder than LOUD_DB, or LOUDER above the noise floor of the 15 s around it; else None."""
    rate = _rate(wav_path)
    inside = levels(_read(wav_path, lo, hi), rate)
    around = levels(_read(wav_path, max(0.0, lo - 15), min(total, hi + 15)), rate)
    sound = around[around > SILENT_DB]
    floor = float(np.percentile(sound, 10)) if sound.size else -90.0
    heard = inside[(inside > LOUD_DB) | (inside > floor + LOUDER)]
    return float(heard.max()) if heard.size >= 2 else None


def _rate(wav_path: Path) -> int:
    with wave.open(str(wav_path)) as w:
        return w.getframerate()


def listen_at(wav_path: Path, total: float, js: list[dict], listener, language: str, work: Path) -> dict[int, list[dict]]:
    """What a second transcription hears around every junction, as {junction: [{"word", "out"}]} with times
    in the edited video; one file and one request for all junctions."""
    rate = 16000
    parts, offsets, pos = [], [], 0.0
    gap = np.zeros(int(GAP * rate), dtype=np.float32)
    with wave.open(str(wav_path)) as w:
        src_rate = w.getframerate()
    for j in js:
        a, b = max(0.0, j["out"] - AROUND), min(total, j["out"] + AROUND)
        samples = _read(wav_path, a, b)
        if src_rate != rate and samples.size:               # simple decimation is enough for speech recognition
            idx = np.linspace(0, samples.size - 1, int(samples.size * rate / src_rate)).astype(int)
            samples = samples[idx]
        offsets.append((pos, a, samples.size / rate, j["no"]))
        parts += [samples, gap]
        pos += samples.size / rate + GAP
    if not parts:
        return {}
    joined = work / "junctions.wav"
    with wave.open(str(joined), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(np.concatenate(parts), -1, 1) * 32767).astype(np.int16).tobytes())
    mp3 = work / "junctions.mp3"
    subprocess.run([binaries.ffmpeg(), "-y", "-v", "error", "-i", str(joined), "-c:a", "libmp3lame", "-b:a", "64k",
                    str(mp3)], stdin=subprocess.DEVNULL, capture_output=True, check=True, creationflags=binaries.flags())
    heard = listener.transcribe_file(mp3, language, [])
    out: dict[int, list[dict]] = {no: [] for *_, no in offsets}
    starts = [o[0] for o in offsets]
    for w in heard.get("words", []):
        k = bisect.bisect_right(starts, w["start"]) - 1
        if k < 0:
            continue
        begin, a, length, no = offsets[k]
        if w["start"] - begin <= length:
            out[no].append({"word": w["word"], "out": round(a + w["start"] - begin, 2)})
    return out


def compare(expected: list[dict], heard: list[dict], at: float) -> tuple[float, str, str] | None:
    """How alike the expected and the heard words near a junction are; None when there is too little to say.
    Letters are compared, not words: a second transcription of the same sound splits and joins words
    differently ("auf das" and "aufs", measured on a lecture: 0.57 by words, 0.84 by letters)."""
    exp = [w for w in expected if abs(w["out"] - at) <= NEAR]
    got = [w for w in heard if abs(w["out"] - at) <= NEAR]
    a = [t for t in (_token(w["word"]) for w in exp) if t]
    b = [t for t in (_token(w["word"]) for w in got) if t]
    if len(a) + len(b) < 4:
        return None
    ratio = difflib.SequenceMatcher(None, "".join(a), "".join(b)).ratio()
    return ratio, " ".join(w["word"] for w in exp), " ".join(w["word"] for w in got)


# ---------------------------------------------------------------- pictures
def frame(folder: Path, e: dict, t: float, work: Path, name: str) -> Path | None:
    """The frame of the long version at recording time t, through the same filter graph as the render."""
    sub = work / name
    sub.mkdir(exist_ok=True)
    try:
        inputs, graph, _p, _w = render.build(folder, e, sub, offset=t, length=0.5)
        script = sub / "graph.txt"
        script.write_text(graph, encoding="utf-8")
        out = sub / "frame.png"
        render._run([*inputs, "-/filter_complex", str(script), "-map", "[vout]", "-frames:v", "1", str(out)], 0, None)
        return out if out.exists() else None
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return None


def _overlap(r1, r2) -> bool:
    x1, y1, w1, h1 = r1
    x2, y2, w2, h2 = r2
    return x1 < x2 + w2 and x2 < x1 + w1 and y1 < y2 + h2 and y2 < y1 + h1


def _stays(lock: dict, p: dict) -> list[tuple[float, float]]:
    """The parts of a lock's span that stay in the video and are not a pause's card."""
    a, b = lock["start"], lock["end"]
    stays = [(max(a, k0), min(b, k1)) for k0, k1 in p["kept"] if k1 > a and k0 < b]
    cards = [(c0, c1) for c0, c1, _ in p["cards"]]
    return [q for s0, s1 in stays for q in locks._uncovered(s0, s1, cards) if q[1] - q[0] > 0.1]


def _times(stays: list[tuple[float, float]]) -> list[float]:
    """Start, middle and end of what stays of a lock, a little inside."""
    if not stays:
        return []
    total = sum(b - a for a, b in stays)
    first, last = stays[0][0] + 0.1, stays[-1][1] - 0.1
    mid, acc = None, 0.0
    for a, b in stays:
        if acc + (b - a) >= total / 2:
            mid = a + total / 2 - acc
            break
        acc += b - a
    return sorted({round(t, 2) for t in (first, mid, last) if t is not None})


# ---------------------------------------------------------------- the check
def run(folder: Path, key, listener=None, settings: dict | None = None,
        progress: Callable[[str], None] | None = None, models: dict | None = None, client=None) -> dict:
    """Check the edit list of a session folder on its rendered result; writes and returns review.json."""
    folder = Path(folder)
    say = progress or (lambda text: None)
    started = time.monotonic()
    e = edit.load(folder)
    if not e:
        raise ValueError("Keine Schnittliste.")
    settings = settings or {}
    transcript = files.read_json(folder / "transcript.json")
    language = transcript.get("language") or files.read_json(folder / "session.json").get("language") or "de"
    ui = settings.get("ui_language", "de")
    words = sorted(transcript.get("words", []), key=lambda w: w["start"])
    starts = [w["start"] for w in words]
    p = plan(folder, e)
    js = junctions(p)
    findings: list[dict] = []
    skipped: list[str] = []
    seen: dict[str, str] = {}
    usage = llm.Usage()
    model = ""
    work = Path(tempfile.mkdtemp(prefix="flex3-review-"))

    def add(kind: str, at: float, text: str, **extra) -> None:
        findings.append({"id": f"P{len(findings) + 1}", "kind": kind, "label": KINDS.get(kind, kind),
                         "severity": "block" if kind in BLOCKING else "warn", "at": round(at, 2),
                         "out": timeline.map_forward(at, p), "clock": _clock(at), "text": text, **extra})

    try:
        # 1. words the cut runs through, from the word times
        for j in js:
            for w in clipped(words, starts, j):
                bound = j["left"] if w["start"] < j["left"] < w["end"] else j["right"]
                add("clipped_word", j["right"], f"„{w['word']}“ wird bei {_clock(bound)} durchgeschnitten.",
                    junction=j["no"])
        # 2. the sound of the long version, as the render makes it
        say(f"Prüfung: Ton der Langfassung mit {len(js)} Übergängen …")
        wav = render.sound(p, work)
        heard: dict[int, list[dict]] = {}
        if listener is not None and js:
            say(f"Prüfung: hört {len(js)} Übergänge noch einmal ab …")
            try:
                heard = listen_at(wav, p["duration"], js, listener, language, work)
            except Exception as ex:              # noqa: BLE001 - the rest of the check goes on without it
                skipped.append(f"Übergänge nicht abgehört: {ex}"[:200])
        elif js:
            skipped.append("Übergänge nicht abgehört: kein AssemblyAI-Schlüssel")
        context: dict[int, dict] = {}
        for j in js:
            before = _kept_words(words, p, starts, j["left"] - 60, j["left"])[-CONTEXT_WORDS:]
            after = _kept_words(words, p, starts, j["right"], j["right"] + 60)[:CONTEXT_WORDS]
            context[j["no"]] = {"before": before, "after": after}
            if j["no"] in heard:
                result = compare(before + after, heard[j["no"]], j["out"])
                if result and result[0] < SIMILAR and not any(      # two junctions close together hear the same
                        f.get("heard") == result[2] and f.get("expected") == result[1] for f in findings):
                    add("sounds_different", j["right"], "Im geschnittenen Ton klingt der Übergang anders als erwartet.",
                        junction=j["no"], expected=result[1], heard=result[2], likeness=round(result[0], 2))
        # 3. the locks the list hides, in the result
        state = [x for x in locks.state(folder, e) if x["status"] == "hidden" or x.get("seen")]   # seen before: again
        picture_checks = []
        for lock in state:
            stays = _stays(lock, p)
            if not stays:
                continue
            if lock["sense"] in ("sound", "both"):
                for a, b in stays:
                    if b - a < 0.3:
                        continue
                    lo, hi = timeline.map_forward(a, p) + 0.1, timeline.map_forward(b, p) - 0.1
                    db = audible(wav, lo, hi, p["duration"])
                    if db is not None:
                        add("lock_audible", a, f"Sperre {lock['id']} ({lock['label']}) ist im geschnittenen Ton zu hören "
                            f"({db:.0f} dB), obwohl die Liste sie stummschaltet.", lock=lock["id"])
                        seen[lock["id"]] = locks.fingerprint(lock, e)
                        break
            if lock["sense"] in ("picture", "both") and lock["kind"] in PICTURE_KINDS:
                picture_checks += [(lock, t) for t in _times(stays)]
        if picture_checks:
            say(f"Prüfung: rendert {len(picture_checks)} Bilder an gesperrten Stellen …")
            frames = [frame(folder, e, t, work, f"f{i}") for i, (_, t) in enumerate(picture_checks)]
            reads: list = [None] * len(frames)
            readable = [f for f in frames if f]
            try:
                got = iter(ocr.recognize_many(readable, language))
                reads = [next(got) if f else None for f in frames]
            except (RuntimeError, subprocess.TimeoutExpired) as ex:
                skipped.append(f"Bilder nicht gelesen: {ex}"[:200])
            allow = cut_rules.policy(settings).get("privacy_allow") or []
            for (lock, t), read in zip(picture_checks, reads):
                if read is None or lock["id"] in seen:
                    continue
                region = lock.get("region") or [0.0, 0.0, 1.0, 1.0]
                hits = [h for h in screening.findings(read, allow)
                        if h["lock"] and h["kind"] == lock["kind"] and h.get("region") and _overlap(h["region"], region)]
                if hits:
                    box = [round(v, 4) for v in hits[0]["region"]]
                    add("lock_visible", t, f"Sperre {lock['id']} ({lock['label']}) ist im gerenderten Bild bei {_clock(t)} "
                        f"lesbar, obwohl die Liste sie verdeckt; gelesen im Bereich {box}.", lock=lock["id"], region=box)
                    seen[lock["id"]] = locks.fingerprint(lock, e)
        # 4. the transitions, read by a model
        if js:
            say(f"Prüfung: ein Sprachmodell liest {len(js)} Übergänge …")
            try:
                if client is None:
                    keys = key if callable(key) else (lambda service: key if service == "openai" else None)
                    client = llm.make(llm.stage_config("review", models), keys)
                model = getattr(client, "model", "")
                reasons = {j["no"]: [x["reason"] for x in e["items"] if x["state"] == "accepted"
                                     and x["kind"] in edit.REMOVING and x["end"] > j["left"] - 0.5
                                     and x["start"] < j["right"] + 0.5] for j in js}
                text = "\n\n".join(
                    f"Junction {j['no']} at {_clock(j['out'])} of the edited video: {j['right'] - j['left']:.0f} s cut "
                    f"({'; '.join(reasons[j['no']][:2]) or 'no reason given'}).\n"
                    f"Before: …{' '.join(w['word'] for w in context[j['no']]['before'])}\n"
                    f"After: {' '.join(w['word'] for w in context[j['no']]['after'])}…"
                    + (f"\nHeard at the junction: {' '.join(w['word'] for w in heard[j['no']])}" if heard.get(j["no"]) else "")
                    for j in js)
                language_name = {"de": "German", "en": "English"}.get(ui, ui)
                reply = client.complete(INSTRUCTIONS.replace("{ui_language}", language_name),
                                        [llm.Message("user", [llm.Text(text)])], [TOOL])
                usage.add(reply.usage)
                by_no = {j["no"]: j for j in js}
                for c in reply.calls:
                    if c.name != "report_transitions":
                        continue
                    for prob in c.arguments.get("problems") or []:
                        j = by_no.get(int(prob.get("junction") or 0))
                        if j is None:
                            continue
                        kind = prob.get("kind")
                        if kind not in ("refers_to_cut", "not_one_thought", "clipped_word"):
                            kind = "transition"
                        if kind == "clipped_word" and any(f["kind"] == "clipped_word" and f.get("junction") == j["no"]
                                                         for f in findings):
                            continue
                        add(kind, j["right"], str(prob.get("note") or "").strip() or KINDS[kind], junction=j["no"],
                            by="model")
            except llm.ModelError as ex:
                skipped.append(f"Übergänge nicht gelesen: {ex}"[:200])
    finally:
        shutil.rmtree(work, ignore_errors=True)
    findings.sort(key=lambda f: f["at"])
    for i, f in enumerate(findings, 1):
        f["id"] = f"P{i}"
    review = {"version": VERSION, "checked": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
              "signature": render.signature(e), "junctions": len(js),
              "locks": sum(1 for x in locks.state(folder, e) if x["status"] != "lifted"),
              "findings": findings, "seen": seen, "skipped": skipped,
              "agent": {"model": model, "usage": usage.as_dict(), "seconds": round(time.monotonic() - started, 1)}}
    files.write_json(folder / "review.json", review)
    return review


def load(folder: Path) -> dict:
    return files.read_json(Path(folder) / "review.json")


def view(folder: Path, e: dict) -> dict | None:
    """What the approval shows of the check: its findings, and whether it was made for the list as it is."""
    r = load(folder)
    if not r:
        return None
    if r.get("error"):
        return {"error": r["error"], "checked": r.get("checked")}
    return {**{k: r.get(k) for k in ("checked", "junctions", "locks", "findings", "skipped", "passes", "agent")},
            "current": r.get("signature") == render.signature(e)}


# ---------------------------------------------------------------- fixing after the agent's proposal
def fix(folder: Path, key, listener, settings: dict, findings: list[dict],
        progress: Callable[[str], None] | None = None, models: dict | None = None) -> dict:
    """One more conversation for every section with findings, with the strong model; returns its record."""
    from . import cut_stages                                   # cut_stages imports the tools; only needed here
    from .cut_agent import AGENT_COPY, tools_for
    from .cut_tools import Workbench

    say = progress or (lambda text: None)
    keys = key if callable(key) else (lambda service: key if service == "openai" else None)
    config = llm.stage_config("section", models)
    bench = Workbench(Path(folder), settings, listener)
    started = time.monotonic()
    try:
        staged = cut_stages.StagedCut(bench, lambda stage: llm.make(config, keys), say, WORKERS)
        o = outline.load(bench.folder)
        sections = cut_stages.plan_sections({} if o.get("error") else o, bench.duration, bench.segments)
        targets = []
        for sec in sections:
            mine = [f for f in findings if sec["start"] <= f["at"] < sec["end"]]
            if mine:
                targets.append(({**sec, "light": False}, mine))
        ui = bench.settings.get("ui_language", "de")
        language = {"de": "German", "en": "English"}.get(ui, ui)
        tools = [t for t in tools_for(bench.kinds_off) if t["name"] not in cut_stages.SECTION_ONLY] + [cut_stages.SECTION_DONE]
        instructions = cut_stages.SECTION.replace("{ui_language}", language)
        say(f"Prüfung: {len(targets)} Abschnitte beheben {len(findings)} Befunde …")

        def one(target):
            sec, mine = target
            lines = "\n".join(f"- {f['clock']} ({f['at']:.1f} s) {f['label']}: {f['text']}"
                              + (f" Expected: {f['expected']} | heard: {f['heard']}" if f.get("heard") is not None else "")
                              for f in mine)
            return staged._section(sec, len(sections), instructions, tools, extra=f"{FIX}\n{lines}")

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            agents = list(pool.map(one, targets))
        usage = llm.Usage()
        for a in agents:
            usage.add(a.usage)
        record = {"sections": [s["no"] for s, _ in targets], "findings": len(findings),
                  "rounds": sum(a.rounds for a in agents), "images": sum(a.images for a in agents),
                  "usage": usage.as_dict(), "seconds": round(time.monotonic() - started, 1),
                  "model": config.get("model", "")}
        bench.edit.setdefault("agent", {}).setdefault("review_fixes", []).append(record)
        bench.save()
        shutil.copyfile(bench.folder / "edit.json", bench.folder / AGENT_COPY)
        return record
    finally:
        bench.close()


def _same(a: dict, b: dict) -> bool:
    """Whether a finding of a later check is still the one of an earlier: the same lock, or the same kind
    within a few seconds (junction numbers shift when a fix adds or removes one)."""
    if a.get("lock") or b.get("lock"):
        return a.get("lock") == b.get("lock") and a["kind"] == b["kind"]
    return a["kind"] == b["kind"] and abs(a["at"] - b["at"]) < 5.0


def check_and_fix(folder: Path, key, listener=None, settings: dict | None = None,
                  progress: Callable[[str], None] | None = None, models: dict | None = None) -> dict:
    """After the agent's proposal: check, let the sections fix what was found, check again; at most PASSES
    rounds of fixes. A failure of the check never costs the proposal: review.json then says what went wrong."""
    folder = Path(folder)
    passes = []
    try:
        review = run(folder, key, listener, settings, progress, models)
        while review["findings"] and len(passes) < PASSES:
            before = review["findings"]
            record = fix(folder, key, listener, settings or {}, before, progress, models)
            review = run(folder, key, listener, settings, progress, models)
            record["found"] = [{**{k: f.get(k) for k in ("kind", "label", "at", "clock", "text")},
                                "fixed": not any(_same(f, g) for g in review["findings"])} for f in before]
            record["fixed"] = sum(1 for f in record["found"] if f["fixed"])
            passes.append(record)
            if record["fixed"] == 0:
                break
        if passes:
            review["passes"] = passes
            files.write_json(folder / "review.json", review)
        return review
    except Exception as ex:                    # noqa: BLE001 - the proposal stands without a check
        review = {"version": VERSION, "error": f"Prüfung fehlgeschlagen: {ex}"[:400], "passes": passes,
                  "checked": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
        files.write_json(folder / "review.json", review)
        return review
