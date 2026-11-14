"""Languages of the videos (Nicolas, 2026-11-13): subtitles in more languages than the one spoken, and
captions burned into the picture with the word being spoken highlighted.

Rendering gives every video subtitles in the language of its sound (render.py, products.py). Here the
lecturer chooses per video, the long version and each topic video, in which other languages it gets
subtitles and in which languages a version with captions burned into the picture is made. The
profile gives the choice for new recordings (cut.subtitle_languages, cut.burned_captions); it is
fixed in languages.json the first time this step reaches a recording, so a later change of the
settings does not reach back to recordings already done.

  languages.json          "defaults" (the settings when it was fixed), "choices" per video id ("long",
                          or the id of a topic video) with "languages" and "burned", and per file
                          made here its signature ("made")
  translation.<l>.json    the subtitles of the whole recording in language l: per cue of the spoken
                          subtitles the words it covers (indices into transcript.json) and the text,
                          translated once for every video
  <video>.<l>.vtt         subtitles of a video in language l; the spoken language's come from rendering
  <video>.burned.<l>.mp4  the video with captions in language l in the picture

A language model translates cue by cue, a batch of cues per request with the module's technical terms,
so the subtitles keep the timing of the spoken ones: a cue appears in a video when at least half of
its words remain after the cut. Only in the spoken language does every word have a time of its own,
so burned-in captions highlight the word being spoken there and show whole cues in a translation.
They look as the server's style says ("captions", style.py).

The package (publish.py) carries the subtitles as files next to each video, the way Opencast takes
them, and inside each video as tracks to switch; the burned-in versions are videos of their own.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from . import edit, files, llm, products, subtitles, timeline
from . import render as rendering
from . import style as styles

NAMES = {"de": "Deutsch", "en": "Englisch", "fr": "Französisch", "es": "Spanisch", "it": "Italienisch",
         "nl": "Niederländisch", "pl": "Polnisch", "tr": "Türkisch", "uk": "Ukrainisch", "ar": "Arabisch",
         "zh": "Chinesisch"}
ISO3 = {"de": "ger", "en": "eng", "fr": "fre", "es": "spa", "it": "ita", "nl": "dut", "pl": "pol", "tr": "tur",
        "uk": "ukr", "ar": "ara", "zh": "chi"}           # what an MP4 subtitle track names its language with
ENGLISH = {"de": "German", "en": "English", "fr": "French", "es": "Spanish", "it": "Italian", "nl": "Dutch",
           "pl": "Polish", "tr": "Turkish", "uk": "Ukrainian", "ar": "Arabic", "zh": "Simplified Chinese"}
FILE = "languages.json"
LONG = "long"            # the id of the long version among the videos
BATCH = 60               # cues per request to the language model
PARALLEL = 4             # requests at the same time
KEEP_SHARE = 0.5         # a translated cue appears in a video when this share of its words remains
BURNED = re.compile(r"^(edited|topic-\d{2})\.burned\.([a-z]{2})\.mp4$")


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _sig(*parts) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def spoken(folder: Path) -> str:
    """The language of the sound, as the transcript says."""
    folder = Path(folder)
    return files.read_json(folder / "transcript.json").get("language") or \
        files.read_json(folder / "session.json").get("language") or "de"


def load(folder: Path) -> dict:
    return files.read_json(Path(folder) / FILE)


def defaults(folder: Path, settings: dict | None) -> dict:
    """The choice for every video of a recording from the lecturer's settings."""
    settings = settings or {}
    extra = [x for x in settings.get("subtitle_languages") or [] if x in NAMES and x != spoken(folder)]
    return {"languages": extra, "burned": [spoken(folder)] if settings.get("burned_captions") else []}


def fixed(folder: Path, settings: dict | None) -> dict:
    """languages.json, fixed with the choice of the settings if there is none yet."""
    data = load(folder)
    if not data:
        data = {"defaults": defaults(folder, settings), "choices": {}, "made": {}, "fixed": _now()}
        files.write_json(Path(folder) / FILE, data)
    return data


