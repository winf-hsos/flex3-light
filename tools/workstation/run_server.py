"""Run the FLEX3 Light server on the workstation, as the scheduled task set up by setup.ps1 does.

    python tools/workstation/run_server.py --config C:\\flex3-light\\config.json

The config is the JSON file setup.ps1 writes:

    {"data": "D:\\flex3-light\\data",            the server's data folder
     "secrets": "C:\\flex3-light\\secrets.json", the project's keys for the AI services
     "host": "0.0.0.0", "port": 8765,
     "url": "http://<workstation>:8765",      the address lecturers connect to, for connection codes
     "path": ["C:\\tools\\ffmpeg\\bin"],         folders put in front of PATH, for ffmpeg and typst
     "logs": "C:\\flex3-light\\logs"}

Unlike tools/dev_server.py it never ends by itself and reads no keys from the Credential
Manager: the keys are the project's, in the secrets file. Its log goes to server.log in the
logs folder, a new file each day and two weeks kept; the log names recordings by their id
only, as everywhere in the server.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load(config: Path) -> dict:
    c = json.loads(config.read_text(encoding="utf-8-sig"))
    for key in ("data", "secrets"):
        if not c.get(key):
            raise SystemExit(f"{config}: '{key}' is missing")
    return {"host": "0.0.0.0", "port": 8765, "url": "", "path": [], "logs": str(config.parent / "logs"), **c}


def environment(c: dict) -> None:
    """What app_from_env() and the admin tool read, and the tools on PATH."""
    os.environ["FLEX3_LIGHT_SERVER_DATA"] = c["data"]
    os.environ["FLEX3_LIGHT_SERVER_SECRETS"] = c["secrets"]
    if c["url"]:
        os.environ["FLEX3_LIGHT_SERVER_URL"] = c["url"]
    if c["path"]:
        os.environ["PATH"] = os.pathsep.join([*c["path"], os.environ.get("PATH", "")])


def logging_to(folder: Path) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    handler = {"class": "logging.handlers.TimedRotatingFileHandler", "filename": str(folder / "server.log"),
               "when": "midnight", "backupCount": 14, "encoding": "utf-8", "formatter": "plain"}
    return {"version": 1, "disable_existing_loggers": False,
            "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
            "handlers": {"file": handler},
            "root": {"handlers": ["file"], "level": "INFO"},
            "loggers": {"uvicorn": {"level": "INFO"}, "uvicorn.access": {"level": "WARNING"}}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", required=True, type=Path)
    a = ap.parse_args(argv)
    c = load(a.config)
    environment(c)
    Path(c["data"]).mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT / "server"))
    import uvicorn
    uvicorn.run("flex3_light_server.app:app_from_env", factory=True, host=c["host"], port=int(c["port"]),
                log_config=logging_to(Path(c["logs"])), timeout_graceful_shutdown=30)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
