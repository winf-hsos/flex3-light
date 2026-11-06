# FLEX3 Light server

Receives recordings from FLEX3 Light piece by piece while the lecture is still running, and turns each completed recording into a session folder after the FLEX3 contract (`knowledge-base/architecture/session-folder.md` in `flex3-hsos/flex3`):

| File | Content |
|---|---|
| `recording.mp4` | program mix, 1920×1080, H.264 and AAC 48 kHz |
| `screen.mp4`, `camera.mp4` | the separate tracks, listed under `tracks` in `session.json` |
| `session.json` | metadata; `device_id` is set by the server from the token |

## API

Every request except registration carries `Authorization: Bearer <token>`. The token decides the `device_id` and the lecturer (one token per person and device), and a session belongs to the device that opened it. On completion the server sets `device_id`, `instructor_id` and `instructor_name` in `session.json` from the token, whatever the device sent.

| Request | Purpose |
|---|---|
| `PUT /sessions/{id}` | open a session; repeating it does no harm |
| `GET /sessions/{id}` | state (`open`, `assembling`, `complete`) and the parts received so far |
| `PUT /sessions/{id}/parts/{track}/{n}` | part `n` of `mix`, `screen` or `camera`; body is the MP4, `X-Content-SHA256` its hash. A part sent twice replaces itself |
| `POST /sessions/{id}/complete` | `{"session": {...session.json...}, "parts": {"mix": n, "screen": n, "camera": n}}`; answers 202 and joins the parts in the background |
| `DELETE /sessions/{id}` | throws away a session that is not complete, for "Verwerfen" in the app |
| `GET /profile` | the lecturer's profile: `user_id`, `user_name` and the modules |
| `POST /profile/sync` | `{"modules": [...]}`: merges the device's entries and returns all of them |
| `POST /devices/register` | `{"key", "device_name"}`, no token: exchanges an invitation key for the device token |
| `GET /me` | the device and lecturer of the token |

### Lecturers, keys and devices

Since 2026-11-06 a device connects itself (`accounts.py`). Whoever runs the server creates an invitation for a lecturer; it prints a **connection code**, the key and the server's address in one line (`FLX3-7K3M-Q9PD-2WRT-H4NA@https://…`), valid once and for 14 days. The lecturer pastes it in FLEX3 Light under Einstellungen, Server; the app exchanges it for a device token, which it keeps in the Windows Credential Manager. One token per person and device: registering the same computer again keeps its `device_id` and invalidates the old token, and a lost laptop is revoked on the server. Keys and tokens are stored only as SHA-256 hashes in `accounts.sqlite` in the data folder.

```bash
python -m flex3_light_server.admin --data /srv/flex3-light --url https://flex3.example.org invite "Jane Doe"
```

`users` and `devices` list what exists, `revoke <device_id>` blocks a device. The lecturer's id is also the key of their profile, so the profile is the user register of the FLEX3 IT concept (decision of 2026-11-06).

### Profiles

A profile holds the modules a lecturer records, so that every device of that person offers the same list. Each module is an entry with `id`, `name` (also the folder in the local archive), `code` (used in the session id), `language`, optionally `ilias_course_id` and `opencast_series_id`, plus `updated_at` and `deleted`. Devices work offline on a local copy and send all their entries when they reach the server; the merge goes entry by entry, the newer change wins, and a deleted entry stays as a tombstone so that an old copy on another device cannot bring it back. The app (`lecturer_profile.py`) merges with the same rule (`profiles.py`), checked by a test. A module with both ILIAS course and Opencast series makes its sessions arrive as `assigned`.

Joining copies the streams without re-encoding (ffmpeg concat demuxer, `-c copy`) and checks that every track is as long as its parts. The session folder appears under `sessions/` only when it is complete; until then everything stays under `incoming/`. If joining fails, the session goes back to `open` with an `error`, and the client can complete it again.

## Running it

Requirements: Python 3.10 or later and ffmpeg on the `PATH`.

```bash
pip install -r server/requirements.txt
```

Tests and setups from before 2026-11-06 can still name a JSON file of fixed tokens in `FLEX3_LIGHT_SERVER_TOKENS` (`{"<token>": {"device_id", "user_id", "user_name"}}`, a plain string being a device without a lecturer); it is never logged and does not belong in the repository.

To try it on your own computer, `python tools/dev_server.py` runs the server on `127.0.0.1:8765` with its data in `%LOCALAPPDATA%\flex3-light-devserver`, and `python tools/dev_server.py --invite` prints a connection code for you to paste into the app. Stop the server to see the app work offline; "Trennen" in the app goes back to recording locally only.

```bash
FLEX3_LIGHT_SERVER_DATA=/srv/flex3-light uvicorn --app-dir server flex3_light_server.app:app_from_env --factory --host 0.0.0.0 --port 8000
```

In production it runs behind a reverse proxy with TLS. The proxy has to allow request bodies of at least 100 MB: in a test on 2026-10-26 one minute came to about 57 MB for the camera track (webcam noise at the inherited quality setting), 9 MB for the program mix and 5 MB for the screen.

## Tests

```bash
pip install -r requirements-dev.txt
```

```bash
python -m pytest tests
```

`tests/test_server.py` covers the API, `tests/test_accounts.py` keys, registration and revoked devices, `tests/test_delivery.py` the whole way from synthetic OBS segments through the app's background processing to a running server, and `tests/test_profile.py` the profile: seeding, offline changes, two devices of one lecturer, and the same merge rule in app and server.
