"""Live camera preview for the window, as an MJPEG stream on 127.0.0.1, and the recordings' videos.

OBS holds the webcam, so the picture comes from OBS as a source screenshot (a few
milliseconds each). Sending every picture through pywebview's JavaScript bridge made
the preview stutter, so a small local HTTP server streams the pictures instead, as
multipart/x-mixed-replace, which the window's <img> plays natively. Each viewer gets
its own websocket connection to OBS, and pictures are only taken while the window
shows the stream and enabled() is true. The address carries a random token.

The same server hands the page the videos and subtitles of filed recordings
(/<token>/media/<session id>/<file>), with range requests so the player can seek;
media() decides which file that is, and returns None for anything else.
"""
from __future__ import annotations

import base64
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

import obs_remote as remote

FPS = 25
WIDTH = 640
TYPES = {".mp4": "video/mp4", ".vtt": "text/vtt; charset=utf-8"}


class PreviewServer:
    def __init__(self, source: str, enabled: Callable[[], bool],
                 media: Callable[[str, str], Path | None] = lambda sid, name: None):
        self.source, self.enabled, self.media = source, enabled, media
        self.token = secrets.token_urlsafe(16)
        server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        server.daemon_threads = True
        self.port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/{self.token}/camera.mjpg"

    @property
    def media_base(self) -> str:
        return f"http://127.0.0.1:{self.port}/{self.token}/media/"

    def _frames(self):
        """JPEG pictures of the source, at most FPS per second, while enabled() holds."""
        with remote.Obs() as o:
            while True:
                if not self.enabled():
                    time.sleep(0.2)
                    continue
                started = time.monotonic()
                r = o.request("GetSourceScreenshot", {"sourceName": self.source, "imageFormat": "jpg",
                                                      "imageWidth": WIDTH, "imageCompressionQuality": 75}, check=False)
                data = r.get("imageData", "")
                if data:
                    yield base64.b64decode(data.split(",", 1)[1])
                time.sleep(max(0.0, 1 / FPS - (time.monotonic() - started)))

    def _handler(self):
        preview = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                # an optional ?v=... only makes the player load a changed file again
                m = re.fullmatch(rf"/{re.escape(preview.token)}/media/([\w.-]+)/([\w.-]+)(?:\?[^/]*)?", self.path)
                if m:
                    self._file(preview.media(m.group(1), m.group(2)))
                    return
                if self.path != f"/{preview.token}/camera.mjpg":
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    for jpg in preview._frames():
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n%s\r\n"
                                         % (len(jpg), jpg))
                except (OSError, remote.ObsError):
                    pass                       # the window stopped watching, or OBS went away

            def _file(self, path: Path | None):
                """Serve a file, honouring one Range header, so a <video> can seek in it."""
                if path is None or not path.is_file():
                    self.send_error(404)
                    return
                size = path.stat().st_size
                start, end = 0, size - 1
                rng = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
                if rng and (rng.group(1) or rng.group(2)):
                    if rng.group(1):
                        start = int(rng.group(1))
                        end = min(int(rng.group(2)), size - 1) if rng.group(2) else size - 1
                    else:
                        start = max(0, size - int(rng.group(2)))
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                else:
                    self.send_response(200)
                self.send_header("Content-Type", TYPES.get(path.suffix, "application/octet-stream"))
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Access-Control-Allow-Origin", "*")   # the page itself is a file:// page
                self.end_headers()
                try:
                    with open(path, "rb") as f:
                        f.seek(start)
                        left = end - start + 1
                        while left > 0:
                            chunk = f.read(min(1 << 20, left))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            left -= len(chunk)
                except OSError:
                    pass                       # the player stopped reading, e.g. to seek

            def log_message(self, *args):
                pass

        return Handler
