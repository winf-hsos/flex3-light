"""First proposals for the cut, from rules alone: cheap, local, no model.

- edges: the silence before the first and after the last spoken word;
- pauses: gaps between words longer than a set length, to be replaced by a card
  "Pause entfernt", shortened or cut (the lecturer's setting);
- programs: spans in which a program or window title was in front that the lecturer
  wants out of the video. A browser is a presentation tool for one lecturer and a mail
  client for the next, so this is a policy: programs always cut (accepted at once),
  programs always kept (never proposed), and suggestions and title patterns (proposed).

Whether the first words are a microphone check is not a rule's business; the agent
decides that by reading, looking and listening.
"""
from __future__ import annotations

from . import edit

DEFAULT_POLICY = {
    "pause_min": 30.0,               # seconds without a word
    "pause_mode": "card",            # "card" (Pause entfernt), "shorten" or "cut"
    "cut_programs": [],              # always cut, from the lecturer's settings
    "keep_programs": [],             # never proposed
    "suggest_programs": ["outlook.exe", "olk.exe", "hxoutlook.exe", "thunderbird.exe", "whatsapp.exe",
                         "signal.exe", "telegram.exe", "explorer.exe"],
    "cut_titles": ["posteingang", "inbox", "gmail", "whatsapp web", "outlook"],
    "kinds_off": [],                 # kinds of items the lecturer does not want proposed, e.g. blur or zoom
}
EDGE_MIN = 3.0                       # seconds of silence at an edge worth a proposal
SHORTEST_SPAN = 1.0                  # program spans shorter than this are Alt+Tab blips
MERGE_GAP = 2.0                      # spans of the same rule closer than this become one


CARD_DEFAULTS = {"title_card": True, "closing_card": True, "chapter_display": "card", "chapter_sound": True,
                 "music": True}
AFTER_RECORDING = ("nothing", "transcribe", "cut")   # what starts by itself once a recording is filed


def policy(settings: dict | None) -> dict:
    return {**DEFAULT_POLICY, **(settings or {})}


def settings_from(preferences: dict) -> dict:
    """The lecturer's settings for the cut from the preferences of their profile ({key: value},
    the cut ones as "cut.<field>"): cards, the rule policy, and "auto" when the cut proposal
    follows the transcription by itself. The same in the app and on the server."""
    cut = {k[4:]: v for k, v in preferences.items() if k.startswith("cut.")}
    cut.pop("auto", None)                       # an old switch, replaced by after_recording on 2026-11-04
    return {**CARD_DEFAULTS, **policy(cut), "auto": after_recording(preferences) == "cut"}


CUT_FIELDS = ("pause_mode", "pause_min", "cut_programs", "keep_programs", "cut_titles", "kinds_off",
              "title_card", "closing_card", "chapter_display", "chapter_sound", "music")


def cut_preferences(current: dict, values: dict) -> dict:
    """The preferences "cut.<field>" after a change of the cut settings: current are the lecturer's
    cut preferences without the prefix, values what changes. Every field is checked and completed,
    so the result is whole; the same in the app and on the server."""
    current = {k: v for k, v in current.items() if k in CUT_FIELDS}
    current.update({k: v for k, v in values.items() if k in CUT_FIELDS})
    if current.get("pause_mode") not in ("card", "shorten", "cut"):
        current["pause_mode"] = "card"
    current["pause_min"] = min(600.0, max(10.0, float(current.get("pause_min", 30))))
    for k in ("cut_programs", "keep_programs", "cut_titles"):
        # a list never set keeps its default: until 2026-11-09 it became empty here, which switched
        # the title rule (Posteingang, Inbox, ...) off for everyone who changed any cut setting
        given = current.get(k, DEFAULT_POLICY[k])
        current[k] = sorted({str(x).strip().lower() for x in given if str(x).strip()})
    current["kinds_off"] = sorted({k for k in current.get("kinds_off", []) if k in edit.KINDS})
    if current.get("chapter_display") not in edit.CHAPTER_DISPLAYS:
        current["chapter_display"] = "card"
    for k in ("title_card", "closing_card", "chapter_sound", "music"):
        current[k] = bool(current.get(k, True))
    return {f"cut.{k}": v for k, v in current.items()}


