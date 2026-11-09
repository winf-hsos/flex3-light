# FLEX3 Light

FLEX3 Light runs on the lecturer's own laptop and takes over the jobs of the FLEX3 device at Osnabrück University of Applied Sciences: recording the lecture, delivering it to the FLEX3 server, streaming the live part and acting as the camera for Teams and Zoom. Most lecturers have a webcam and only plug their laptop into the projector, so a piece of software can cover much of what the device does, and on Windows it also has to be the camera for video calls, because usually only one application can hold the webcam.

It is a fork of the [lecture recorder](https://github.com/winf-hsos/lecture-recorder), with its Git history, and starts out with the same features: it records screen, webcam and microphone in one go and afterwards files two synchronized MP4 videos (screen and camera) into a folder per term and module. Delivery to the server, streaming and the virtual camera are being planned in [docs/](docs/).

OBS does the capturing and encoding in the background. The app's window follows the way a lecture goes: a sidebar leads to **Neue Aufnahme** (pick module and topic, check picture and sound, record), **Meine Aufnahmen** (the recordings on the FLEX3 server: watch one with subtitles, read and search its transcript, approve its cut and see how far it is on its way to publication), **Module** and **Einstellungen**. The window can be resized at its right and bottom edges or filled to the screen with the button next to minimize (or a double click on the title); it remembers its size. While you are recording, only a slim bar is left. The user interface is in German, because it was written for teaching at a German university; code, comments and this documentation are in English.

**Lecture recorder and FLEX3 Light side by side.** Both can be installed on the same machine. They keep separate settings (`%APPDATA%\flex3-light`), raw folders, OBS profiles and scene collections (`FLEX3Light` instead of `Lecture`) and taskbar identities. They cannot run at the same time, because both drive the one OBS instance and register the same hotkeys; FLEX3 Light refuses to start while the lecture recorder is open. On its first start FLEX3 Light takes over the lecture recorder's settings, except for the raw folder.

| Before recording | After stopping |
|---|---|
| ![Setup view](docs/screenshots/setup.png) | ![Result](docs/screenshots/result.png) |

While recording, the window shrinks to a slim bar that turns yellow when paused and can be minimized (the hotkeys keep working), and stopping asks whether to keep the recording:

![Recording bar](docs/screenshots/recording.png)

![Paused bar](docs/screenshots/paused.png)

![Save question](docs/screenshots/save-question.png)

## Features

- **Screen, camera and microphone are selectable**, the microphone level is shown live, and a selected webcam shows its picture next to the settings, so you notice a muted mic or a covered camera before the lecture starts. Because OBS holds the webcam, the picture comes from OBS as source screenshots, streamed to the window as MJPEG at 25 frames per second by a small server on 127.0.0.1 (`preview.py`); it only runs while the window shows it.
- **A calm main window.** The window behaves like any other window; only the slim bar while recording and the save question stay on top. Before recording, three checks next to the record button tell whether a module is chosen, sound arrives and the camera runs. Options such as hiding the taskbar and Alt+Tab, the folders and the keys of the AI services live under **Einstellungen**.
- **Meine Aufnahmen** shows the server's page for the recordings of whoever is signed in, newest first (since phase 5 the app itself only records). Each recording shows its way through the pipeline as five stations (recorded, transcript, cut, content, publish), plays in the window with its subtitles, and its transcript jumps to the spot that is clicked. Without a server connection the page says so and leads to **Einstellungen**.
- **The window itself is never recorded.** While recording it stays visible on your screen but is excluded from capture (`SetWindowDisplayAffinity`). Windows applies this to screenshots as well, so it is only switched on right before a recording starts and off again afterwards.
- **The taskbar is left out** of the screen track by default, using the work area of the selected monitor.
- **"Zeigen" shows which screen gets recorded**: a frame in the university's blue goes around exactly the recorded area for a few seconds, with a label in its middle, like "Identify" in the Windows display settings.
- **The Alt+Tab overview and the taskbar previews are hidden.** The Windows task switcher and the small window previews over the taskbar cannot be excluded from capture, so the app logs when you press Alt+Tab or a preview opens on the recorded screen and freezes the screen track for that moment afterwards, while audio and camera keep running.
- **Pause and resume** with a button or a global hotkey.
- **Privacy key**: the button with the crossed-out eye in the recording bar, or **Ctrl+Alt+H**, freezes the screen track until it is pressed again, while sound and camera go on. Press it before opening mail or a folder that should not end up in the recording.
- **Save, continue or discard**: stopping first pauses the recording and asks what to do, so an accidental stop costs nothing. A discarded recording goes to the recycle bin.
- **One folder per recording**, `<archive>\<term>\<module>\<date> - <topic>\`, laid out like a FLEX3 session folder: `screen.mp4` and `camera.mp4` (both with the same audio and starting at the same instant, so they can be placed side by side in an editor without syncing), `recording.mp4` (program mix, the screen with the webcam small in a corner), `session.json` (module, language, date, duration and tracks) and `activity.json` (which program was in front when, and the spans frozen with the privacy key). The pipeline of [docs/pipeline.md](docs/pipeline.md) will add transcript, subtitles and the rest to the same folder. `activity.json` holds window titles and stays local.
- **Done when you stop.** OBS starts a new file every minute, and each finished minute is split in the background while the lecture goes on. After the stop only the last minute is left, so even a three-hour lecture is filed within seconds.
- **Delivery to the FLEX3 Light server** (`server/`), also during the lecture: each processed minute is uploaded right away, including a program mix in 1920×1080 for the FLEX3 pipeline, and the server builds a session folder after the FLEX3 contract once the recording is complete. Without a configured server, recordings are only filed locally.
- **Modules come from the lecturer's profile.** The page **Module** lists the modules with code, language and optionally ILIAS course, Opencast series and technical terms for the transcription. The profile is kept locally and synced with the server, so every device of the same lecturer offers the same list, also after changes made offline. On the first start it takes over the module folders of the current term in the archive and an optional Markdown list (`module_file`); a module typed in when recording joins the profile on its own.
- **Transcript and subtitles after the lecture.** By default a recording is only filed; the transcription is started on the recording under **Meine Aufnahmen**, or by itself if the setting "Nach der Aufnahme" says so (also: transcription and then the cut proposal). It is transcribed only while nothing is recorded, with AssemblyAI Universal-3 Pro, in the EU region when the key belongs there, as one file with the module's technical terms as key terms; the transcript is deleted at AssemblyAI right after it has been fetched. Should AssemblyAI fail, OpenAI `whisper-1` takes over in pieces cut at pauses, until it is shut down on 2027-02-26. `transcript.json` (word timestamps and sentences) and `subtitles.<language>.vtt` land in the session folder, and the window shows how far it is. Without a key for either service it waits. **Meine Aufnahmen** shows which service transcribed a recording and with which terms, and "Erneut transkribieren" runs the transcription and the subtitles again, for instance after terms were added to the module; the old transcript stays until the new one replaces it. Below the transcript, **Unsichere Stellen** lists the words AssemblyAI was unsure about, in their sentence and with a button to hear them; a word can be confirmed or corrected (the subtitles follow, `transcript.raw.json` keeps what the service returned) and taken into the module's terms, so the next lecture gets it right. **Löschen** moves a recording's whole session folder into the server's trash, which an administrator empties, after a question in the window; like every question before something that cannot be undone, it is a dialog of the page, not the browser's box.
- **The cut, proposed and approved.** After the subtitles the cut agent (OpenAI `gpt-6.1-sol`) looks at the recording, reads, looks at frames and listens, and writes an edit list `edit.json`: cuts, pauses, muted and blurred spans, zooms, chapters, clips, title and description, each with a reason. Rules add the silence at the edges, long pauses and the programs the lecturer wants cut. In **Meine Aufnahmen** the station "Schnitt" shows the list on a timeline under the video: the agent decides every proposal itself and sums up what is cut, by kind and why, which short videos come out of it and where a look is worth it; folded under "Vorschläge sichten und überarbeiten", each proposal can be watched, taken or dropped and its bounds moved by half seconds, with the agent's reason beside every rule proposal; own cuts are set from the playing position; "Mit Schnitt abspielen" plays the recording as it would be cut, without rendering; the programs in front during the recording can be set to "always cut" or "always keep" for all recordings. Nothing is rendered before "Schnitt freigeben"; then the long version `edited.mp4`, with a title card, chapter cards (or lower thirds) and a closing card whose texts can be changed beforehand, short sounds made once with ElevenLabs, the clips, subtitles and chapters for them and the thumbnail are rendered into the session folder, with the progress in the window, and the long version and every clip can be played in the window. Under **Einstellungen** the agent can be switched off, single kinds of proposals (pixelation, zoom and the others) can be taken away from it and the rules, and pauses get a card "Pause entfernt", are shortened or cut.
- **Works with and without the server.** The window shows whether it is online; while it is offline, recordings and module changes stay local and go to the server when it is back. There is no separate offline mode to switch.
- **A recording outlives the window.** If the app ends while OBS is still recording, the next start takes the recording over and shows the bar again, instead of leaving OBS running with nobody to stop it. Errors, crashes and hangs are written to `%APPDATA%\flex3-light\flex3-light.log`.
- **Nothing gets lost on the way.** Every session keeps a manifest, so work interrupted by a closed window, a crash or a missing network continues at the next start. Raw files are deleted only after the archive is written and checked, uploaded parts only after the server has confirmed the complete session.
- **Configurable folders** for the archive and for the raw recordings, plus an "open in folder" button once a recording is filed.
- **Moderate load and file size**: hardware encoding on NVIDIA (NVENC) with constant quality, about 3.4 GB per hour for the raw recording. Without an NVIDIA GPU the app falls back to x264, which works on any machine but uses noticeably more CPU.
- **One file to share**: a single `flex3-light.exe` with ffmpeg built in. It sets up OBS on its own, including the remote control.

## Quick start (exe)

1. Install [OBS Studio](https://obsproject.com/) 28 or later, open it once and close the auto-configuration wizard.
2. Download `flex3-light.exe` from the releases (the public repository is not set up yet) and start it.

That is all. The exe starts OBS minimized to the tray, switches on OBS's remote control (obs-websocket) with a random password if it is off, and creates its own OBS profile and scene collection named `FLEX3Light`, with a wide 3840×1200 canvas that holds the screen on the left and the webcam on the right. Existing OBS profiles are not touched. If OBS is already running with the remote control switched off, the app asks you to close OBS once so it can switch it on.

The exe is not code-signed, so on first start Windows SmartScreen may show "Windows protected your PC"; click **More info → Run anyway**. It takes a few seconds to start because it unpacks itself into a temporary folder.

## Running from source

Requirements: Windows 10 or 11, OBS Studio 28 or later, [ffmpeg](https://ffmpeg.org/) on the `PATH` (with `h264_nvenc` and `libx264`, for example a gyan.dev full build), [Typst](https://typst.app/) on the `PATH` for the summary PDF (`winget install Typst.Typst`), and Python 3.10 or later.

```bash
pip install -r requirements.txt
```

```bash
python recorder.py
```

`flex3-light.cmd` starts it without a console window. `python tools/start_menu.py` adds a Start menu entry with the app's icon and window ID, which can then be pinned to the taskbar ("An Taskleiste anheften"; Windows does not let programs pin themselves). `python tools/setup_obs.py` checks the OBS connection and lists the devices OBS sees. The app reads the remote-control password from OBS's own configuration; the environment variable `OBS_WEBSOCKET_PASSWORD` works as a fallback. The password is never printed.

To build the exe yourself, install PyInstaller and run:

```bash
python tools/build_exe.py
```

It bundles ffmpeg and Typst from your `PATH` and writes `dist/flex3-light.exe` (about 115 MB).

## Usage

1. Pick or type the module and optionally a topic for the session; both go into the file name.
2. Check screen, camera and microphone, watch the level bar and the camera preview.
3. When all three checks next to the button are green, click **Aufnahme starten** or press **Ctrl+Alt+R**. The window shrinks to a bar at the top of the screen.
4. Pause and resume with the pause button or **Ctrl+Alt+P**; freeze the screen picture with the eye button or **Ctrl+Alt+H**.
5. Stop with the stop button, or press **Ctrl+Alt+R** twice within three seconds (the double press protects against stopping a lecture with a stray keystroke). The recording is paused and the bar asks whether to save it: **Speichern** files it, **Weiter** continues recording, and **Verwerfen** (click twice) moves the raw file to the recycle bin.

While recording, every finished one-minute segment is split into screen and camera (and the program mix, if a server is configured) at low priority, so it never slows OBS down. After stopping, the last segment follows, the parts are joined into the two MP4 files without re-encoding, and the raw segments are deleted only once the result has been checked to be as long as the parts. If anything fails, the raw segments are kept. Closing the window during a recording keeps it as well; if the app ends before the recording is filed, it continues at the next start. **Verwerfen** also deletes whatever was already uploaded.

To deliver recordings to a FLEX3 Light server, paste the connection code you got from it under **Einstellungen, Server** and click **Verbinden**. The app exchanges the code once for a token of this computer, keeps the token in the Windows Credential Manager (it is never shown, printed or logged) and from then on sends recordings and profile changes there; **Trennen** goes back to recording locally only. `"upload_kbit": 20000` in `settings.json` caps the upload bandwidth (optional). The window shows how many parts have been transferred. Recordings go to the server as `unassigned`, with consent restricted to recording and nothing published, unless their module has both ILIAS course and Opencast series; then they arrive as `assigned`.

Keys for the AI services of the pipeline, AssemblyAI, OpenAI and ElevenLabs, are entered under **Einstellungen**, "KI-Dienste". They go into the Windows Credential Manager as "FLEX3 Light/assemblyai", "FLEX3 Light/openai" and "FLEX3 Light/elevenlabs", never into `settings.json` or the log, and the window only shows whether a key is stored. "Prüfen" asks the service whether the key works and, for OpenAI, which transcription models it may use, for AssemblyAI, in which region the data stays; it sends no audio or text. An ElevenLabs key may be restricted: FLEX3 Light only needs the permission for sound effects, and a key without the permission to read the user counts as valid. Environment variables such as `OPENAI_API_KEY` are not used.

To try all this without a real server, `python tools/dev_server.py` runs one on this computer and `python tools/dev_server.py --invite` prints a connection code for it (see [server/README.md](server/README.md)).

The current term is derived from the date: winter term from 1 September to the end of February, summer term from 1 March. Folder names follow the pattern `2026-27 - WS` and `2026 - SS`. Module suggestions come from the lecturer's profile (see above).

**The app records, the server does the rest** (phase 5, 2026-11-09). Transcription, cut proposal, rendering, summary and publishing run on the FLEX3 server only; "Nach der Aufnahme" says what starts there by itself. The server gets the activity log without window titles: only the word of the title list a title contained (see [server/README.md](server/README.md)). **Meine Aufnahmen** is the server's own page in the window, signed in with a one-time ticket, where a lecture is reviewed, its cut approved and its files fetched for the upload; "Im Browser öffnen" opens it in the browser. Publishing from the server unpacks the files into the folder for uploads under term and module, with the same names as before.

**The local copy.** Every recording is still filed in the archive on this computer first, so nothing depends on the network during a lecture. Once the server has confirmed a recording completely, its session folder gets `confirmed.json`, and seven days later the app moves the folder to the recycle bin; it checks once an hour. A recording the server never confirmed stays. Without a server connection the app only files.

**A server on this computer.** Until the server runs on the workstation (phase 6), a connection to `127.0.0.1` makes the app start the server itself (`local_server.py`) when nothing answers there: `tools/dev_server.py --idle-exit 30` without a window, logging to `%LOCALAPPDATA%\flex3-light-devserver\server.log`. It goes on working after the window is closed and ends by itself after thirty minutes without a request once its pipeline has nothing left to do. A server elsewhere is left alone, and the packaged exe starts none.

**What belongs to the lecturer and what to the computer** (since 2026-11-09). The lecturer's profile, `profile.json` in `%APPDATA%\flex3-light`, holds the modules and the preferences: everything under **Schnitt**, "Nach der Aufnahme" and the default language for modules without one. It is synced with the server like the modules, setting by setting, so a change on one computer reaches the others without undoing what was changed there meanwhile. `settings.json` next to it keeps only what belongs to this computer: screen, camera, microphone, hiding the taskbar and window switches, the folders, the window size, the upload cap and the server's address and `device_id`. Settings of an older version that belong to the profile move there on the first start, dated so that what the server already has wins.

## How it works

| File | Role |
|---|---|
| `recorder.py` | Window and JavaScript bridge (pywebview with Edge WebView2), single instance, capture exclusion |
| `ui.html` | The user interface |
| `core.py` | Settings, device selection, recording state, hotkey handling |
| `delivery.py` | Sessions with their manifest; background threads that process each one-minute segment, file the archive and upload to the server |
| `server_client.py` | Client for the server's API, standard library only, with an optional bandwidth cap |
| `pipeline/` | The steps after a recording, independent of the window, for the server to reuse: `audio.py` (pieces at pauses), `transcribe.py` (transcription with word timestamps), `subtitles.py` (WebVTT), `review.py` (unsure words and their correction), `edit.py` (the edit list `edit.json`), `cut_rules.py` (first cut proposals from rules), `cut_tools.py` and `cut_agent.py` (the cut agent with its tools, OpenAI `gpt-6.1-sol`), `ocr.py` (text with its position from the OCR built into Windows, for exact blurs and zooms), `timeline.py` and `render.py` (the approved list becomes videos), `cards.py` (title, chapter and closing cards, lower thirds), `sounds.py` (the sounds, made once with ElevenLabs), `summary.py` (the one-page summary in German and English as PDF, with `prompts/summary.md` and `templates/summary.typ`), `publish.py` (moves the finished files to the upload folder under Opencast names and records them in `publish.json`) |
| `pipeline_runner.py` | Queue that runs the pipeline on filed recordings, only while nothing is recorded |
| `credentials.py`, `services.py` | API keys in the Windows Credential Manager, and the check whether a key works |
| `activity.py` | Which program is in front during a recording, merged into spans for the cut agent |
| `identify.py` | Frames the recorded screen area for a moment (Tk, in a thread of its own) |
| `preview.py` | Camera preview: OBS source screenshots as an MJPEG stream on 127.0.0.1 with a random token in the address; also serves the videos and subtitles of listed recordings, with range requests |
| `library.py` | The session folders of the archive for **Meine Aufnahmen**: summary, transcript, media and the five stations |
| `recordings.py` | What a lecturer can do with a recording (review, cut, approval, deletion), shared by the app and the server |
| `lecturer_profile.py` | The lecturer's modules and preferences: local copy, seeding on the first start, merge with the server entry by entry |
| `tools/dev_server.py` | Runs the server on this computer and points the app at it |
| `local_server.py` | Starts that server when the connection points to this computer and nothing answers |
| `server/` | The receiving server (FastAPI), see [server/README.md](server/README.md) |
| `obs_remote.py` | Minimal obs-websocket v5 client, OBS startup, switching the remote control on |
| `obs_scene.py` | OBS profile, scene, sources, encoder settings, screen crop |
| `monitors.py` | Monitor geometry and taskbar margins from Windows |
| `hotkeys.py` | Global hotkeys, a low-level keyboard hook that detects Alt+Tab, and an event hook that notices the shell's previews over the taskbar |
| `hardware.py` | Finds ffmpeg and picks NVENC or x264 |
| `split.py` | ffmpeg split into screen and camera track and the program mix, freezing the Alt+Tab spans, and joining parts without re-encoding; also usable on the command line |
| `tests/` | Tests for the server and for the whole delivery without OBS (`python -m pytest tests`, needs `requirements-dev.txt`) |
| `tools/benchmark.py` | Test recording that measures CPU, GPU and encoder load |
| `tools/build_exe.py` | Builds the single exe with PyInstaller |
| `tools/make_icon.py` | Draws the app icon (`assets/icon.ico`, `assets/icon.png`): the blue square with the white dot from the window's sidebar; the window sets it at start, so the taskbar shows it also when run from source |

Hiding Alt+Tab needs a small correction: the time OBS reports runs about 0.55 seconds ahead of the picture that ends up in the file, so the detected spans are shifted by that delay and padded a little on both sides before they are frozen.

## Limitations

- Windows only. Without an NVIDIA GPU, recording and splitting run on the CPU (x264), which a weaker laptop may struggle with at this canvas size.
- The camera field is 1920×1080; the screen field holds up to 1920×1200.
- Streaming and the virtual camera are not built in yet.
- The layout of the program mix (webcam small in the bottom right corner) is a proposal; the FLEX3 contract leaves it open.

## Third-party software

The exe contains [FFmpeg](https://ffmpeg.org/) (gyan.dev full build, licensed under the GPLv3; source at https://github.com/FFmpeg/FFmpeg and build details at https://www.gyan.dev/ffmpeg/builds/) and the Python runtime with pywebview, pythonnet and websockets, each under its own license.

## License

The source code is licensed under the MIT License, see [LICENSE](LICENSE). The bundled FFmpeg in the exe keeps its own license (GPLv3).
