"""What a recording cost: tokens per model and step, audio hours, and the price of it all.

Every call to a language model or a transcription service made while the pipeline works on a
recording is written to usage.jsonl in its folder, one line per call: the step (stage of
llm.py, or "transcribe", "screening", "review" for audio), the model, and the tokens or the
seconds of audio. The pipeline works on one recording at a time (pipeline_runner.py), so the
folder is a setting of the process (working_on) that the threads of a step share.

Recordings processed before 2026-11-19 have no such list. For them view() estimates the use
from what the steps left behind: the rounds in edit.log.jsonl, the agents in outline.json and
review.json, the length of the transcript and of the screening's listening. What the steps
did not record (the summary, feedback on the topic videos) is named as missing.

Prices are USD and come from PRICES, which the server's data folder can override with
prices.json ({"<model>": {"input": .., "cached": .., "output": ..}} per million tokens,
{"<model>": {"hour": .., "terms": ..}} per hour of audio).
"""
from __future__ import annotations

import datetime as dt
import json
import threading
from contextlib import contextmanager
from pathlib import Path

FILE = "usage.jsonl"

# Per million tokens (input / from the cache / output), as OpenAI listed them on 2026-11-11; per hour
# of audio as AssemblyAI and OpenAI listed them on 2026-10-27 ("terms": extra for a word list).
PRICES: dict[str, dict] = {
    "gpt-6.1-sol": {"input": 2.00, "cached": 0.10, "output": 10.00},
    "gpt-6.1-terra": {"input": 1.00, "cached": 0.05, "output": 5.00},
    "gpt-5.4-mini": {"input": 0.75, "cached": 0.075, "output": 4.50},
    "gpt-6-luna": {"input": 0.10, "cached": 0.01, "output": 0.50},
    "assemblyai/universal-3-pro": {"hour": 0.21, "terms": 0.05},
    "openai/whisper-1": {"hour": 0.36},
}
PRICE_DATE = "Preise der Anbieter vom 27.10. und 11.11.2026"
price_file: Path | None = None              # set by the server: prices.json in its data folder

# The steps as the page names them, in the order of the pipeline.
GROUPS = [("transcribe", "Transkription"), ("screening", "Sichtung"), ("outline", "Gliederung"),
          ("cut", "Schnitt"), ("review", "Prüfung am Ergebnis"), ("topics", "Themenvideos"),
          ("translate", "Sprachen"), ("summary", "Zusammenfassung")]
_GROUP_OF = {"section": "cut", "section_light": "cut", "closing": "cut"}

_lock = threading.Lock()
_folder: Path | None = None


def group_of(step: str) -> str:
    step = (step or "cut").split(" ")[0]       # "section 5" in the cut's log
    return _GROUP_OF.get(step, step)


# ---------------------------------------------------------------- recording

@contextmanager
def working_on(folder: Path):
    """Calls inside this block, in any thread, are counted for this recording."""
    global _folder
    before, _folder = _folder, Path(folder)
    try:
        yield
    finally:
        _folder = before


