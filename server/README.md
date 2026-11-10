# FLEX3 Light server

Receives recordings from FLEX3 Light piece by piece while the lecture is still running, turns each completed recording into a session folder after the FLEX3 contract (`knowledge-base/architecture/session-folder.md` in `flex3-hsos/flex3`), and runs the pipeline on it: transcription, cut proposal, rendering after approval, summary (since 2026-11-09, see "Pipeline" below).

| File | Content |
|---|---|
| `recording.mp4` | program mix, 1920×1080, H.264 and AAC 48 kHz |
| `screen.mp4`, `camera.mp4` | the separate tracks, listed under `tracks` in `session.json` |
| `session.json` | metadata; `device_id` is set by the server from the token |
| `activity.json` | programs in front and privacy-key spans; instead of a window title only the word of the lecturer's title list it contained |

## API

Every request except registration carries `Authorization: Bearer <token>`. The token decides the `device_id` and the lecturer (one token per person and device), and a session belongs to the device that opened it. On completion the server sets `device_id`, `instructor_id` and `instructor_name` in `session.json` from the token, whatever the device sent.

| Request | Purpose |
|---|---|
| `PUT /sessions/{id}` | open a session; repeating it does no harm |
| `GET /sessions/{id}` | state (`open`, `assembling`, `complete`) and the parts received so far |
| `PUT /sessions/{id}/parts/{track}/{n}` | part `n` of `mix`, `screen` or `camera`; body is the MP4, `X-Content-SHA256` its hash. A part sent twice replaces itself |
| `POST /sessions/{id}/complete` | `{"session": {...session.json...}, "parts": {"mix": n, "screen": n, "camera": n}, "activity": {...}}`; answers 202 and joins the parts in the background |
| `GET /sessions/{id}/pipeline` | where the pipeline stands for a recording of this lecturer: `steps`, a `status` line, `queued`, `active`, `failed` |
| `POST /sessions/{id}/pipeline` | `{"action": ...}`: `transcribe`, `retranscribe`, `cut` (propose again), `approve` (the edit list, then it is rendered), `render`, `summary`; 409 with the reason when something stands in the way |
| `DELETE /sessions/{id}` | throws away a session that is not complete, for "Verwerfen" in the app |
| `GET /profile` | the lecturer's profile: `user_id`, `user_name`, the modules and the preferences |
| `POST /profile/sync` | `{"modules": [...], "preferences": {...}}`: merges the device's entries and returns all of them |
| `POST /devices/register` | `{"key", "device_name"}`, no token: exchanges an invitation key for the device token |
| `GET /me` | the device and lecturer of the token |
| `GET /library/` | the page "Meine Aufnahmen": the app's `ui.html` in its server mode, no token needed |
| `POST /library/ticket` | a one-time ticket (two minutes) that opens the page as the lecturer of the token |
| `POST /library/session` | `{"ticket"}`: a page session of twelve hours, no token needed |
| `POST /library/api/{call}` | `[arguments]`: an action of the page, by the names the app uses (`my_recordings.CALLS`), for the page session's lecturer and their recordings only |
| `GET /library/media/{id}/{file}` | a video, its subtitles, the thumbnail or the summary of one's own recording, with range requests; the page session as `?s=` |
| `GET /library/package/{id}` | the zip file of a published recording, with the names Opencast uploads get |

### Lecturers, keys and devices

Since 2026-11-06 a device connects itself (`accounts.py`). Whoever runs the server creates an invitation for a lecturer; it prints a **connection code**, the key and the server's address in one line (`FLX3-7K3M-Q9PD-2WRT-H4NA@https://…`), valid once and for 14 days. The lecturer pastes it in FLEX3 Light under Einstellungen, Server; the app exchanges it for a device token, which it keeps in the Windows Credential Manager. One token per person and device: registering the same computer again keeps its `device_id` and invalidates the old token, and a lost laptop is revoked on the server. Keys and tokens are stored only as SHA-256 hashes in `accounts.sqlite` in the data folder.