def choice(data: dict, vid: str) -> dict:
    return (data.get("choices") or {}).get(vid) or data.get("defaults") or {"languages": [], "burned": []}


def videos(folder: Path) -> list[dict]:
    """The rendered videos of a recording: the long version first, then the approved topic videos, each
    with a signature that changes whenever the video is rendered anew."""
    folder = Path(folder)
    long = files.read_json(folder / "render.json")
    if not long:
        return []
    out = [{"id": LONG, "name": "edited", "title": "Langfassung", "parent": None,
            "signature": _sig(long.get("signature"), long.get("rendered"))}]
    proposed = {x["id"]: x for x in products.load(folder).get("products", [])}
    for v in products.finished(folder):
        x = proposed.get(v["id"]) or {}
        out.append({"id": v["id"], "name": v["name"], "title": v.get("title") or x.get("title", ""),
                    "parent": x.get("parent"),
                    "signature": _sig(v.get("signature"), v["name"]), "video": v})
    return out


def _source(folder: Path) -> tuple[list[dict], str]:
    """The words of the transcript with their index, and a signature of them."""
    words = files.read_json(Path(folder) / "transcript.json").get("words", [])
    indexed = [{**w, "i": k} for k, w in enumerate(words)]
    return indexed, _sig([(w["word"], w["start"], w["end"]) for w in words])


def _captions_signature(style: dict) -> str:
    return _sig(style["captions"], style["font"]["semibold"])


def wanted(folder: Path, data: dict | None = None, style: dict | None = None) -> dict[str, dict]:
    """Every file this step should have made: name -> kind ("vtt", "burned"), video, language, signature,
    and for a burned-in video the signature of the captions' look (only with style)."""
    folder = Path(folder)
    data = load(folder) if data is None else data
    if not data:
        return {}
    src = spoken(folder)
    _, source = _source(folder)
    look = _captions_signature(style) if style else None
    out = {}
    for v in videos(folder):
        c = choice(data, v["id"])
        for lang in c.get("languages", []):
            if lang in NAMES and lang != src:
                out[f"{v['name']}.{lang}.vtt"] = {"kind": "vtt", "video": v["id"], "language": lang,
                                                  "signature": _sig(v["signature"], "vtt", lang, source)}
        for lang in c.get("burned", []):
            if lang in NAMES or lang == src:
                out[f"{v['name']}.burned.{lang}.mp4"] = {"kind": "burned", "video": v["id"], "language": lang,
                                                         "signature": _sig(v["signature"], "burned", lang, source),
                                                         "style": look}
    return out


def due(folder: Path, style: dict | None = None, data: dict | None = None) -> bool:
    """Whether run() has something to do: a file missing, made for something else, or no longer wanted.
    Without style the look of burned-in captions is not compared."""
    folder = Path(folder)
    data = load(folder) if data is None else data
    if not data:
        return False                      # nothing fixed yet: the runner fixes it when it gets there
    want, made = wanted(folder, data, style), data.get("made") or {}
    if set(made) - set(want):
        return True
    return any(made.get(n, {}).get("signature") != w["signature"] or not (folder / n).exists()
               or (style is not None and w["kind"] == "burned" and made[n].get("style") != w["style"])
               for n, w in want.items())


def finished(folder: Path) -> list[dict]:
    """The files made here that belong to the videos as they are now: name, kind, video name, language."""
    folder = Path(folder)
    data = load(folder)
    want, made = wanted(folder, data), (data or {}).get("made") or {}
    names = {v["id"]: v["name"] for v in videos(folder)}
    return [{"file": n, "kind": w["kind"], "video": names[w["video"]], "language": w["language"]}
            for n, w in want.items() if made.get(n, {}).get("signature") == w["signature"] and (folder / n).exists()]