def after_recording(preferences: dict) -> str:
    value = preferences.get("after_recording")
    return value if value in AFTER_RECORDING else "nothing"


def edges(words: list[dict], duration: float) -> list[dict]:
    out = []
    if not words:
        return out
    if words[0]["start"] > EDGE_MIN:
        out.append(edit.item("cut", 0.0, words[0]["start"] - 0.4, "Stille vor dem ersten Wort", "rule", 0.9,
                             "accepted", "dead_time", rule="edge"))
    if duration - words[-1]["end"] > EDGE_MIN:
        out.append(edit.item("cut", words[-1]["end"] + 0.6, duration, "Stille nach dem letzten Wort", "rule", 0.9,
                             "accepted", "dead_time", rule="edge"))
    return out


def pauses(words: list[dict], pol: dict) -> list[dict]:
    out = []
    for a, b in zip(words, words[1:]):
        gap = b["start"] - a["end"]
        if gap >= pol["pause_min"]:
            minutes = gap / 60
            text = f"{minutes:.0f} Minuten ohne Sprache" if minutes >= 1.5 else f"{gap:.0f} Sekunden ohne Sprache"
            out.append(edit.item("pause", a["end"] + 0.5, b["start"] - 0.5, text, "rule", 0.7, "proposed", "pause",
                                 mode=pol["pause_mode"], length=round(gap, 1), rule="pause"))
    return out


def _merge(spans: list[list]) -> list[list]:
    merged: list[list] = []
    for s in sorted(spans):
        if merged and s[0] - merged[-1][1] <= MERGE_GAP and s[2] == merged[-1][2]:
            merged[-1][1] = max(merged[-1][1], s[1])
            merged[-1][3] |= s[3]
        else:
            merged.append(list(s))
    return merged


def programs(activity: list, pol: dict) -> list[dict]:
    """activity: [start, end, process, window title] spans from activity.json."""
    always = {p.lower() for p in pol["cut_programs"]}
    keep = {p.lower() for p in pol["keep_programs"]}
    suggest = {p.lower() for p in pol["suggest_programs"]}
    titles = [t.lower() for t in pol["cut_titles"]]
    spans = []
    for start, end, process, title in activity:
        exe = (process or "").lower()
        if exe in keep or end - start < SHORTEST_SPAN:
            continue
        hit = next((t for t in titles if t in (title or "").lower()), None)
        if exe in always:
            spans.append([start, end, f"{process} (immer schneiden)", {"accepted"}])
        elif exe in suggest:
            spans.append([start, end, f"{process} im Vordergrund", {"proposed"}])
        elif hit:
            spans.append([start, end, f"Fenster „{hit}“ in {process}", {"proposed"}])
    return [edit.item("cut", a, b, why, "rule", 0.8 if "accepted" in states else 0.5,
                      "accepted" if "accepted" in states else "proposed", "privacy", rule="program")
            for a, b, why, states in _merge(spans)]


def without_titles(activity: list, titles: list[str]) -> list[list]:
    """The activity as it may leave the device (decision of 2026-11-06): per span start, end and
    program, and instead of the window title only the word of the title list it contains, or ""
    if none. programs() above gives the same result on this as on the full log, and the
    agent sees which word matched instead of the title."""
    words = [t.lower() for t in titles]
    out = []
    for start, end, process, title in activity:
        hit = next((w for w in words if w in (title or "").lower()), "")
        out.append([start, end, process, hit])
    return out


def program_summary(activity: list) -> list[dict]:
    """Every program in front during the recording with its time, longest first, for the approval."""
    total: dict[str, float] = {}
    for start, end, process, _title in activity:
        total[process] = total.get(process, 0.0) + max(0.0, end - start)
    return [{"program": p, "seconds": round(s, 1)} for p, s in sorted(total.items(), key=lambda x: -x[1])]


def propose(words: list[dict], duration: float, activity: list, settings: dict | None = None) -> list[dict]:
    pol = policy(settings)
    found = edges(words, duration) + pauses(words, pol) + programs(activity, pol)
    return [x for x in found if x["kind"] not in pol["kinds_off"]]
