# Delivery to the FLEX3 server and live streaming: plan

Status: proposal of 2026-10-26, nothing of it is built yet. Decisions that only Nicolas can make are marked **Decision**.

FLEX3 Light takes over the jobs of the FLEX3 device on the lecturer's laptop. For the server it has to look like a device: it delivers one session folder per recording to the ingest endpoint and sends one live stream. This document plans both, based on the FLEX3 repository (`flex3-hsos/flex3`): `knowledge-base/architecture/session-folder.md` (the contract), `post-pipeline.md`, `live-pipeline.md`, `design-principles.md` and the work packages `device`, `ingest-delivery` and `livestream`. Open points of the contract were checked with the FLEX3 project on 2026-10-26. On the same day Nicolas decided that FLEX3 Light is an alternative to the device, not its replacement (IT concept §4.2, decision 30), and that a session folder may carry extra tracks (decision 31). The remaining open points are listed in §4.2 of the IT concept and below.

## What FLEX3 Light records today

| | today (inherited from the lecture recorder) | the device / the contract |
|---|---|---|
| Picture | OBS canvas 3840×1200: screen left, webcam right | ATEM program mix, 1080p |
| Result | two MP4 per session, `… - Bildschirm.mp4` and `… - Kamera.mp4`, filed by term and module | one folder per session with `recording.mp4` and `session.json` |
| Codec | H.264 (NVENC or x264), AAC 160 kbit/s, 48 kHz stereo | H.264, AAC 48 kHz |
| Frame rate | 30 fps | not fixed by the contract; the live simulator has to match the device (25 or 50) |
| Pauses | OBS leaves them out of the file | not fixed |
| Upload | none | store and forward to the ingest endpoint with a device token |
| Live | none | one stream to the server, RTMP (TCP 1935) or SRT (UDP 8890) into MediaMTX, campus IP ranges only |

## Part 1: the session folder

How the program mix comes about depends on the canvas decision in [virtual-camera.md](virtual-camera.md). With a 1920×1080 mix canvas (layout B there) OBS records the mix directly. With today's wide canvas (layout A) FLEX3 Light makes it after stopping: it already reads the raw recording once to split it, and the same ffmpeg run can write a third output, so no second pass over the file is needed. Either way the folder holds:

- `recording.mp4`: 1920×1080, 30 fps, H.264 and AAC 48 kHz. The screen fills the picture, the webcam sits small in a corner (picture in picture); without a camera only the screen. The Alt+Tab freeze applies to the mix as well.
- `session.json` with the contract's fields. FLEX3 Light knows the module (course name), the date and start time, the net duration, the language (to be added to the window) and `software_version`. It does not know `ilias_course_id`, `opencast_series_id` or the consent from the user register, so it delivers `status: "unassigned"`, and the server's queue for unassigned sessions fills in the rest. That is how the contract already handles a device session without assignment.
- `screen.mp4` and `camera.mp4`, the separate tracks, whenever they exist. Nicolas allowed extra tracks on 2026-10-26 (FLEX3 IT concept §4, decision 31): they start at the same instant as `recording.mp4`, carry plain English names and are listed in `session.json` under `"tracks": [{"file": "camera.mp4", "kind": "camera"}, {"file": "screen.mp4", "kind": "screen"}]`. The post-pipeline keeps working on `recording.mp4`, and the ingest takes the other tracks along unchanged. The split already guarantees the common start, so FLEX3 Light only has to name and list them. The local archive by term and module stays as it is.