```bash
python -m flex3_light_server.admin --data /srv/flex3-light --url https://flex3.example.org invite "Jane Doe"
```

`users` and `devices` list what exists, `revoke <device_id>` blocks a device. The lecturer's id is also the key of their profile, so the profile is the user register of the FLEX3 IT concept (decision of 2026-11-06).

### Profiles

A profile holds the modules a lecturer records, so that every device of that person offers the same list. Each module is an entry with `id`, `name` (also the folder in the local archive), `code` (used in the session id), `language`, optionally `ilias_course_id` and `opencast_series_id`, plus `updated_at` and `deleted`. Devices work offline on a local copy and send all their entries when they reach the server; the merge goes entry by entry, the newer change wins, and a deleted entry stays as a tombstone so that an old copy on another device cannot bring it back. The app (`lecturer_profile.py`) merges with the same rule (`profiles.py`), checked by a test. A module with both ILIAS course and Opencast series makes its sessions arrive as `assigned`.

Since 2026-11-09 the profile also holds the lecturer's **preferences**, everything that belongs to the person and not to a computer: the cut settings (`cut.pause_min`, `cut.cut_programs` and so on), `after_recording` and the default `language`. Each is a key with `{"value", "updated_at"}` and is merged on its own with the same rule, so two devices that changed different settings offline both keep their change. Keys are lower case with at most one dot, a value is any JSON up to 8 KB, at most 200 keys; the server stores them without checking their meaning, and its pipeline reads the cut settings, `after_recording` and `pipeline` from here. An app from before that date sends no preferences and keeps working.

### Pipeline

Since 2026-11-09 the server runs the same pipeline as the app (`worker.py` drives `pipeline_runner.py` and the steps in `pipeline/` at the repository root, which is why the server needs the whole repository, not only `server/`). It works on one recording after the other and keeps the state of each step in `pipeline.json` in the session folder, as the app does. What a recording gets comes from the profile of whoever recorded it: the technical terms of its module, the cut settings, and whether anything starts by itself. That happens when "Nach der Aufnahme" is not "nur speichern"; since phase 5 the app has no pipeline of its own, so a lecture is never worked on, and paid for, twice (the preference `pipeline` of the time before is ignored). Everything else is started with `POST /sessions/{id}/pipeline`, which only the lecturer who recorded it may call.

The keys for the AI services are the project's, in a JSON file named by `FLEX3_LIGHT_SERVER_SECRETS` (`{"openai": "...", "assemblyai": "...", "elevenlabs": "..."}`), never logged and never in the repository; without them the steps wait, as in the app. Which language model a step uses is set in `models.json` in the data folder, one entry per stage, e.g. `{"cut": {"adapter": "openai-responses", "model": "gpt-6.1-sol", "effort": "high"}}` or a local server that speaks the chat completions API (`"adapter": "openai-chat", "base_url": "http://127.0.0.1:8000/v1"`); it is read anew for every step, and without it the defaults of `pipeline/llm.py` apply. Rendering uses NVENC when ffmpeg can open it (an NVIDIA card) and x264 otherwise, which works everywhere but takes a multiple of the time. Typst sets the summary when it is on the `PATH`. The text recognition the cut agent uses is Windows' own; on another system the agent is told it is missing and does without.

The activity log arrives without window titles (decision of 2026-11-06): the app matches each title against the lecturer's title list (`cut.cut_titles`, by default Posteingang, Inbox, Gmail, WhatsApp Web, Outlook) and sends only the word it found, so the rule "propose to cut such windows" and the agent work as before, and the titles stay on the lecturer's computer. The server refuses entries that look like titles.

Recordings filed before the server existed are taken over with `python -m flex3_light_server.admin import <folder> --user <id>`: the files of the recording and the activity log without titles, not the results of a local pipeline.

### Meine Aufnahmen

