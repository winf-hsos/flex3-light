"""Step "transcribe": recording.mp4 -> transcript.json with word timestamps.

The audio goes to a transcription service in pieces cut at pauses (audio.py). Each
piece gets the module's technical terms and the end of the previous text as prompt,
so terms and sentences carry across the cuts. What Whisper invents in silence
("Thank you", "Untertitel von ...", the prompt repeated) is recognised; such a piece
is asked again without prompt, and words that lie inside a pause are dropped.
Finished pieces are cached in work/, so an interruption costs nothing twice.

The service sits behind a small adapter. AssemblyAI (Universal-3 Pro, in the EU) won
the comparison of 2026-10-27 (docs/pipeline.md): it takes the whole recording at once,
with the terms as key terms, and returns timed words with punctuation and sentences,
so pieces, prompt and punctuation repair are only needed for OpenAIWhisper. That one
uses whisper-1, the only OpenAI model with word timestamps, and stays as the fallback
until it is shut down on 2027-02-26.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Callable

from . import audio

PROMPT_LIMIT = 800          # characters; whisper-1 takes at most 224 tokens of prompt
# Phrases Whisper writes into silence, longest first; German ones come from subtitled TV in its training data.
INVENTED = ("untertitel im auftrag des zdf", "untertitelung des zdf", "untertitel der amara.org-community",
            "vielen dank fürs zuschauen", "thanks for watching", "für funk", "thank you", "untertitel",
            "amara.org", "copyright", "zdf", "swr")


class TranscriptionError(RuntimeError):
    pass


class OpenAIWhisper:
    name = "openai/whisper-1"
    URL = "https://api.openai.com/v1/audio/transcriptions"

    def __init__(self, key: str, timeout: float = 300):
        self._key, self.timeout = key, timeout

    def __repr__(self) -> str:
        return "OpenAIWhisper()"                 # never the key

    def transcribe(self, mp3: Path, language: str, prompt: str) -> dict:
        """{"text", "words": [{"word", "start", "end"}], "segments": [{"start", "end", "text"}]}"""
        fields = [("model", "whisper-1"), ("response_format", "verbose_json"), ("language", language),
                  ("timestamp_granularities[]", "word"), ("timestamp_granularities[]", "segment")]
        if prompt:
            fields.append(("prompt", prompt))
        boundary = uuid.uuid4().hex
        parts = [f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode("utf-8")
                 for k, v in fields]
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{mp3.name}\"\r\n"
                     f"Content-Type: audio/mpeg\r\n\r\n".encode("utf-8") + mp3.read_bytes() + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        req = urllib.request.Request(self.URL, data=b"".join(parts), method="POST", headers={
            "Authorization": f"Bearer {self._key}", "Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise TranscriptionError(f"OpenAI antwortet mit Fehler {e.code}: {detail}") from None
        return {"text": body.get("text", ""), "words": body.get("words", []), "segments": body.get("segments", [])}


class AssemblyAI:
    """Universal-3 Pro in the region the key belongs to; the transcript is deleted there afterwards."""
    name = "assemblyai/universal-3-pro"
    whole = True                                 # one file per recording, no pieces
    MODEL = "universal-3-pro"
    POLL = 5                                     # seconds between status questions
    LONGEST_WAIT = 2 * 3600

    def __init__(self, key: str, base: str, timeout: float = 300):
        self._key, self.base, self.timeout = key, base.rstrip("/"), timeout

    def __repr__(self) -> str:
        return f"AssemblyAI({self.base})"         # never the key

    def _call(self, method: str, path: str, body: dict | None = None, raw: bytes | None = None) -> dict:
        headers = {"Authorization": self._key}
        if body is not None:
            raw = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=raw, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise TranscriptionError(f"AssemblyAI antwortet mit Fehler {e.code}: {detail}") from None

    def transcribe_file(self, mp3: Path, language: str, terms: list[str]) -> dict:
        """{"text", "words": [{"word", "start", "end"}], "segments": [{"start", "end", "text"}]}, in seconds."""
        upload = self._call("POST", "/v2/upload", raw=mp3.read_bytes())
        request = {"audio_url": upload["upload_url"], "language_code": language, "speech_models": [self.MODEL],
                   "punctuate": True, "format_text": True}
        keyterms = [t for t in terms if len(t.split()) <= 6][:1000]     # the service's limits
        if keyterms:
            request["keyterms_prompt"] = keyterms
        job = self._call("POST", "/v2/transcript", request)
        try:
            waited = 0
            while job.get("status") not in ("completed", "error"):
                if waited > self.LONGEST_WAIT:
                    raise TranscriptionError("AssemblyAI ist nach zwei Stunden nicht fertig.")
                time.sleep(self.POLL)
                waited += self.POLL
                job = self._call("GET", f"/v2/transcript/{job['id']}")
            if job["status"] == "error":
                raise TranscriptionError(f"AssemblyAI meldet: {job.get('error', 'unbekannter Fehler')}"[:300])
            sentences = self._call("GET", f"/v2/transcript/{job['id']}/sentences").get("sentences", [])
        finally:
            try:
                self._call("DELETE", f"/v2/transcript/{job['id']}")    # nothing stays with the service
            except (TranscriptionError, OSError):
                pass
        return {"text": job.get("text") or "",
                "words": [{"word": w["text"], "start": w["start"] / 1000, "end": w["end"] / 1000}
                          for w in job.get("words") or []],
                "segments": [{"start": x["start"] / 1000, "end": x["end"] / 1000, "text": x["text"]}
                             for x in sentences]}


def prompt_for(terms: list[str], previous: str) -> str:
    """Technical terms first, then as much of the previous text as fits."""
    head = ("Fachbegriffe: " + ", ".join(terms) + ". ") if terms else ""
    head = head[:PROMPT_LIMIT // 2]
    room = PROMPT_LIMIT - len(head)
    tail = previous[-room:] if room > 0 else ""
    if tail and " " in tail and len(previous) > room:
        tail = tail[tail.index(" ") + 1:]        # start at a word
    return (head + tail).strip()


def invented(result: dict, prompt: str) -> bool:
    """True if a piece looks made up: the prompt repeated, or only the usual phrases."""
    text = " ".join(result["text"].lower().split())
    if not text:
        return False
    if prompt and len(prompt) > 40 and prompt.lower()[-40:] in text:
        return True
    rest = text
    for phrase in INVENTED:
        rest = rest.replace(phrase, "")
    return len(rest.strip(" .,!?-")) < 0.2 * len(text)


def _plain(token: str) -> str:
    return "".join(ch for ch in token.lower() if ch.isalnum())


def punctuate(words: list[dict], text: str) -> list[dict]:
    """whisper-1 returns its timed words without punctuation and capitals at sentence starts;
    the text has them. Walk both in order and take the written form of each matching word."""
    tokens = text.split()
    out, j = [], 0
    for w in words:
        target = _plain(w["word"])
        for k in range(j, min(j + 4, len(tokens))):     # resync over a few tokens at most
            if _plain(tokens[k]) == target and target:
                w = {**w, "word": tokens[k]}
                j = k + 1
                break
        out.append(w)
    return out


def in_pause(word: dict, quiet: list[tuple[float, float]]) -> bool:
    """A word that lies entirely within a pause of at least 0.8 s was not spoken."""
    return any(b - a >= 0.8 and word["start"] >= a - 0.05 and word["end"] <= b + 0.05 for a, b in quiet)


def run(session_dir: Path, transcriber, terms: list[str] | None = None,
        progress: Callable[[int, int], None] | None = None) -> dict:
    """Transcribe the session folder's recording; writes transcript.raw.json and transcript.json."""
    session_dir = Path(session_dir)
    meta = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
    language = meta.get("language") or "de"
    video = session_dir / "recording.mp4"
    if not video.exists():
        video = session_dir / "screen.mp4"
    work = session_dir / "work"
    work.mkdir(exist_ok=True)
    wav = work / "audio.wav"
    if not wav.exists():
        audio.extract(video, wav)
    total = audio.duration(wav)
    quiet = audio.silences(wav)
    run_with = _whole if getattr(transcriber, "whole", False) else _pieces
    words, segments, texts = run_with(transcriber, wav, total, quiet, work, language, terms or [], progress)
    transcript = {"language": language, "model": transcriber.name, "created": dt.datetime.now().astimezone()
                  .isoformat(timespec="seconds"), "duration": round(total, 3), "terms": terms or [],
                  "text": " ".join(t.strip() for t in texts if t.strip()), "words": words, "segments": segments}
    for name in ("transcript.raw.json", "transcript.json"):      # corrected later; until then the same
        (session_dir / name).write_text(json.dumps(transcript, ensure_ascii=False, indent=1), encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    return transcript


def _whole(transcriber, wav, total, quiet, work, language, terms, progress):
    """The whole recording in one request; the service punctuates and finds the sentences itself."""
    if progress:
        progress(0, 1)
    mp3 = work / "recording.mp3"
    audio.export(wav, 0, total, mp3)
    result = transcriber.transcribe_file(mp3, language, terms)
    mp3.unlink(missing_ok=True)
    words = [w for w in ({"start": round(w["start"], 3), "end": round(w["end"], 3), "word": w["word"]}
                         for w in result["words"]) if not in_pause(w, quiet)]
    segments = [{"start": round(s["start"], 3), "end": round(s["end"], 3), "text": s["text"].strip()}
                for s in result["segments"]]
    if progress:
        progress(1, 1)
    return words, segments, [result["text"]]


def _pieces(transcriber, wav, total, quiet, work, language, terms, progress):
    """Pieces cut at pauses, each with the terms and the previous text as prompt (whisper-1)."""
    pieces = audio.plan(total, quiet)
    words, segments, texts = [], [], []
    for i, (start, end) in enumerate(pieces):
        if progress:
            progress(i, len(pieces))
        cache = work / f"piece-{i:03d}.json"
        if cache.exists():
            result = json.loads(cache.read_text(encoding="utf-8"))
        else:
            mp3 = work / f"piece-{i:03d}.mp3"
            audio.export(wav, start, end, mp3)
            prompt = prompt_for(terms, " ".join(texts))
            result = transcriber.transcribe(mp3, language, prompt)
            if invented(result, prompt):
                result = transcriber.transcribe(mp3, language, "")
                if invented(result, ""):
                    result = {"text": "", "words": [], "segments": []}
            cache.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            mp3.unlink(missing_ok=True)
        texts.append(result["text"])
        for w in punctuate(result["words"], result["text"]):
            word = {"start": round(w["start"] + start, 3), "end": round(w["end"] + start, 3), "word": w["word"]}
            if not in_pause(word, quiet):
                words.append(word)
        segments += [{"start": round(s["start"] + start, 3), "end": round(s["end"] + start, 3),
                      "text": s["text"].strip()} for s in result["segments"]]
    if progress:
        progress(len(pieces), len(pieces))
    return words, segments, texts
