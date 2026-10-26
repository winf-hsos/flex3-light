# FLEX3 Light

FLEX3 Light runs on the lecturer's own laptop and takes over the jobs of the FLEX3 device at Osnabrück University of Applied Sciences: recording the lecture, delivering it to the FLEX3 server, streaming the live part and acting as the camera for Teams and Zoom. Most lecturers have a webcam and only plug their laptop into the projector, so a piece of software can cover much of what the device does, and on Windows it also has to be the camera for video calls, because usually only one application can hold the webcam.

It is a fork of the [lecture recorder](https://github.com/winf-hsos/lecture-recorder), with its Git history, and starts out with the same features: it records screen, webcam and microphone in one go and afterwards files two synchronized MP4 videos (screen and camera) into a folder per term and module. Delivery to the server, streaming and the virtual camera are being planned in [docs/](docs/).

OBS does the capturing and encoding in the background, while the app gives you a compact window to pick module and devices and turns into a slim bar while you are recording. The user interface is in German, because it was written for teaching at a German university; code, comments and this documentation are in English.

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
- **A calm main window.** The setup window behaves like any other window; only the slim bar while recording and the save question stay on top. Options such as hiding the taskbar and Alt+Tab and the folders live in the settings behind the gear.
- **The window itself is never recorded.** While recording it stays visible on your screen but is excluded from capture (`SetWindowDisplayAffinity`). Windows applies this to screenshots as well, so it is only switched on right before a recording starts and off again afterwards.
- **The taskbar is left out** of the screen track by default, using the work area of the selected monitor.
- **The Alt+Tab overview is hidden.** The Windows task switcher cannot be excluded from capture, so the app logs when you press Alt+Tab and freezes the screen track for that moment afterwards, while audio and camera keep running.
- **Pause and resume** with a button or a global hotkey.
- **Save, continue or discard**: stopping first pauses the recording and asks what to do, so an accidental stop costs nothing. A discarded recording goes to the recycle bin.
- **Two MP4 files per session**, for example `2026-10-09 - Regression - Bildschirm.mp4` and `… - Kamera.mp4`, filed under `<archive>\<term>\<module>\`. Both tracks share the same audio and start at the same instant, so they can be placed side by side in an editor without syncing.
- **Done when you stop.** OBS starts a new file every minute, and each finished minute is split in the background while the lecture goes on. After the stop only the last minute is left, so even a three-hour lecture is filed within seconds.
- **Delivery to the FLEX3 Light server** (`server/`), also during the lecture: each processed minute is uploaded right away, including a program mix in 1920×1080 for the FLEX3 pipeline, and the server builds a session folder after the FLEX3 contract once the recording is complete. Without a configured server, recordings are only filed locally.
- **Modules come from the lecturer's profile.** "Module verwalten" in the window lists the modules with code, language and optionally ILIAS course and Opencast series. The profile is kept locally and synced with the server, so every device of the same lecturer offers the same list, also after changes made offline. On the first start it takes over the module folders of the current term in the archive and an optional Markdown list (`module_file`); a module typed in when recording joins the profile on its own.
- **Works with and without the server.** The window shows whether it is online; while it is offline, recordings and module changes stay local and go to the server when it is back. There is no separate offline mode to switch.
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

Requirements: Windows 10 or 11, OBS Studio 28 or later, [ffmpeg](https://ffmpeg.org/) on the `PATH` (with `h264_nvenc` and `libx264`, for example a gyan.dev full build), and Python 3.10 or later.

```bash
pip install -r requirements.txt
```

```bash
python recorder.py
```

`flex3-light.cmd` starts it without a console window, and `python tools/setup_obs.py` checks the OBS connection and lists the devices OBS sees. The app reads the remote-control password from OBS's own configuration; the environment variable `OBS_WEBSOCKET_PASSWORD` works as a fallback. The password is never printed.

To build the exe yourself, install PyInstaller and run:

```bash
python tools/build_exe.py
```

It bundles the ffmpeg from your `PATH` and writes `dist/flex3-light.exe` (about 115 MB).

## Usage

1. Pick or type the module and optionally a topic for the session; both go into the file name.
2. Check screen, camera and microphone, watch the level bar and the camera preview.
3. Click **Aufnahme starten** or press **Ctrl+Alt+R**. The window shrinks to a bar at the top of the screen.
4. Pause and resume with the pause button or **Ctrl+Alt+P**.
5. Stop with the stop button, or press **Ctrl+Alt+R** twice within three seconds (the double press protects against stopping a lecture with a stray keystroke). The recording is paused and the bar asks whether to save it: **Speichern** files it, **Weiter** continues recording, and **Verwerfen** (click twice) moves the raw file to the recycle bin.

While recording, every finished one-minute segment is split into screen and camera (and the program mix, if a server is configured) at low priority, so it never slows OBS down. After stopping, the last segment follows, the parts are joined into the two MP4 files without re-encoding, and the raw segments are deleted only once the result has been checked to be as long as the parts. If anything fails, the raw segments are kept. Closing the window during a recording keeps it as well; if the app ends before the recording is filed, it continues at the next start. **Verwerfen** also deletes whatever was already uploaded.

To deliver recordings to a FLEX3 Light server, add its address and the token of this installation to the settings (the token is never printed or logged):

```json
"server_url": "https://flex3.example.org",
"server_token": "…",
"device_id": "flex3-light-pc1234",
"language": "de",
"upload_kbit": 20000
```

`upload_kbit` caps the upload bandwidth (optional); `device_id` defaults to `flex3-light-<computer name>`, `language` to `de` for modules without a language. The window shows how many parts have been transferred. Recordings go to the server as `unassigned`, with consent restricted to recording and nothing published, unless their module has both ILIAS course and Opencast series; then they arrive as `assigned`.

To try all this without a real server, `python tools/dev_server.py --configure` and then `python tools/dev_server.py` run one on this computer (see [server/README.md](server/README.md)).

The current term is derived from the date: winter term from 1 September to the end of February, summer term from 1 March. Folder names follow the pattern `2026-27 - WS` and `2026 - SS`. Module suggestions come from the lecturer's profile (see above).

Settings are stored in `%APPDATA%\flex3-light\settings.json`, the profile in `profile.json` next to it.

## How it works

| File | Role |
|---|---|
| `recorder.py` | Window and JavaScript bridge (pywebview with Edge WebView2), single instance, capture exclusion |
| `ui.html` | The user interface |
| `core.py` | Settings, device selection, recording state, hotkey handling |
| `delivery.py` | Sessions with their manifest; background threads that process each one-minute segment, file the archive and upload to the server |
| `server_client.py` | Client for the server's API, standard library only, with an optional bandwidth cap |
| `preview.py` | Camera preview: OBS source screenshots as an MJPEG stream on 127.0.0.1 with a random token in the address |
| `lecturer_profile.py` | The lecturer's modules: local copy, seeding on the first start, merge with the server entry by entry |
| `tools/dev_server.py` | Runs the server on this computer and points the app at it |
| `server/` | The receiving server (FastAPI), see [server/README.md](server/README.md) |
| `obs_remote.py` | Minimal obs-websocket v5 client, OBS startup, switching the remote control on |
| `obs_scene.py` | OBS profile, scene, sources, encoder settings, screen crop |
| `monitors.py` | Monitor geometry and taskbar margins from Windows |
| `hotkeys.py` | Global hotkeys and a low-level keyboard hook that detects Alt+Tab |
| `hardware.py` | Finds ffmpeg and picks NVENC or x264 |
| `split.py` | ffmpeg split into screen and camera track and the program mix, freezing the Alt+Tab spans, and joining parts without re-encoding; also usable on the command line |
| `tests/` | Tests for the server and for the whole delivery without OBS (`python -m pytest tests`, needs `requirements-dev.txt`) |
| `tools/benchmark.py` | Test recording that measures CPU, GPU and encoder load |
| `tools/build_exe.py` | Builds the single exe with PyInstaller |

Hiding Alt+Tab needs a small correction: the time OBS reports runs about 0.55 seconds ahead of the picture that ends up in the file, so the detected spans are shifted by that delay and padded a little on both sides before they are frozen.

## Limitations

- Windows only. Without an NVIDIA GPU, recording and splitting run on the CPU (x264), which a weaker laptop may struggle with at this canvas size.
- The camera field is 1920×1080; the screen field holds up to 1920×1200.
- Streaming and the virtual camera are not built in yet, and the server settings have no place in the window yet.
- The layout of the program mix (webcam small in the bottom right corner) is a proposal; the FLEX3 contract leaves it open.

## Third-party software

The exe contains [FFmpeg](https://ffmpeg.org/) (gyan.dev full build, licensed under the GPLv3; source at https://github.com/FFmpeg/FFmpeg and build details at https://www.gyan.dev/ffmpeg/builds/) and the Python runtime with pywebview, pythonnet and websockets, each under its own license.

## License

The source code is licensed under the MIT License, see [LICENSE](LICENSE). The bundled FFmpeg in the exe keeps its own license (GPLv3).
