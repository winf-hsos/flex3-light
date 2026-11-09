"""Live camera preview for the window, as an MJPEG stream on 127.0.0.1.

OBS holds the webcam, so the picture comes from OBS as a source screenshot (a few
milliseconds each). Sending every picture through pywebview's JavaScript bridge made
the preview stutter, so a small local HTTP server streams the pictures instead, as
multipart/x-mixed-replace, which the window's <img> plays natively. Each viewer gets
its own websocket connection to OBS, and pictures are only taken while the window
shows the stream and enabled() is true. The address carries a random token.

Until phase 5 (2026-11-09) the same server also played the recordings in the window; that is
the FLEX3 server's job now (/library/media/...).
"""
from __future__ import annotations

import base64
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

import obs_remote as remote

FPS = 25
WIDTH = 640


class PreviewServer:
    def __init__(self, source: str, enabled: Callable[[], bool]):
        self.source, self.enabled = source, enabled
        self.token = secrets.token_urlsafe(16)
        server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        server.daemon_threads = True
        self.port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/{self.token}/camera.mjpg"

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

            def log_message(self, *args):
                pass

        return Handler
