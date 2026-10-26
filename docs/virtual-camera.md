# Virtual camera for Teams and Zoom: proposal

Status: proposal of 2026-10-26, nothing of it is built yet. Decisions that only Nicolas can make are marked **Decision**. Claims without a primary source are marked *unverified*.

## The problem

On Windows a webcam can usually be opened by only one application. While OBS holds the webcam for the recording, Teams and Zoom cannot use it. FLEX3 Light therefore has to offer a camera of its own that Teams and Zoom can pick, fed from the picture OBS already has.

Measured on the development machine: OBS 32.2.2 with obs-websocket 5.7.4 and Windows 11 build 26200. OBS records the main canvas of 3840×1200 at 30 fps, with the screen on the left and the webcam on the right.

## What OBS offers

**The OBS Virtual Camera** has been built into OBS for Windows since version 26. It is a DirectShow filter, registered machine-wide by the OBS installer, so FLEX3 Light needs no admin rights of its own ([filter source](https://github.com/obsproject/obs-studio/blob/master/plugins/win-dshow/virtualcam-module/virtualcam-filter.cpp), [OBS troubleshooting](https://obsproject.com/kb/virtual-camera-troubleshooting)). obs-websocket can start and stop it with `StartVirtualCam` and `StopVirtualCam` and report its state with `GetVirtualCamStatus` ([protocol](https://github.com/obsproject/obs-websocket/blob/master/docs/generated/protocol.md)); all three are available on the development machine.

**What it shows** is set in OBS as the output type, with four choices: Program, Preview, a fixed scene, or a single source ([OBS guide](https://obsproject.com/kb/virtual-camera-guide)). The websocket cannot change this setting. It is stored in the scene collection file under `"virtual-camera": {"type2": 1, "scene": "…"}`, where 1 means a fixed scene ([SceneCollections.cpp](https://github.com/obsproject/obs-studio/blob/master/frontend/widgets/OBSBasic_SceneCollections.cpp), [VCamConfig.hpp](https://github.com/obsproject/obs-studio/blob/master/frontend/utility/VCamConfig.hpp)). Because FLEX3 Light has its own scene collection, it can write the setting once and afterwards only change the contents of that scene over the websocket.

**The catch is the resolution.** Whatever the output type, the virtual camera runs at the output resolution of the main canvas ([BasicOutputHandler.cpp](https://github.com/obsproject/obs-studio/blob/master/frontend/utility/BasicOutputHandler.cpp), [obs-view.c](https://github.com/obsproject/obs-studio/blob/master/libobs/obs-view.c)). With today's canvas Teams would get a strip of 3840×1200, where a webcam picture fills only a small part of a 3.2:1 frame. The same holds for streaming, which also sends the main canvas.

**Additional canvases** exist since OBS 31: libobs can create a canvas with its own resolution, and a plugin can route the virtual camera to it ([canvas API](https://github.com/obsproject/obs-studio/blob/master/docs/sphinx/reference-canvases.rst), marked unstable). The websocket can list canvases and build scenes on them (`canvasUuid`), but it cannot create one; on the development machine `GetCanvasList` returns only the main canvas. The plugin Aitum Vertical shows that it works: it creates a canvas and points the virtual camera at it ([source](https://github.com/Aitum/obs-vertical-canvas/blob/main/vertical-canvas.cpp)). It is a GPL plugin made for vertical video, and whether it accepts a 1920×1080 landscape canvas is unverified.

## Do Teams and Zoom accept it?

In practice Teams and Zoom list the OBS Virtual Camera, while the Windows camera app does not, because it only sees Media Foundation cameras ([OBS forum](https://obsproject.com/forum/threads/obs-virtual-camera-doesnt-show-as-a-connected-camera-in-win11-settings.175246/), [Microsoft Q&A](https://learn.microsoft.com/en-us/answers/questions/872883/which-api-to-use-for-creating-virtual-webcam)). Updates of both clients have broken DirectShow cameras for a while more than once: a black picture in the new Teams ([Tech Community](https://techcommunity.microsoft.com/discussions/microsoftteams/new-microsoft-teams-has-broken-virtual-cameras-again-/4062479/replies/4065139)), and a missing camera in Zoom ([Zoom community](https://community.zoom.com/meetings-2/obs-virtual-camera-not-working-after-updates-2023-41532)). Neither Microsoft nor Zoom documents support for it officially. The virtual camera should be started before Teams or Zoom opens it, otherwise the filter announces the size of its last run.

## Alternatives

| Approach | What it gives | Effort and risk |
|---|---|---|
| **Windows 11 multi-app camera** (24H2 and later) | Several apps open the same webcam at once, according to Microsoft also Teams and Zoom together ([Microsoft support](https://support.microsoft.com/en-us/windows/hardware/camera/manage-cameras-with-camera-settings-in-windows-11)) | No code. Teams gets the raw webcam, without any composition. Whether OBS's DirectShow capture takes part in the sharing is unverified, and the university's IT can switch the setting off on managed machines. |
| **Own Media Foundation camera** (`MFCreateVirtualCamera`, Windows 11) | A camera that every app sees, including the Windows camera app ([API](https://learn.microsoft.com/en-us/windows/win32/api/mfvirtualcamera/nf-mfvirtualcamera-mfcreatevirtualcamera), [Microsoft sample](https://github.com/microsoft/Windows-Camera/tree/master/Samples/VirtualCamera), [VCamSample](https://github.com/smourier/VCamSample)) | A native COM DLL, registration in HKLM with admin rights, code signing, several weeks. The VCamSample author reports that Teams shows the camera in its preview without sending it to the other side. |
| **Softcam** ([tshino/softcam](https://github.com/tshino/softcam), MIT) | A DirectShow camera that the app itself feeds | The same DirectShow caveats as OBS, plus an admin install. Only worthwhile if OBS's camera fails. |
| pyvirtualcam, Unity Capture, NVIDIA Broadcast, Logi Tune | | GPL, unmaintained or tied to hardware; not suitable for a free MIT app. |

## The real decision: which canvas

All camera and streaming options run into the same question, because OBS sends its main canvas. There are two sensible layouts.

**A. Keep the wide canvas (3840×1200)** for the recording, as today. The separate screen and camera tracks stay, and the program mix for `recording.mp4` is made afterwards with ffmpeg (see [delivery-and-streaming.md](delivery-and-streaming.md)). For Teams, Zoom and streaming a second 1920×1080 canvas is needed, which only a plugin can create: either Aitum Vertical as a separately installed plugin or a small FLEX3 plugin of our own, written in C against libobs (GPL) while the Python app stays MIT. Both depend on an API that OBS marks as unstable.

**B. A 1920×1080 canvas showing the program mix.** OBS records, streams and serves the camera from the same picture, with stock OBS only. The recording is directly the contract's `recording.mp4`. For Teams the virtual camera points at a second scene "Kamera" that shows only the webcam in full size. The price is that the separate tracks disappear, because OBS records only the mix; for videos edited later with screen and camera side by side the lecture recorder stays available.

**C. A mixture:** FLEX3 Light records with layout A by default and switches to layout B only for sessions with Teams, Zoom or a stream. OBS accepts new video settings over the websocket as long as no output runs, so the switch happens before the recording starts.

Since 2026-10-26 the separate tracks are worth more than before: Nicolas allowed extra tracks in the session folder (FLEX3 IT concept §4, decision 31), so with layout A the server gets `screen.mp4` and `camera.mp4` next to the mix instead of the mix alone.

**Decision:** layout A, B or C. Proposed: **C**. Sessions without a live part keep everything layout A gives (the mix plus both tracks, for the archive and for the server), while sessions with Teams, Zoom or a stream get a proper 1080p picture with stock OBS and no plugin. The cost is that live sessions arrive with the mix only. A plugin for a second canvas (layout A for everything) remains possible later if that turns out to matter.

## Proposed steps

1. **Test multi-app camera on this machine** (in the camera settings of Windows): whether OBS records the webcam while Teams uses it at the same time. If it works, lecturers who only need their face in Teams get it without any FLEX3 code, and it costs one test.
2. **Virtual camera with stock OBS** according to the canvas decision: write the output type "scene" into FLEX3 Light's own scene collection, add a scene "Kamera", start and stop the camera from the window with `StartVirtualCam` and `StopVirtualCam`, and show its state in the bar. The window should remind lecturers to start the camera before joining the meeting.

   The scene "Kamera" shows **only the webcam**, in full size; the screen reaches Teams and Zoom through their own screen sharing (Nicolas, 2026-10-26). A second picture with the screen and the webcam small in a corner would have been possible, switched within the same scene with `SetSceneItemEnabled`, but Teams and Zoom send a camera at a lower resolution and with stronger compression than a screen share, so slides would suffer.

   Because the scene shows the webcam only, it still needs a 16:9 canvas: on today's 3840×1200 canvas the webcam would fill a small part of a wide strip. The canvas decision below therefore stands. If the multi-app camera of step 1 works, it covers this case without any OBS camera, and the OBS camera remains the fallback for machines where Windows does not allow it, for example because the IT department switched it off.
3. **Test with Teams and Zoom** on a university laptop, including the remote side, because the preview alone proves nothing.
4. Only if DirectShow stops working in Teams: a Media Foundation camera of our own, based on VCamSample.

## Risks

- Updates of Teams or Zoom can break DirectShow cameras at any time; this cannot be fixed on our side.
- Any additional canvas depends on an unstable OBS API, so a plugin may need fixes with each OBS release.
- Managed university machines may lock camera settings or forbid admin installs.
