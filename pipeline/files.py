"""Reading and writing the JSON files of a session folder while the window reads them too.

Windows refuses to open a file in the moment it is replaced, and to replace a file that
is open. The window reads pipeline.json and transcript.json several times a second while
the pipeline writes them, so both sides wait a moment and try again instead of failing.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")


def retry(action: Callable[[], T], tries: int = 25) -> T:
    for attempt in range(tries):
        try:
            return action()
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(0.02)
    raise AssertionError("unreachable")


def read_json(path: Path, default=None):
    """The file's content, or default if it is missing, unreadable or broken."""
    try:
        return retry(lambda: json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {} if default is None else default


def write_json(path: Path, data) -> None:
    """Write next to the file and replace it in one step, so a reader never sees half a file."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    retry(lambda: os.replace(tmp, path))
