"""HTTP API that receives a recording piece by piece while the lecture is still running.

    PUT    /sessions/{id}                      open a session (idempotent)
    GET    /sessions/{id}                      state and the parts received so far
    PUT    /sessions/{id}/parts/{track}/{n}    one part of a track, body is the MP4, X-Content-SHA256 its hash
    POST   /sessions/{id}/complete             session.json and the number of parts per track
    DELETE /sessions/{id}                      throw away a session that is not complete

Every request carries "Authorization: Bearer <token>"; the token decides the device_id.
On completion the parts of each track are joined without re-encoding into a session
folder after the FLEX3 contract: recording.mp4 (program mix), screen.mp4, camera.mp4 and
session.json. Nothing is confirmed before the folder is complete.

Configuration from the environment:
    FLEX3_LIGHT_SERVER_DATA     data folder (incoming/ and sessions/ below it)
    FLEX3_LIGHT_SERVER_TOKENS   JSON file {"<token>": "<device_id>", ...}
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import threading
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .assemble import join

logger = logging.getLogger(__name__)

SESSION_ID = re.compile(r"^\d{4}-\d{2}-\d{2}_[a-z0-9-]{1,40}_[a-z0-9-]{1,40}$")
# Track names in the API and their file names in the session folder.
TRACK_FILES = {"mix": "recording.mp4", "screen": "screen.mp4", "camera": "camera.mp4"}
TRACK_KINDS = {"screen": "screen", "camera": "camera"}
REQUIRED = ("session_id", "status", "recorded_at", "duration_sec", "language", "consent", "consent_source",
            "software_version")
MAX_PARTS = 10000


def create_app(data_dir: Path, tokens: dict[str, str]) -> FastAPI:
    data_dir = Path(data_dir)
    incoming, sessions = data_dir / "incoming", data_dir / "sessions"
    incoming.mkdir(parents=True, exist_ok=True)
    sessions.mkdir(parents=True, exist_ok=True)
    locks: dict[str, threading.Lock] = {}
    locks_guard = threading.Lock()
    app = FastAPI(title="FLEX3 Light server")

    def lock(sid: str) -> threading.Lock:
        with locks_guard:
            return locks.setdefault(sid, threading.Lock())

    def device(request: Request) -> str:
        """The device_id that belongs to the bearer token; compared in constant time."""
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        for token, device_id in tokens.items():
            if given and hmac.compare_digest(given.encode(), token.encode()):
                return device_id
        raise HTTPException(401, "unknown token")

    def folder(sid: str) -> Path:
        if not SESSION_ID.match(sid):
            raise HTTPException(400, "invalid session id")
        return incoming / sid

    def read_state(sid: str) -> dict | None:
        try:
            return json.loads((incoming / sid / "state.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"state": "complete"} if (sessions / sid / "session.json").exists() else None

    def write_state(sid: str, state: dict) -> None:
        tmp = incoming / sid / "state.json.tmp"
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        os.replace(tmp, incoming / sid / "state.json")

    def owned(sid: str, device_id: str) -> dict:
        state = read_state(sid)
        if state is None:
            raise HTTPException(404, "unknown session")
        if state.get("device_id", device_id) != device_id:
            raise HTTPException(409, "session belongs to another device")
        return state

    def parts(sid: str) -> dict[str, list[int]]:
        found = {}
        for track in TRACK_FILES:
            d = incoming / sid / "parts" / track
            found[track] = sorted(int(p.stem) for p in d.glob("*.mp4")) if d.exists() else []
        return found

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.put("/sessions/{sid}")
    def open_session(sid: str, device_id: str = Depends(device)):
        d = folder(sid)
        with lock(sid):
            state = read_state(sid)
            if state is None:
                (d / "parts").mkdir(parents=True, exist_ok=True)
                state = {"device_id": device_id, "state": "open"}
                write_state(sid, state)
                logger.info("Opened session %s for %s", sid, device_id)
            elif state.get("device_id", device_id) != device_id:
                raise HTTPException(409, "session belongs to another device")
        return {"state": state["state"]}

    @app.get("/sessions/{sid}")
    def get_session(sid: str, device_id: str = Depends(device)):
        folder(sid)
        state = owned(sid, device_id)
        result = {"state": state["state"], "parts": parts(sid) if state["state"] != "complete" else {}}
        if state.get("error"):
            result["error"] = state["error"]
        return result

    @app.put("/sessions/{sid}/parts/{track}/{index}")
    async def put_part(sid: str, track: str, index: int, request: Request, device_id: str = Depends(device)):
        d = folder(sid)
        if track not in TRACK_FILES or not 0 <= index < MAX_PARTS:
            raise HTTPException(400, "invalid track or index")
        expected = request.headers.get("x-content-sha256", "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise HTTPException(400, "X-Content-SHA256 missing")
        if owned(sid, device_id)["state"] != "open":
            raise HTTPException(409, "session is no longer open")
        target = d / "parts" / track / f"{index:04d}.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.{threading.get_ident()}.tmp")
        digest = hashlib.sha256()
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                digest.update(chunk)
                f.write(chunk)
        if digest.hexdigest() != expected:
            tmp.unlink(missing_ok=True)
            raise HTTPException(400, "checksum mismatch")
        os.replace(tmp, target)          # sending the same part twice just replaces it
        return {"received": index}

    @app.post("/sessions/{sid}/complete")
    def complete(sid: str, body: dict, device_id: str = Depends(device)):
        folder(sid)
        with lock(sid):
            state = owned(sid, device_id)
            if state["state"] in ("assembling", "complete"):
                return JSONResponse({"state": state["state"]}, status_code=202 if state["state"] == "assembling" else 200)
            session, counts = body.get("session") or {}, body.get("parts") or {}
            missing = [k for k in REQUIRED if k not in session]
            if missing:
                raise HTTPException(422, f"session.json lacks {', '.join(missing)}")
            if session["session_id"] != sid:
                raise HTTPException(422, "session_id does not match")
            if not counts.get("mix") or any(t not in TRACK_FILES or not isinstance(n, int) or n < 0
                                            for t, n in counts.items()):
                raise HTTPException(422, "parts must name a count per track, mix included")
            have = parts(sid)
            gaps = {t: sorted(set(range(n)) - set(have[t])) for t, n in counts.items() if n}
            gaps = {t: g for t, g in gaps.items() if g}
            if gaps:
                raise HTTPException(409, {"missing": gaps})
            state.update(state="assembling", error=None)
            write_state(sid, state)
        threading.Thread(target=assemble, args=(sid, session, counts, device_id), daemon=True).start()
        return JSONResponse({"state": "assembling"}, status_code=202)

    def assemble(sid: str, session: dict, counts: dict, device_id: str) -> None:
        d = incoming / sid
        out = d / "out"
        try:
            shutil.rmtree(out, ignore_errors=True)
            out.mkdir()
            tracks = []
            for track, n in counts.items():
                if not n:
                    continue
                join([d / "parts" / track / f"{i:04d}.mp4" for i in range(n)], out / TRACK_FILES[track])
                if track in TRACK_KINDS:
                    tracks.append({"file": TRACK_FILES[track], "kind": TRACK_KINDS[track]})
            session = {**session, "device_id": device_id}
            if tracks:
                session["tracks"] = tracks
            (out / "session.json").write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(out, sessions / sid)       # the session folder appears complete or not at all
            shutil.rmtree(d)
            logger.info("Session %s complete", sid)
        except Exception as e:
            logger.error("Assembling session %s failed: %s", sid, e)
            with lock(sid):
                write_state(sid, {"device_id": device_id, "state": "open", "error": str(e)})

    @app.delete("/sessions/{sid}")
    def delete(sid: str, device_id: str = Depends(device)):
        d = folder(sid)
        with lock(sid):
            state = read_state(sid)
            if state is None:
                return {"state": "deleted"}
            if state.get("device_id", device_id) != device_id:
                raise HTTPException(409, "session belongs to another device")
            if state["state"] in ("assembling", "complete"):
                raise HTTPException(409, "session is already complete")
            shutil.rmtree(d)
            logger.info("Deleted session %s", sid)
        return {"state": "deleted"}

    return app


def app_from_env() -> FastAPI:
    data = Path(os.environ.get("FLEX3_LIGHT_SERVER_DATA", "data"))
    tokens = json.loads(Path(os.environ["FLEX3_LIGHT_SERVER_TOKENS"]).read_text(encoding="utf-8"))
    return create_app(data, tokens)
