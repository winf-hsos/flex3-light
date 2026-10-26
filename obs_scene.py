"""OBS profile, scene and devices for FLEX3 Light.

Wide canvas of 3840x1200: the screen on the left, the webcam on the right,
each fitted into a 1920x1200 field. split.py later cuts the recording into
two synchronized tracks. Everything here is idempotent.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import hardware
import obs_remote as remote

# OBS profile, scene collection and scene. The lecture recorder uses "Lecture". Letters and digits
# only, because the profile folder that recordEncoder.json goes into is named after it.
NAME = "FLEX3Light"
SCREEN, CAMERA, MICROPHONE = "Screen", "Camera", "Microphone"
FIELD_W, FIELD_H = 1920, 1200
NO_CAMERA = "keine"                 # shown in the (German) user interface


def output_settings(raw_dir: Path) -> list[tuple[str, str, str]]:
    return [
        ("Output", "Mode", "Advanced"),
        ("AdvOut", "RecType", "Standard"),
        ("AdvOut", "RecFilePath", Path(raw_dir).as_posix()),
        ("AdvOut", "RecFormat2", "mkv"),                 # survives a crash, unlike mp4
        ("AdvOut", "RecEncoder", hardware.obs_encoder()[0]),
        ("AdvOut", "RecTracks", "1"),
        ("AdvOut", "Track1Bitrate", "160"),
        ("AdvOut", "RecRescale", "false"),
        ("Output", "FilenameFormatting", "%CCYY-%MM-%DD %hh-%mm-%ss"),
    ]


def _input(o: remote.Obs, name: str, kind: str, settings: dict) -> None:
    if name not in [i["inputName"] for i in o.request("GetInputList")["inputs"]]:
        o.request("CreateInput", {"sceneName": NAME, "inputName": name, "inputKind": kind,
                                  "inputSettings": settings, "sceneItemEnabled": True})


def ensure_profile_and_scene(o: remote.Obs, raw_dir: Path) -> None:
    """Make sure profile, collection, canvas, output and the three sources exist."""
    Path(raw_dir).mkdir(parents=True, exist_ok=True)
    profiles = o.request("GetProfileList")
    if NAME not in profiles["profiles"]:
        o.request("CreateProfile", {"profileName": NAME})
    elif profiles["currentProfileName"] != NAME:
        o.request("SetCurrentProfile", {"profileName": NAME})
    collections = o.request("GetSceneCollectionList")
    if NAME not in collections["sceneCollections"]:
        o.request("CreateSceneCollection", {"sceneCollectionName": NAME})
    elif collections["currentSceneCollectionName"] != NAME:
        o.request("SetCurrentSceneCollection", {"sceneCollectionName": NAME})
    time.sleep(1)

    v = o.request("GetVideoSettings")
    if (v["baseWidth"], v["baseHeight"], v["fpsNumerator"]) != (2 * FIELD_W, FIELD_H, 30):
        o.request("SetVideoSettings", {"baseWidth": 2 * FIELD_W, "baseHeight": FIELD_H,
                                       "outputWidth": 2 * FIELD_W, "outputHeight": FIELD_H,
                                       "fpsNumerator": 30, "fpsDenominator": 1})

    # Output settings changed over the websocket only take effect after the profile is
    # reloaded. Restarting OBS would mean killing it, which triggers its crash dialog,
    # so switch to another profile and back instead.
    wanted = output_settings(raw_dir)
    differs = [w for w in wanted if o.request("GetProfileParameter", {
        "parameterCategory": w[0], "parameterName": w[1]})["parameterValue"] != w[2]]
    encoder_file = Path(os.environ["APPDATA"]) / "obs-studio" / "basic" / "profiles" / NAME / "recordEncoder.json"
    current = json.loads(encoder_file.read_text(encoding="utf-8")) if encoder_file.exists() else {}
    encoder = hardware.obs_encoder()[1]
    if differs or current != encoder:
        for category, name, value in wanted:
            o.request("SetProfileParameter", {"parameterCategory": category, "parameterName": name,
                                              "parameterValue": value})
        others = [p for p in o.request("GetProfileList")["profiles"] if p != NAME]
        if not others:
            o.request("CreateProfile", {"profileName": "Empty"})
            others = ["Empty"]
        o.request("SetCurrentProfile", {"profileName": others[0]})
        encoder_file.write_text(json.dumps(encoder), encoding="utf-8")
        o.request("SetCurrentProfile", {"profileName": NAME})
        time.sleep(1)

    if NAME not in [s["sceneName"] for s in o.request("GetSceneList")["scenes"]]:
        o.request("CreateScene", {"sceneName": NAME})
    o.request("SetCurrentProgramScene", {"sceneName": NAME})

    _input(o, SCREEN, "monitor_capture", {"capture_cursor": True})
    _input(o, CAMERA, "dshow_input", {"res_type": 1, "resolution": "1920x1080"})
    _input(o, MICROPHONE, "wasapi_input_capture", {})
    o.request("SetInputMute", {"inputName": CAMERA, "inputMuted": True}, check=False)
    for name in o.request("GetSpecialInputs").values():     # global desktop audio and mic off
        if name:
            o.request("SetInputMute", {"inputName": name, "inputMuted": True}, check=False)
    for name, x in ((SCREEN, 0), (CAMERA, FIELD_W)):
        item = o.request("GetSceneItemId", {"sceneName": NAME, "sourceName": name})["sceneItemId"]
        o.request("SetSceneItemTransform", {"sceneName": NAME, "sceneItemId": item, "sceneItemTransform": {
            "positionX": x, "positionY": 0, "alignment": 5,
            "boundsType": "OBS_BOUNDS_SCALE_INNER", "boundsWidth": FIELD_W, "boundsHeight": FIELD_H,
            "boundsAlignment": 0}})


def crop_screen(o: remote.Obs, width: int, height: int, margins: dict | None) -> tuple[int, int]:
    """Crop the margins (e.g. the taskbar) off the screen and place it top left in its field.

    Returns the size of the screen image on the canvas, rounded down to even numbers.
    """
    m = margins or {"top": 0, "bottom": 0, "left": 0, "right": 0}
    w, h = width - m["left"] - m["right"], height - m["top"] - m["bottom"]
    s = min(FIELD_W / w, FIELD_H / h)
    item = o.request("GetSceneItemId", {"sceneName": NAME, "sourceName": SCREEN})["sceneItemId"]
    o.request("SetSceneItemTransform", {"sceneName": NAME, "sceneItemId": item, "sceneItemTransform": {
        "positionX": 0, "positionY": 0, "alignment": 5,
        "cropTop": m["top"], "cropBottom": m["bottom"], "cropLeft": m["left"], "cropRight": m["right"],
        "boundsType": "OBS_BOUNDS_SCALE_INNER", "boundsWidth": FIELD_W, "boundsHeight": FIELD_H,
        "boundsAlignment": 5}})         # top left, so splitting only drops the right and bottom
    return int(w * s) // 2 * 2, int(h * s) // 2 * 2


def _items(o: remote.Obs, source: str, prop: str) -> list[tuple[str, str]]:
    items = o.request("GetInputPropertiesListPropertyItems", {"inputName": source, "propertyName": prop})["propertyItems"]
    return [(i["itemName"], i["itemValue"]) for i in items
            if i.get("itemEnabled", True) and i.get("itemValue") not in ("", None)]


def devices(o: remote.Obs) -> dict[str, list[tuple[str, str]]]:
    """What OBS currently sees: (name, id) per screen, camera and microphone."""
    return {"screen": _items(o, SCREEN, "monitor_id"),
            "camera": _items(o, CAMERA, "video_device_id"),
            "microphone": _items(o, MICROPHONE, "device_id")}


def apply(o: remote.Obs, screen: str | None = None, camera: str | None = None, microphone: str | None = None) -> None:
    """Set device ids; camera == NO_CAMERA hides the webcam."""
    if screen:
        o.request("SetInputSettings", {"inputName": SCREEN, "inputSettings": {"monitor_id": screen}})
    if camera:
        item = o.request("GetSceneItemId", {"sceneName": NAME, "sourceName": CAMERA})["sceneItemId"]
        enabled = camera != NO_CAMERA
        if enabled:
            o.request("SetInputSettings", {"inputName": CAMERA, "inputSettings": {
                "video_device_id": camera, "res_type": 1, "resolution": "1920x1080"}})
        o.request("SetSceneItemEnabled", {"sceneName": NAME, "sceneItemId": item, "sceneItemEnabled": enabled})
    if microphone:
        o.request("SetInputSettings", {"inputName": MICROPHONE, "inputSettings": {"device_id": microphone}})
        o.request("SetInputMute", {"inputName": MICROPHONE, "inputMuted": False})