def set_choices(folder: Path, given: dict) -> str:
    """The lecturer's choice {video id: {"languages": [...], "burned": [...]}}; returns an error or ''."""
    folder = Path(folder)
    data = load(folder) or {"defaults": {"languages": [], "burned": []}, "choices": {}, "made": {}, "fixed": _now()}
    ids = {v["id"] for v in videos(folder)}
    if not ids:
        return "Erst wenn die Langfassung gerendert ist, lassen sich Sprachen wählen."
    src = spoken(folder)
    choices = dict(data.get("choices") or {})
    for vid, c in (given or {}).items():
        if vid not in ids or not isinstance(c, dict):
            return "Dieses Video gibt es nicht mehr."
        langs = [x for x in dict.fromkeys(c.get("languages") or []) if x in NAMES and x != src]
        burned = [x for x in dict.fromkeys(c.get("burned") or []) if x in NAMES or x == src]
        choices[vid] = {"languages": langs, "burned": burned}
    data["choices"] = choices
    files.write_json(folder / FILE, data)
    return ""


# ---------------------------------------------------------------- translation

TRANSLATE = """You translate the subtitles of a university lecture from {source} into {target}. Each cue is a
short piece of what the lecturer says, shown for a few seconds. Translate every cue on its own so it fits its
place in time, but read the cues around it for the meaning; finish a sentence where the cue ends, even if the
lecturer's sentence goes on in the next cue. Keep it spoken and plain, as subtitles are read quickly: no
additions, no explanations, no notes. Keep technical terms, names of programs, code and formulas as they are,
or in the form usual in {target}. Keep each translation about as long as its cue, at most 90 characters.

Answer with JSON only, no other text: {{"cues": [{{"n": <number of the cue>, "text": "<its translation>"}}]}},
one entry for every cue given, with its number."""


def cues(words: list[dict]) -> list[dict]:
    """The cues of the spoken subtitles: the index of their first and last word, how many words, the text."""
    out = []
    for g in subtitles.groups([w for w in words if w["word"].strip()]):
        out.append({"from": g[0]["i"], "to": g[-1]["i"], "words": len(g), "text": " ".join(w["word"] for w in g)})
    return out


def _parse(text: str) -> dict[int, str]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return {}
    out = {}
    for x in data.get("cues", []) if isinstance(data, dict) else []:
        if isinstance(x, dict) and isinstance(x.get("n"), int) and isinstance(x.get("text"), str):
            out[x["n"]] = " ".join(x["text"].split())
    return out


def translation(folder: Path, language: str) -> dict:
    return files.read_json(Path(folder) / f"translation.{language}.json")


def translate(folder: Path, language: str, client, terms: list[str] | None = None,
              progress: Callable[[str], None] | None = None) -> dict:
    """translation.<language>.json for the transcript as it is now; made only if missing or stale."""
    folder = Path(folder)
    say = progress or (lambda t: None)
    words, source = _source(folder)
    have = translation(folder, language)
    if have.get("source") == source:
        return have
    src = spoken(folder)
    all_cues = cues(words)
    instructions = TRANSLATE.format(source=ENGLISH.get(src, src), target=ENGLISH.get(language, language))
    glossary = ", ".join((terms or [])[:300])
    usage, started = llm.Usage(), time.monotonic()
    batches = [list(range(k, min(k + BATCH, len(all_cues)))) for k in range(0, len(all_cues), BATCH)]
    done = [0]

    def ask(numbers: list[int]) -> dict[int, str]:
        before = " ".join(all_cues[k]["text"] for k in range(max(0, numbers[0] - 3), numbers[0]))
        after = " ".join(all_cues[k]["text"] for k in range(numbers[-1] + 1, min(len(all_cues), numbers[-1] + 3)))
        body = ((f"Technical terms of the module: {glossary}\n\n" if glossary else "")
                + (f"Said just before (do not translate): {before}\n\n" if before else "")
                + "Cues:\n" + json.dumps([{"n": k, "text": all_cues[k]["text"]} for k in numbers], ensure_ascii=False)
                + (f"\n\nSaid right after (do not translate): {after}" if after else ""))
        reply = client.complete(instructions, [llm.Message("user", [llm.Text(body)])], [])
        usage.add(reply.usage)
        return _parse(reply.text)

    def batch(numbers: list[int]) -> dict[int, str]:
        got = ask(numbers)
        missing = [k for k in numbers if not got.get(k)]
        if missing:                                    # once more for what did not come back
            got.update({k: t for k, t in ask(missing).items() if k in missing})
        missing = [k for k in numbers if not got.get(k)]
        if missing:
            raise llm.ModelError(f"Das Sprachmodell hat {len(missing)} Untertitel nicht übersetzt.")
        done[0] += 1
        say(f"Übersetzung ins {NAMES.get(language, language)}e, {done[0]} von {len(batches)} Teilen …")
        return got

    say(f"Übersetzung ins {NAMES.get(language, language)}e …")
    texts: dict[int, str] = {}
    with ThreadPoolExecutor(PARALLEL) as pool:
        for got in pool.map(batch, batches):
            texts.update(got)
    result = {"language": language, "spoken": src, "source": source, "made": _now(),
              "model": getattr(client, "model", ""), "seconds": round(time.monotonic() - started, 1),
              "usage": usage.as_dict(),
              "cues": [{**{k: c[k] for k in ("from", "to", "words")}, "text": texts[n]} for n, c in enumerate(all_cues)]}
    files.write_json(folder / f"translation.{language}.json", result)
    return result


