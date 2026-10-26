# FLEX3 Light server

Receives recordings from FLEX3 Light piece by piece while the lecture is still running, and turns each completed recording into a session folder after the FLEX3 contract (`knowledge-base/architecture/session-folder.md` in `flex3-hsos/flex3`):

| File | Content |
|---|---|
| `recording.mp4` | program mix, 1920×1080, H.264 and AAC 48 kHz |
| `screen.mp4`, `camera.mp4` | the separate tracks, listed under `tracks` in `session.json` |
| `session.json` | metadata; `device_id` is set by the server from the token |

## API

Every request carries `Authorization: Bearer <token>`. The token decides the `device_id`, and a session belongs to the device that opened it.

| Request | Purpose |
|---|---|
| `PUT /sessions/{id}` | open a session; repeating it does no harm |
| `GET /sessions/{id}` | state (`open`, `assembling`, `complete`) and the parts received so far |
| `PUT /sessions/{id}/parts/{track}/{n}` | part `n` of `mix`, `screen` or `camera`; body is the MP4, `X-Content-SHA256` its hash. A part sent twice replaces itself |
| `POST /sessions/{id}/complete` | `{"session": {...session.json...}, "parts": {"mix": n, "screen": n, "camera": n}}`; answers 202 and joins the parts in the background |
| `DELETE /sessions/{id}` | throws away a session that is not complete, for "Verwerfen" in the app |

Joining copies the streams without re-encoding (ffmpeg concat demuxer, `-c copy`) and checks that every track is as long as its parts. The session folder appears under `sessions/` only when it is complete; until then everything stays under `incoming/`. If joining fails, the session goes back to `open` with an `error`, and the client can complete it again.

## Running it

Requirements: Python 3.10 or later and ffmpeg on the `PATH`.

```bash
pip install -r server/requirements.txt
```

The tokens live in a JSON file that maps each token to a device id, for example `{"<long random token>": "flex3-light-pc1234"}`. It is never logged and does not belong in the repository.

```bash
FLEX3_LIGHT_SERVER_DATA=/srv/flex3-light FLEX3_LIGHT_SERVER_TOKENS=/etc/flex3-light/tokens.json uvicorn --app-dir server flex3_light_server.app:app_from_env --factory --host 0.0.0.0 --port 8000
```

In production it runs behind a reverse proxy with TLS. The proxy has to allow request bodies of at least 100 MB: in a test on 2026-10-26 one minute came to about 57 MB for the camera track (webcam noise at the inherited quality setting), 9 MB for the program mix and 5 MB for the screen.

## Tests

```bash
pip install -r requirements-dev.txt
```

```bash
python -m pytest tests
```

`tests/test_server.py` covers the API, `tests/test_delivery.py` the whole way from synthetic OBS segments through the app's background processing to a running server.