def _append(entry: dict) -> None:
    folder = _folder
    if folder is None or not folder.exists():
        return                                  # a test or a call outside the pipeline
    line = json.dumps({"at": dt.datetime.now().astimezone().isoformat(timespec="seconds"), **entry}, ensure_ascii=False)
    try:
        with _lock, open(folder / FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass                                    # counting must never stop the work


def note_tokens(step: str, model: str, usage: dict) -> None:
    """One call of a language model; usage as llm.Usage.as_dict()."""
    _append({"step": step or "cut", "model": model, "input": int(usage.get("input_tokens", 0)),
             "cached": int(usage.get("cached_tokens", 0)), "output": int(usage.get("output_tokens", 0)),
             "reasoning": int(usage.get("reasoning_tokens", 0))})


def note_audio(step: str, model: str, seconds: float, terms: bool = False, speakers: bool = False) -> None:
    """One file sent to a transcription service."""
    _append({"step": step, "model": model, "seconds": round(float(seconds or 0), 1), "terms": bool(terms),
             "speakers": bool(speakers)})


# ---------------------------------------------------------------- reading

def prices() -> dict:
    custom = {}
    if price_file is not None:
        try:
            custom = json.loads(Path(price_file).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            custom = {}
    return {**PRICES, **{k: v for k, v in custom.items() if isinstance(v, dict)}}


def cost(entry: dict, table: dict) -> float | None:
    """USD for one entry, None if the model has no price."""
    p = table.get(entry["model"])
    if not p:
        return None
    if "seconds" in entry:
        if "hour" not in p:
            return None
        return entry["seconds"] / 3600 * (p["hour"] + (p.get("terms", 0) if entry.get("terms") else 0))
    if "input" not in p:
        return None
    fresh = max(entry.get("input", 0) - entry.get("cached", 0), 0)
    return (fresh * p["input"] + entry.get("cached", 0) * p.get("cached", p["input"])
            + entry.get("output", 0) * p["output"]) / 1_000_000


def _read_lines(path: Path) -> list[dict]:
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def _json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _tokens(step: str, model: str, usage: dict) -> dict:
    return {"step": step, "model": model, "input": usage.get("input_tokens", 0), "cached": usage.get("cached_tokens", 0),
            "output": usage.get("output_tokens", 0), "reasoning": usage.get("reasoning_tokens", 0)}


def estimate(folder: Path) -> tuple[list[dict], list[str]]:
    """Entries rebuilt from the files of the steps, for recordings without usage.jsonl, and the
    steps that ran but left no numbers."""
    folder = Path(folder)
    entries, missing = [], []
    t = _json(folder / "transcript.json")
    if t.get("model") and t.get("duration"):
        entries.append({"step": "transcribe", "model": t["model"], "seconds": float(t["duration"]),
                        "terms": bool(t.get("terms")), "speakers": False})
    if (_json(folder / "privacy.json").get("checked") or {}).get("sound"):
        seconds = _json(folder / "overview.json").get("duration") or t.get("duration") or 0
        if seconds:
            entries.append({"step": "screening", "model": "assemblyai/universal-3-pro", "seconds": float(seconds),
                            "terms": False, "speakers": True})
    agent = _json(folder / "outline.json").get("agent") or {}
    if agent.get("usage"):
        entries.append(_tokens("outline", agent.get("model", ""), agent["usage"]))
    for e in _read_lines(folder / "edit.log.jsonl"):     # every round of the cut, its fixes included
        if e.get("event") == "round" and e.get("usage"):
            entries.append(_tokens(e.get("stage") or "cut", e.get("model", ""), e["usage"]))
    agent = _json(folder / "review.json").get("agent") or {}
    if agent.get("usage"):
        entries.append(_tokens("review", agent.get("model", ""), agent["usage"]))
    if (folder / "summary.json").exists():
        missing.append("Zusammenfassung")
    notes = _json(folder / "products.json").get("notes") or []
    if notes:
        missing.append("Rückmeldungen zu den Themenvideos")
    return entries, missing


def view(folder: Path) -> dict | None:
    """Tokens and cost per step and model for the page; None if nothing was spent yet."""
    folder = Path(folder)
    logged = _read_lines(folder / FILE)
    estimated, missing = estimate(folder)
    have = {group_of(e["step"]) for e in logged}
    # a step in the list counts from the list; the others, from before the list, from their files
    rebuilt = [e for e in estimated if group_of(e["step"]) not in have]
    entries = logged + rebuilt
    missing = [m for m in missing if not (m == "Zusammenfassung" and "summary" in have)
               and not (m.startswith("Rückmeldungen") and "topics" in have)]
    if not entries:
        return None
    table, rows = prices(), {}
    for e in entries:
        key = (group_of(e["step"]), e.get("model") or "unbekannt")
        r = rows.setdefault(key, {"calls": 0, "input": 0, "cached": 0, "output": 0, "reasoning": 0, "seconds": 0.0,
                                  "cost": 0.0, "priced": True})
        r["calls"] += 1
        for k in ("input", "cached", "output", "reasoning", "seconds"):
            r[k] += e.get(k, 0) or 0
        c = cost({**e, "model": key[1]}, table)
        if c is None:
            r["priced"] = False
        else:
            r["cost"] += c
    order = {g: i for i, (g, _) in enumerate(GROUPS)}
    labels = dict(GROUPS)
    out = [{"step": g, "label": labels.get(g, g), "model": m, **{k: (round(v, 4) if k == "cost" else
                                                                      round(v, 1) if k == "seconds" else v)
                                                                  for k, v in r.items()}}
           for (g, m), r in sorted(rows.items(), key=lambda x: (order.get(x[0][0], 99), x[0][1]))]
    return {"rows": out, "total": round(sum(r["cost"] for r in out if r["priced"]), 4),
            "unpriced": sorted({r["model"] for r in out if not r["priced"]}),
            "estimated": sorted({labels.get(group_of(e["step"]), e["step"]) for e in rebuilt}),
            "missing": missing, "prices": PRICE_DATE}