# ---------------------------------------------------------------- the words of each video

class Timing:
    """Where the words of the recording land in each video, as rendering put them."""

    def __init__(self, folder: Path, style_dir: Path | None = None):
        self.folder = Path(folder)
        self.e = edit.load(self.folder)
        self.p = products._long_plan(self.folder, self.e)
        self.s = rendering.measured(styles.load(style_dir))
        self.ins = timeline.inserts(self.e, self.p, self.s)
        self.words, _ = _source(self.folder)

    def words_of(self, video: dict) -> list[dict]:
        if video["id"] == LONG:
            return timeline.words({"words": self.words}, self.p, ins=self.ins)
        v = video["video"]
        a, b = v["start"], v["end"]
        own = products._own_inserts(self.e, {"title": v["title"]}, round(b - a, 3), self.s)
        return [{**w, "start": timeline.shift(w["start"], own), "end": timeline.shift(w["end"], own)}
                for w in timeline.words({"words": self.words}, self.p, a, b, self.ins)]


def placed(timed: list[dict], translated: list[dict]) -> list[tuple[float, float, str]]:
    """The translated cues in a video: each where its words are, if at least half of them remain; one after
    the other without overlap."""
    at = {w["i"]: w for w in timed if w["word"].strip()}
    out: list[list] = []
    for c in translated:
        kept = [at[i] for i in range(c["from"], c["to"] + 1) if i in at]
        if not kept or len(kept) < KEEP_SHARE * c["words"]:
            continue
        out.append([kept[0]["start"], kept[-1]["end"], c["text"]])
    out.sort(key=lambda x: x[0])
    for a, b in zip(out, out[1:]):
        a[1] = min(a[1], b[0])
    return [(a, b, t) for a, b, t in out if b - a > 0.05]


def vtt(entries: list[tuple[float, float, str]]) -> str:
    body = ["WEBVTT", ""]
    for a, b, text in entries:
        body += [f"{subtitles._time(a)} --> {subtitles._time(b)}", subtitles._lines(text.split()), ""]
    return "\n".join(body)


# ---------------------------------------------------------------- captions burned into the picture