Since phase 4 (2026-11-09) the review runs on the server: the server serves the app's own `ui.html` at `/library/`, which recognises that it runs there (`window.FLEX3_SERVER`) and shows only "Meine Aufnahmen", with every action sent to `/library/api/<call>` instead of the app. One file, so the page in the app and the page on the server never drift apart. The actions themselves are shared too: `recordings.py` at the repository root holds them, and `my_recordings.py` applies them to the server's session folders of whoever is signed in (since phase 5 the app has no library of its own), with the server's pipeline and the settings of their profile; a program rule or a term added on the page lands in the profile and reaches the app with its next sync.

Nobody types a password: the app asks for a one-time ticket with its device token and opens the page with it, in a frame of its window or in the default browser ("Im Browser öffnen"). The page trades the ticket for a page session of twelve hours, kept in memory (a restarted server only means a new ticket); a `<video>` sends no header, so media addresses carry the page session, never the device token. Deleting moves the session folder into `trash/` below the data folder, which an administrator empties.

Publishing moves nothing on the server. It packs the long version, the clips, their subtitles and the summary into a zip file (`packages/`) with the names Opencast uploads get; in the app's frame FLEX3 Light fetches it and unpacks it into its folder for uploads under `<term>\<module>`, as publishing on the computer did, while a browser downloads the zip file. Until FLEX3 talks to Opencast, the upload stays by hand.

Joining copies the streams without re-encoding (ffmpeg concat demuxer, `-c copy`) and checks that every track is as long as its parts. The session folder appears under `sessions/` only when it is complete; until then everything stays under `incoming/`. If joining fails, the session goes back to `open` with an `error`, and the client can complete it again.

## Running it

Requirements: Python 3.10 or later, ffmpeg on the `PATH`, Typst for the summary, and the whole repository (the pipeline lives at its root).

```bash
pip install -r server/requirements.txt
```

Tests and setups from before 2026-11-06 can still name a JSON file of fixed tokens in `FLEX3_LIGHT_SERVER_TOKENS` (`{"<token>": {"device_id", "user_id", "user_name"}}`, a plain string being a device without a lecturer); it is never logged and does not belong in the repository.

To try it on your own computer, `python tools/dev_server.py` runs the server on `127.0.0.1:8765` with its data in `%LOCALAPPDATA%\flex3-light-devserver`, and `python tools/dev_server.py --invite` prints a connection code for you to paste into the app. Its pipeline uses the keys FLEX3 Light keeps in this computer's Credential Manager (`--no-keys` leaves them out). Stop the server to see the app work offline; "Trennen" in the app goes back to recording locally only. Since phase 5 the app starts this server by itself when it is connected to `127.0.0.1` and nothing answers there; `--idle-exit 30` then ends it after thirty idle minutes once its pipeline has nothing left to do.

```bash
FLEX3_LIGHT_SERVER_DATA=/srv/flex3-light FLEX3_LIGHT_SERVER_SECRETS=/etc/flex3-light/secrets.json uvicorn --app-dir server flex3_light_server.app:app_from_env --factory --host 0.0.0.0 --port 8000
```

In production it runs behind a reverse proxy with TLS. The proxy has to allow request bodies of at least 100 MB: in a test on 2026-10-26 one minute came to about 57 MB for the camera track (webcam noise at the inherited quality setting), 9 MB for the program mix and 5 MB for the screen.

## Tests

```bash
pip install -r requirements-dev.txt
```

```bash
python -m pytest tests
```

`tests/test_server.py` covers the API, `tests/test_accounts.py` keys, registration and revoked devices, `tests/test_server_pipeline.py` the activity without titles, the pipeline on the server for its lecturer and the import, `tests/test_server_library.py` the page: ticket and session, one's own recordings only, ranges, approval, the package and how the app unpacks it, `tests/test_delivery.py` the whole way from synthetic OBS segments through the app's background processing to a running server, and `tests/test_profile.py` the profile: seeding, offline changes, two devices of one lecturer, preferences key by key, the move of old settings into the profile, and the same merge rule in app and server.
