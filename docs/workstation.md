# The server on the workstation

Since 2026-11-25 the FLEX3 Light server can run on a workstation with Windows 11 and an NVIDIA card (phase 6 of the plan to separate app and server, 2026-11-06), while the server on the lecturer's own computer stays available for testing. This guide covers the software the workstation needs, the setup, moving the data from the laptop, updates and backups.

## Two connections in the app

The app keeps two connections side by side, each with its own address, device and token in the Windows Credential Manager:

| Connection | Address | Token in the Credential Manager |
|---|---|---|
| Server | the workstation, e.g. `http://<workstation>:8765` | `FLEX3 Light/server` |
| Testserver (lokal) | `http://127.0.0.1:8765` | `FLEX3 Light/server-test` |

Which one a connection code fills follows from its address: `127.0.0.1` or `localhost` is the test server, anything else the server. Under Einstellungen, Server, "Weiteren Server verbinden" takes the second code, and "Verbindung" switches between them. The app starts the local test server only while that connection is in use, as before (`local_server.py`). Switching waits while a recording is only partly uploaded, so that its parts never end up on two servers; a recording that has not started uploading goes to whichever server is chosen when it does. "Trennen" forgets only the connection in use.

An app from before this change had one connection; on its first start the new app files it under the connection its address belongs to, so a laptop connected to `127.0.0.1` keeps working as the test server.

## Software on the workstation

| What | Note |
|---|---|
| NVIDIA driver | current; `nvidia-smi` must work. Rendering uses NVENC when ffmpeg can open it and falls back to x264, which takes a multiple of the time |
| Python 3.14 | from python.org, with the `py` launcher |
| Git for Windows | to fetch the code and update it |
| ffmpeg | a full build with NVENC (e.g. gyan.dev), its `bin` on `PATH` or given to the setup with `-ToolPaths` |
| Typst | for the summary as PDF; without it the summary has no PDF |
| Windows OCR for de-DE and en-US | the setup installs both language capabilities; the cut agent reads text in the picture with them |

The workstation must not go to sleep (`powercfg /change standby-timeout-ac 0`). Windows Update may restart it; the server starts again at boot.

## Network

For the first tests the server is reached over plain HTTP at the workstation's fixed address, from the university network and its VPN, with no certificate. That works because the app accepts `http://` addresses, but the device token and the page session then travel unencrypted between the VPN gateway and the workstation, so anyone who can read traffic inside the campus network could take them over. That is acceptable for a test with one's own recordings and not for regular operation with other lecturers.

For regular operation the server gets a name in the campus DNS and a certificate of the university, and a reverse proxy such as Caddy terminates TLS in front of it (the server then listens on `127.0.0.1` only, `"host"` in `config.json`). The proxy has to allow request bodies of at least 100 MB, because one minute of camera track came to about 57 MB in a test; Caddy sets no limit by default. Lecturers then connect with a new code for the `https://` address.

The setup opens the port in the Windows firewall for every address that reaches the computer. Once the address range of the VPN is known, the setup is run again with `-RemoteAddress` set to it.

## Setup

In an elevated Windows PowerShell, as the account the server shall run under (it then runs as that account at boot, whether anyone is logged on or not, without a stored password):

```powershell
git clone https://github.com/winf-hsos/flex3-light.git C:\flex3-light\code
powershell -ExecutionPolicy Bypass -File C:\flex3-light\code\tools\workstation\setup.ps1 -Url http://<workstation>:8765 -Data D:\flex3-light\data -BackupTarget E:\flex3-light-backup
```

