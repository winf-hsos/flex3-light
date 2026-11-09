"""Client for the FLEX3 Light server (server/), standard library only.

The token goes into the Authorization header and is never printed or logged.
Uploads stream the file from disk and can be capped to a bandwidth, so a
parallel Teams call or stream keeps priority.
"""
from __future__ import annotations

import json
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path


class ServerError(RuntimeError):
    def __init__(self, status: int, detail):
        super().__init__(f"HTTP {status}: {detail}")
        self.status, self.detail = status, detail


class _Throttled:
    """File reader that sleeps so that it never delivers more than `rate` bytes per second."""

    def __init__(self, f, rate: int | None):
        self.f, self.rate, self.sent, self.start = f, rate, 0, time.monotonic()

    def read(self, n: int = -1) -> bytes:
        chunk = self.f.read(min(n, 256 * 1024) if n and n > 0 else 256 * 1024)
        self.sent += len(chunk)
        if self.rate:
            ahead = self.sent / self.rate - (time.monotonic() - self.start)
            if ahead > 0:
                time.sleep(ahead)
        return chunk


class ServerClient:
    def __init__(self, url: str, token: str, rate_kbit: int | None = None, timeout: float = 60):
        self.url, self._token, self.timeout = url.rstrip("/"), token, timeout
        self.rate = rate_kbit * 1000 // 8 if rate_kbit else None

    def __repr__(self) -> str:
        return f"ServerClient({self.url!r})"        # never the token

    def _call(self, method: str, path: str, body=None, headers: dict | None = None, length: int | None = None):
        h = {"Authorization": f"Bearer {self._token}", **(headers or {})}
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        if length is not None:
            h["Content-Length"] = str(length)
        req = urllib.request.Request(self.url + path, data=body, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read() or b"{}").get("detail")
            except ValueError:
                detail = e.reason
            raise ServerError(e.code, detail) from None

    def health(self) -> None:
        self._call("GET", "/health")

    def open_session(self, sid: str) -> str:
        return self._call("PUT", f"/sessions/{sid}")[1]["state"]

    def status(self, sid: str) -> dict:
        return self._call("GET", f"/sessions/{sid}")[1]

    def put_part(self, sid: str, track: str, index: int, path: Path, sha256: str) -> None:
        with open(path, "rb") as f:
            self._call("PUT", f"/sessions/{sid}/parts/{track}/{index}", body=_Throttled(f, self.rate),
                       headers={"X-Content-SHA256": sha256, "Content-Type": "video/mp4"},
                       length=path.stat().st_size)

    def complete(self, sid: str, session: dict, parts: dict[str, int], activity: dict | None = None) -> str:
        """activity: {"activity": [[start, end, program, matched word]], "private": [[start, end]]}, never
        window titles (cut_rules.without_titles)."""
        body = {"session": session, "parts": parts}
        if activity is not None:
            body["activity"] = activity
        return self._call("POST", f"/sessions/{sid}/complete", body=body)[1]["state"]

    def delete(self, sid: str) -> None:
        self._call("DELETE", f"/sessions/{sid}")

    def get_profile(self) -> dict:
        return self._call("GET", "/profile")[1]

    def sync_profile(self, modules: list[dict], preferences: dict | None = None) -> dict:
        body = {"modules": modules}
        if preferences is not None:
            body["preferences"] = preferences
        return self._call("POST", "/profile/sync", body=body)[1]

    def me(self) -> dict:
        return self._call("GET", "/me")[1]

    def library_ticket(self) -> dict:
        """A one-time ticket for the page "Meine Aufnahmen" on the server: {"ticket", "path"}."""
        return self._call("POST", "/library/ticket")[1]

    def library_call(self, call: str, *args):
        """An action of the page "Meine Aufnahmen" on the server, as the page itself makes it."""
        return self._call("POST", f"/library/api/{call}", body=list(args))[1]

    def download(self, path: str, target: Path) -> None:
        """Save a file of the server to target, e.g. the package of a published recording."""
        req = urllib.request.Request(self.url + path, headers={"Authorization": f"Bearer {self._token}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r, open(target, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
        except urllib.error.HTTPError as e:
            raise ServerError(e.code, e.reason) from None


def register(url: str, key: str, device_name: str, timeout: float = 20) -> dict:
    """Exchange an invitation key for a device token: {"token", "device_id", "user_id", "user_name"}."""
    return ServerClient(url, "", timeout=timeout)._call(
        "POST", "/devices/register", body={"key": key, "device_name": device_name})[1]