Folders wait in a local outbox (`%USERPROFILE%\Videos\flex3-light-outbox\<session_id>\`) until the upload is confirmed. The session id follows the contract's pattern `<date>_<course code>_<session>`.

**Decision:** layout of the program mix (picture in picture, size and corner of the camera, or side by side). The contract leaves it open.

**Decision:** `consent_source`. The contract knows `register` and `restricted_on_device`, and neither fits a laptop without a register. Either FLEX3 Light always delivers `unassigned` and the server takes consent from the register when it assigns the session, or the contract gets a new value. The second is a contract change.

**Decision:** whether `duration_sec` is the net length (without pauses, matching the file) or the gross length. The net length is proposed, because the post-pipeline works on the file.

## Part 2: delivery during the lecture

Nicolas decided on 2026-10-26 that the server receiving the recordings is developed in this repository as well (`server/`).

**Built on 2026-10-26:** `delivery.py`, `server_client.py` and `server/`, with tests in `tests/`. A real recording of 2:30 with OBS, a 10-second pause and an Alt+Tab was filed and on the server 33 seconds after the stop, with all three tracks 140.9 seconds long; the first minute was processed and uploaded 20 seconds after it ended, while OBS kept recording. The sections below describe the design as built.

A lecture of 180 minutes must not end with minutes of converting and uploading after the stop button. FLEX3 Light therefore processes and delivers the recording **piece by piece while it is running**, so that only the last minute is left when the lecturer stops.

### On the laptop

1. **OBS writes segments.** The advanced recording output can split the file automatically by time (`RecSplitFile`, `RecSplitFileType`, `RecSplitFileTime` in minutes; the profile parameters exist on the development machine, default 15 minutes), and obs-websocket offers `SplitRecordFile` for a split on demand. Each segment is a complete file that starts on a keyframe. Proposed length: one to two minutes.
2. **A worker processes each closed segment.** As soon as OBS starts the next file, the previous one is done, and a background worker runs the same ffmpeg step as today on it: crop the screen and camera tracks, compose the program mix, freeze the Alt+Tab spans that fall into it. On a machine with NVENC this takes a fraction of the segment's length. The worker runs at low priority and never competes with OBS: if it falls behind, for example on a laptop without NVENC, it catches up after the stop instead of risking dropped frames in the recording.
3. **An uploader sends each processed segment.** Uploads are resumable and idempotent (a segment has a number and a checksum, sending it twice does no harm), and their bandwidth is capped so that a parallel Teams call or stream keeps priority.
4. **A manifest per session** in the outbox records the state of each segment (recorded, processed, uploaded, confirmed). After a crash or restart FLEX3 Light continues where it stopped.

At the stop only the last segment is left: OBS closes it, the worker processes it, the uploader sends it, and FLEX3 Light reports the session as complete.

### On the server

- `PUT /sessions/{id}` opens a session under an id that FLEX3 Light chooses (`<date>_<module>_<HHMM>`). `PUT /sessions/{id}/parts/{track}/{n}` receives part `n` of a track with a checksum and confirms it. `POST /sessions/{id}/complete` sends `session.json` with the number of parts per track. Details in `server/README.md`.
- On completion the server joins the segments of each track without re-encoding (ffmpeg concat with `-c copy`), checks that every track is as long as the sum of its segments, writes the session folder according to the contract and only then confirms the session.
- `DELETE /sessions/{id}` throws a session away. This matters because segments reach the server before the lecturer decides whether to keep the recording: "Verwerfen" in the window has to delete them there as well.
- Every request carries the device's token; the app gets it by registering with a connection code from the server and keeps it in the Windows Credential Manager, never printed or logged, like the OBS password.

### Rules that stay

The FLEX3 design principles apply unchanged, because the asymmetry is the same on a laptop: a failed upload can be repeated, a lost recording cannot. The recording never depends on the network, and nothing local is deleted before the server has confirmed the complete session. The local archive by term and module is written as today.

### Test of 2026-10-26

A test recording of 3:47 on the development machine (OBS 32.2.2, wide canvas, split every minute, a 12-second pause after 90 seconds, a 440 Hz sine tone instead of the microphone so that any break in the sound is measurable) gave four segments. The recording was deleted afterwards.

- **The pause does not count.** OBS splits by recorded time, so the segment that contains the pause holds 60 seconds of content like the others. The joined result was 214.9 seconds long, the recording time minus the pause.
- **Nothing is lost at the boundaries.** The video frames of the segments add up to the frames of the joined file (6445), and so do the audio samples. The tone runs on without a click across all three boundaries.
- **The planned way is seamless.** Each segment split into screen and camera with `split.py` and the screen parts joined with `-c copy`: the audio timestamps have no gap at all, and the only trace in the picture is one frame shown 70 ms longer at one boundary, which nobody sees.
- **Joining the raw MKV segments directly** leaves a 21 ms gap in the audio timestamps at each boundary, one AAC frame, because the container rounds its duration. The samples are still complete, but the planned way avoids this anyway.
- **`StopRecord` reports the path of the first segment**, not the last one. FLEX3 Light therefore has to follow the segments through the `RecordFileChanged` event.
- **Processing time:** splitting a one-minute segment took 13.6 seconds with NVENC and 9.6 seconds with x264 on this machine, each measured with OBS idle. Both are far below the segment length; a weaker laptop without NVENC still has to be measured while OBS records.
- **Upload volume:** about 57 MB per minute for the camera track, 9 MB for the program mix and 5 MB for the screen, so roughly 9 Mbit/s in total. The camera track dominates because the inherited quality setting (constant quality 21) spends most bits on webcam noise; a lower quality for the camera would cut the upload by more than half.

**Decided (2026-11-06):** one token per person and device. The lecturer pastes a connection code (invitation key and server address) into the app, which registers itself and gets the token and a `device_id` of the form `flex3-light-<computer name>` (`server/flex3_light_server/accounts.py`).

## Part 3: live streaming

OBS can stream on its own, and FLEX3 Light already remote-controls it: `SetStreamServiceSettings` sets a custom RTMP server and key, `StartStream` and `StopStream` run it. The live part therefore needs little code. The open question is which picture gets streamed, because OBS streams its canvas, and today's canvas is the 3840×1200 strip with screen and webcam side by side. This is the same question as for the virtual camera, see [virtual-camera.md](virtual-camera.md), and both should be solved together.

Targets in the order of the FLEX3 work package `livestream`:

1. **Teams or Zoom**, with FLEX3 Light as the camera (see the virtual camera document). This is how hybrid lectures run today and needs no server.
2. **YouTube** for public lectures: RTMP with the lecturer's stream key. No server either.
3. **The FLEX3 server**, once it runs: RTMP or SRT into MediaMTX, from campus networks only, so not from home office.

Recording and streaming are independent outputs in OBS. A failed stream must never stop the recording; FLEX3 Light shows the stream state in the bar and retries, while the recording runs on.

**Decision:** which live targets FLEX3 Light should offer in its first version (proposed: Teams/Zoom as camera first, YouTube second, the FLEX3 server once its ingest exists).

**Decision:** whether the FLEX3 server accepts 30 fps on the live path. The live simulator table asks for the device's frame rate (25 or 50), the post-pipeline does not.

## Order of work

1. Rename to FLEX3 Light, so it runs next to the lecture recorder (done on 2026-10-26).
2. Program mix and session folder in the outbox, without upload. Useful at once: the FLEX3 team gets real sessions for its pipeline.
3. Virtual camera (see the separate document), because it decides the canvas layout that streaming depends on.
4. Streaming to YouTube and as a camera for Teams/Zoom.
5. Segmented recording with background processing and the receiving server in `server/`, starting with a test of the split boundaries.
6. Stream to the FLEX3 server, as soon as MediaMTX runs and a stream key scheme exists.
