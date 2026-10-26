"""Set up the OBS profile and scene of FLEX3 Light and list the devices.

    python tools/setup_obs.py

The recorder window does this on every start; this script is for checking by hand.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core
import obs_remote as remote
import obs_scene as scene

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raw_dir = Path(core.load_settings().get("raw_dir") or core.DEFAULT_RAW)
    remote.start(profile=scene.NAME, collection=scene.NAME)
    with remote.Obs() as o:
        scene.ensure_profile_and_scene(o, raw_dir)
        for kind, items in scene.devices(o).items():
            print(kind)
            for name, _ in items:
                print("   ", name)