def _ass_time(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    h, rest = divmod(cs, 360000)
    m, rest = divmod(rest, 6000)
    s, cs = divmod(rest, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _bgr(hex_color: str) -> str:
    """#rrggbb as ASS writes a color: blue, green, red."""
    return (hex_color[5:7] + hex_color[3:5] + hex_color[1:3]).upper()


def _ass_color(hex_color: str, alpha: int = 0) -> str:
    """A color of a style line, with its transparency (0 opaque, 255 invisible)."""
    return f"&H{alpha:02X}{_bgr(hex_color)}"


def _escape(word: str) -> str:
    return word.replace("\\", "⧵").replace("{", "(").replace("}", ")")


def _broken(words: list[str]) -> list[int]:
    """The line of each word, broken as subtitles._lines breaks."""
    line, cur, out = 0, "", []
    for w in words:
        if cur and len(cur) + 1 + len(w) > subtitles.LINE:
            line, cur = line + 1, w
        else:
            cur = f"{cur} {w}".strip()
        out.append(line)
    return out


def _joined(words: list[str], lit: int | None, color: str) -> str:
    lines = _broken(words)
    parts = []
    for k, w in enumerate(words):
        if k:
            parts.append("\\N" if lines[k] != lines[k - 1] else " ")
        text = _escape(w)
        parts.append(f"{{\\1c&H{color}&}}{text}{{\\1c&HFFFFFF&}}" if k == lit else text)
    return "".join(parts)


def font_file(style: dict) -> tuple[Path | None, str]:
    """(file, name for the captions) of the style's semibold font; without a file, a common sans font."""
    from PIL import ImageFont
    try:
        f = ImageFont.truetype(styles.font(style, "semibold"), 20)
    except OSError:
        return None, "DejaVu Sans"
    family, weight = f.getname()
    return Path(f.path), family if weight in ("Regular", "Book", "Normal") else f"{family} {weight}"


def ass(timed: list[dict] | None, translated: list[tuple[float, float, str]] | None, W: int, H: int,
        style: dict, font_name: str) -> str:
    """Captions as an ASS file: with timed words, one event per word with that word highlighted; with
    translated cues, one event per cue."""
    c = style["captions"]
    size = round(H * c["size"] / 100)
    box = _ass_color("#000000", round(255 * (1 - c["box"])))
    lit = _bgr(c["highlight"])
    head = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 2",
            "ScaledBorderAndShadow: yes", "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
            "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding",
            f"Style: Caption,{font_name},{size},&H00FFFFFF,&H00FFFFFF,{box},{box},0,0,0,0,100,100,0,0,3,"
            f"{max(2, round(size * 0.28))},0,2,{round(W * 0.08)},{round(W * 0.08)},{round(H * 0.06)},1",
            "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    events = []
    if timed is not None:
        for g in subtitles.groups([w for w in timed if w["word"].strip()]):
            words = [w["word"] for w in g]
            for k, w in enumerate(g):
                a = g[0]["start"] if k == 0 else w["start"]
                b = g[k + 1]["start"] if k + 1 < len(g) else g[-1]["end"]
                if b - a < 0.01:
                    continue
                events.append(f"Dialogue: 0,{_ass_time(a)},{_ass_time(b)},Caption,,0,0,0,,{_joined(words, k, lit)}")
    for a, b, text in translated or []:
        events.append(f"Dialogue: 0,{_ass_time(a)},{_ass_time(b)},Caption,,0,0,0,,{_joined(text.split(), None, lit)}")
    return "\n".join(head + events) + "\n"


def _filter_path(p: Path) -> str:
    return "'" + p.as_posix().replace(":", "\\:").replace("'", "\\'") + "'"


def burn(video: Path, captions: str, out: Path, style: dict, video_args: list[str],
         progress: Callable[[float], None] | None = None) -> None:
    """The video with the captions in the picture, sound copied as it is."""
    work = Path(tempfile.mkdtemp(prefix="flex3-captions-"))
    try:
        (work / "captions.ass").write_text(captions, encoding="utf-8")
        fonts = work / "fonts"
        fonts.mkdir()
        font, _ = font_file(style)
        if font and font.exists():
            shutil.copy(font, fonts / font.name)
        graph = f"subtitles=filename={_filter_path(work / 'captions.ass')}:fontsdir={_filter_path(fonts)}"
        part = out.with_name(out.name + ".part")
        rendering._run(["-i", str(video), "-vf", graph, "-map", "0:v:0", "-map", "0:a:0?", *video_args,
                        "-c:a", "copy", "-f", "mp4", str(part)], rendering.probe(video)["duration"], progress)
        part.replace(out)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        out.with_name(out.name + ".part").unlink(missing_ok=True)


# ---------------------------------------------------------------- the step

def run(folder: Path, key, settings: dict | None = None, config: dict | None = None,
        video_args: list[str] | None = None, style_dir: Path | None = None, terms: list[str] | None = None,
        progress: Callable[[str], None] | None = None, client=None) -> dict:
    """Make what the choice of the recording asks for and remove what it no longer asks for; returns
    languages.json."""
    folder = Path(folder)
    say = progress or (lambda t: None)
    data = fixed(folder, settings)
    style = styles.load(style_dir)
    want = wanted(folder, data, style)
    made = dict(data.get("made") or {})
    src = spoken(folder)
    todo = {n: w for n, w in want.items()
            if made.get(n, {}).get("signature") != w["signature"] or not (folder / n).exists()
            or (w["kind"] == "burned" and made[n].get("style") != w["style"])}
    needed = sorted({w["language"] for w in todo.values() if w["language"] != src})
    if needed and client is None:
        keys = key if callable(key) else (lambda service: key if service == "openai" else None)
        client = llm.make(config or llm.stage_config("translate"), keys)
    translated = {lang: translate(folder, lang, client, terms, say)["cues"] for lang in needed}
    timing = Timing(folder, style_dir) if todo else None
    by_id = {v["id"]: v for v in videos(folder)}
    font = font_file(style)[1]
    for k, (name, w) in enumerate(sorted(todo.items()), 1):
        v = by_id[w["video"]]
        timed = timing.words_of(v)
        if w["kind"] == "vtt":
            (folder / name).write_text(vtt(placed(timed, translated[w["language"]])), encoding="utf-8")
        else:
            source = folder / f"{v['name']}.mp4"
            info = rendering.probe(source)
            text = ass(timed if w["language"] == src else None,
                       None if w["language"] == src else placed(timed, translated[w["language"]]),
                       info["width"], info["height"], style, font)
            label = f"{v['title']}, {NAMES.get(w['language'], w['language'])}"
            burn(source, text, folder / name, style, video_args or rendering.X264,
                 lambda share: say(f"Untertitel ins Bild ({label}), {share * 100:.0f} %"))
        made[name] = {k2: w[k2] for k2 in ("kind", "video", "language", "signature")} | \
            ({"style": w["style"]} if w["kind"] == "burned" else {}) | {"made": _now()}
    for name in set(made) - set(want):                # chosen away, or the video is gone
        (folder / name).unlink(missing_ok=True)
        made.pop(name)
    data = {**load(folder), "made": made, "ran": _now()}
    files.write_json(folder / FILE, data)
    return data


def view(folder: Path, settings: dict | None = None) -> dict:
    """What the window shows: the videos with their choice and files, the languages to choose from."""
    folder = Path(folder)
    data = load(folder)
    shown = data or {"defaults": defaults(folder, settings), "choices": {}, "made": {}}
    src = spoken(folder)
    want = wanted(folder, shown)
    made = shown.get("made") or {}
    out = []
    for v in videos(folder):
        c = choice(shown, v["id"])
        mine = [{"file": n, "kind": w["kind"], "language": w["language"],
                 "ready": made.get(n, {}).get("signature") == w["signature"] and (folder / n).exists()}
                for n, w in sorted(want.items()) if w["video"] == v["id"]]
        out.append({"id": v["id"], "name": v["name"], "title": v["title"], "parent": v["parent"],
                    "languages": c.get("languages", []), "burned": c.get("burned", []), "files": mine})
    return {"spoken": src, "names": NAMES, "fixed": bool(data), "defaults": shown.get("defaults"),
            "videos": out, "due": bool(data) and due(folder, None, data),
            "translations": {lang: {k: t.get(k) for k in ("model", "made", "seconds")}
                             for lang in NAMES if (t := translation(folder, lang))}}