| Folder or file | Content |
|---|---|
| `C:\flex3-light\code` | the repository, checked out at a commit (`-Ref`, default `origin/main`) |
| `C:\flex3-light\venv` | the Python environment with `server\requirements.txt` |
| `C:\flex3-light\config.json` | data folder, address, port, tool folders, logs and the name the app shows under its connection (`"name"`, `-Name` of the setup, else the computer's name); read by `tools\workstation\run_server.py` |
| `C:\flex3-light\secrets.json` | the project's keys, `{"openai": "...", "assemblyai": "...", "elevenlabs": "..."}`, readable only by the server's account and administrators |
| `C:\flex3-light\logs` | `server.log`, a new file each day, two weeks kept; `backup.log` |
| the data folder (`-Data`) | sessions, accounts, profiles, packages, style, sounds; best on a drive of its own |

The setup checks the software, installs the OCR languages, creates the folders, the Python environment and `config.json`, writes an empty `secrets.json` if there is none, opens the port, and registers the task "FLEX3 Light Server", which starts the server at boot and again within five minutes if it ends. With `-BackupTarget` it also registers "FLEX3 Light Backup" for 03:00 every day. Running it again is safe and keeps code, data and keys.

Then put the keys into `secrets.json` and restart the task:

```powershell
Restart-ScheduledTask "FLEX3 Light Server"
```

`http://<workstation>:8765/status` shows from any computer in the network the server's name, which commit runs and whether the server is busy, with counts only; the app shows name and version in the foot of its window.

## Moving the data from the laptop

The data of the test server lies in `%LOCALAPPDATA%\flex3-light-devserver` on the laptop: `sessions`, `accounts.sqlite`, `profiles`, `packages`, `style`, `sounds`, `style-applied.json`, `pipeline-queue.json`, and if present `models.json`, `admins.txt` and `trash`. Copying all of it keeps the user ids, so the lecturer's recordings and profile are theirs on the workstation too. `server.log` stays behind, and so does a `tokens.json` from before 2026-11-06.

1. On the laptop, let the test server finish what it is doing; it ends by itself after thirty idle minutes. Its process must not run while copying, or `accounts.sqlite` may be copied half-written.
2. On the workstation, stop the task: `Stop-ScheduledTask "FLEX3 Light Server"`.
3. Copy the folder, over the network or with an external drive, e.g. from the laptop:

   ```powershell
   robocopy "$env:LOCALAPPDATA\flex3-light-devserver" "\\<workstation>\D$\flex3-light\data" /E /XF server.log tokens.json
   ```

4. Start the task again and look at `/status`.
5. On the workstation, make a connection code for the existing user, whose id `users` lists:

   ```powershell
   cd C:\flex3-light\code\server
   C:\flex3-light\venv\Scripts\python.exe -m flex3_light_server.admin --data D:\flex3-light\data --url http://<workstation>:8765 users
   C:\flex3-light\venv\Scripts\python.exe -m flex3_light_server.admin --data D:\flex3-light\data --url http://<workstation>:8765 invite <user id>
   ```

6. In the app, Einstellungen, Server, "Weiteren Server verbinden", paste the code. The app uses the server from then on and keeps the test server to switch back to.

The laptop keeps its copy; recordings made later on one server are not on the other.

## A first test

Record a few minutes with the app connected to the server, let it transcribe and propose a cut, approve it and render. In `server.log` the render names `h264_nvenc`, and the cut agent's overview has no note that text recognition is missing. Then switch to the test server and back, to see that both connections hold.

## Updates

New code reaches the workstation from GitHub, so a change is first committed and pushed. Since 2026-11-25 the workstation takes it by itself: the task "FLEX3 Light Update" (set up by `setup.ps1`) runs `update.ps1 -Auto -WaitMinutes 10` every quarter of an hour, without a window. With nothing new it does nothing and writes nothing; with a new commit on `main` it waits up to ten minutes for an idle server (the next run tries again) and then swaps the code, noting what it did in `logs\update.log`. Should the new code not answer within a minute, it goes back to the commit before and writes the new one to `update-failed.txt`, so it is not tried again until a newer commit is pushed. A push to `main` is therefore a release to the workstation within a quarter of an hour.

By hand, for instance to go back to an older version (then switch the task off first, or it takes `main` again):

```powershell
powershell -ExecutionPolicy Bypass -File C:\flex3-light\code\tools\workstation\update.ps1
```

It fetches the code, waits until `/status` reports the server idle twice in a row (nothing being joined, running or queued, at most two hours, `-WaitMinutes`), stops the task, checks out the new commit, installs the requirements and starts the task again. An upload in progress is no reason to wait, because the app sends the missing parts again once the server answers. `-Ref v0.2.0` or a commit goes to that version, and the script names the commit it came from, to go back with `-Ref`. A checkout with local changes is left alone.

## Backups

`backup.ps1` copies the data folder, `config.json` and `secrets.json` to the backup target, never deleting anything there, and `accounts.sqlite` through SQLite's own backup. Videos make most of it: about 28 GB for the first weeks of one lecturer. The target belongs on another drive than the data, and the backup folder holds the project's keys, so it needs the same care as `secrets.json`.
