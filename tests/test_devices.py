"""Cameras, microphones and screens plugged in or out while the app runs show up without a restart."""
from __future__ import annotations

import sys
import threading
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core  # noqa: E402
import device_watch  # noqa: E402

NONE = core.scene.NO_CAMERA


def lists(cameras, mics=("Standard",), screens=("Monitor 1",)):
    return {"screen": [(n, "s-" + n) for n in screens], "camera": [(n, "c-" + n) for n in cameras],
            "microphone": [(n, "m-" + n) for n in mics]}


class FakeObs:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _core(monkeypatch, cameras, wanted_camera):
    monkeypatch.setattr(core.remote, "Obs", FakeObs)
    state = {"lists": lists(cameras)}
    monkeypatch.setattr(core.scene, "devices", lambda o: {k: list(v) for k, v in state["lists"].items()})
    c = types.SimpleNamespace(session=None, lock=threading.RLock(), devices_stamp=0, applied=[],
                              devices=core.Core._read_devices(None))
    c.choice = {k: v[0][0] for k, v in c.devices.items()}
    c.choice["camera"] = wanted_camera if wanted_camera in [n for n, _ in c.devices["camera"]] else NONE
    c.wanted = dict(c.choice, camera=wanted_camera)
    c.apply_devices = lambda: c.applied.append(dict(c.choice))
    for name in ("refresh_devices", "_device_id"):
        setattr(c, name, getattr(core.Core, name).__get__(c))
    c._read_devices = core.Core._read_devices
    return c, state


def test_pick_prefers_the_choice_then_the_current_then_a_likely_one():
    guess = core.DEVICE_GUESS["camera"]
    assert core.pick_device(["A", "C920"], "A", "C920", guess) == "A"
    assert core.pick_device(["B", "C920"], "A", "B", guess) == "B"
    assert core.pick_device(["B", "HD Pro Webcam C920"], "A", "gone", guess) == "HD Pro Webcam C920"
    assert core.pick_device(["B"], None, None, guess) == "B"
    assert core.pick_device([], "A", None, guess) == ""


def test_a_camera_plugged_in_appears_and_the_chosen_one_comes_back(monkeypatch):
    c, state = _core(monkeypatch, ["Integrated Webcam"], "HD Pro Webcam C920")
    assert c.choice["camera"] == NONE                     # the chosen webcam was not there at the start
    state["lists"] = lists(["Integrated Webcam", "HD Pro Webcam C920"])
    assert c.refresh_devices()
    assert [n for n, _ in c.devices["camera"]] == ["Integrated Webcam", "HD Pro Webcam C920", NONE]
    assert c.choice["camera"] == "HD Pro Webcam C920" and c.applied and c.devices_stamp == 1


def test_a_new_microphone_is_listed_without_switching_to_it(monkeypatch):
    c, state = _core(monkeypatch, ["HD Pro Webcam C920"], "HD Pro Webcam C920")
    state["lists"] = lists(["HD Pro Webcam C920"], mics=("Standard", "DJI Mic"))
    assert c.refresh_devices()
    assert "DJI Mic" in [n for n, _ in c.devices["microphone"]]
    assert c.choice["microphone"] == "Standard" and not c.applied and c.devices_stamp == 1


def test_an_unplugged_camera_is_replaced_but_stays_wanted(monkeypatch):
    c, state = _core(monkeypatch, ["Integrated Webcam", "HD Pro Webcam C920"], "HD Pro Webcam C920")
    state["lists"] = lists(["Integrated Webcam"])
    c.refresh_devices()
    assert c.choice["camera"] == "Integrated Webcam" and c.wanted["camera"] == "HD Pro Webcam C920"
    state["lists"] = lists(["Integrated Webcam", "HD Pro Webcam C920"])
    c.refresh_devices()
    assert c.choice["camera"] == "HD Pro Webcam C920"


def test_nothing_changes_during_a_recording_or_without_a_change(monkeypatch):
    c, state = _core(monkeypatch, ["HD Pro Webcam C920"], "HD Pro Webcam C920")
    assert not c.refresh_devices() and c.devices_stamp == 0
    c.session = object()
    state["lists"] = lists(["HD Pro Webcam C920", "Other"])
    assert not c.refresh_devices() and "Other" not in [n for n, _ in c.devices["camera"]]


def test_obs_is_asked_only_after_windows_reports_a_change(monkeypatch):
    calls = []
    prints = iter([("a",), ("a",), ("b",), ("b",), ("b",)])
    monkeypatch.setattr(device_watch, "fingerprint", lambda: next(prints))
    clock = iter([0.0, 2.0, 4.0, 5.5, 9.5])
    monkeypatch.setattr(core.time, "time", lambda: next(clock))
    c = types.SimpleNamespace(device_print=("a",), devices_due=[], refresh_devices=lambda: calls.append(1))
    watch = core.Core.watch_devices.__get__(c)
    watch(); watch()                                     # no change: OBS is not asked
    assert calls == []
    watch()                                              # change at 4.0: ask at 5.0 and 9.0
    assert c.devices_due == [5.0, 9.0] and calls == []
    watch(); watch()
    assert calls == [1, 1] and c.devices_due == []


def test_the_fingerprint_is_cheap_and_stable():
    a, b = device_watch.fingerprint(), device_watch.fingerprint()
    assert a == b
    if sys.platform == "win32":
        assert len(a) == 5
