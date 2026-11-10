"""The pipeline on the server (phase 3, 2026-11-09): activity without titles, worker, actions, import."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import pipeline_runner  # noqa: E402
from flex3_light_server import profiles  # noqa: E402
from flex3_light_server.accounts import Accounts  # noqa: E402
from flex3_light_server.admin import main as admin  # noqa: E402
from flex3_light_server.app import create_app  # noqa: E402
from pipeline import cut_rules, edit  # noqa: E402
from test_server import clip, put, session_json  # noqa: E402

ONE, TWO = "lecturer-one-token", "lecturer-two-token"
TOKENS = {ONE: {"device_id": "flex3-light-laptop", "user_id": "lecturer1", "user_name": "Lecturer One"},
          TWO: {"device_id": "flex3-light-other", "user_id": "lecturer2", "user_name": "Lecturer Two"}}
AUTH, AUTH2 = {"Authorization": f"Bearer {ONE}"}, {"Authorization": f"Bearer {TWO}"}
LOG = [[0.0, 30.0, "POWERPNT.EXE", "Vorlesung 5 - PowerPoint"],
       [30.0, 40.0, "msedge.exe", "Posteingang - Nicolas - Outlook im Web"],
       [40.0, 60.0, "Code.exe", "core.py - flex3-light"]]


def prefer(data: Path, user: str, **values) -> None:
    """Set preferences of a lecturer's profile on the server, as the app's sync would."""
    store = profiles.Store(data / "profiles")
    modules, prefs = store.load(user)
    prefs.update({k: {"value": v, "updated_at": time.time()} for k, v in values.items()})
    store.save(user, modules, prefs)


def test_window_titles_never_leave_the_device():
    titles = cut_rules.DEFAULT_POLICY["cut_titles"]
    public = cut_rules.without_titles(LOG, titles)
    assert public == [[0.0, 30.0, "POWERPNT.EXE", ""], [30.0, 40.0, "msedge.exe", "posteingang"],
                      [40.0, 60.0, "Code.exe", ""]]
    policy = cut_rules.policy({})
    assert [x["start"] for x in cut_rules.programs(public, policy)] == \
        [x["start"] for x in cut_rules.programs(LOG, policy)]                 # the rule finds the same spans


def upload(client, sid, clips, activity):
    assert client.put(f"/sessions/{sid}", headers=AUTH).status_code == 200
    for i, c in enumerate(clips):
        assert put(client, "mix", i, c, sid=sid, headers=AUTH).status_code == 200
    return client.post(f"/sessions/{sid}/complete", headers=AUTH,
                       json={"session": session_json(sid), "parts": {"mix": len(clips)}, "activity": activity})


def test_the_activity_arrives_and_the_profile_decides_what_starts(tmp_path):
    clips = [clip(tmp_path / f"{i}.mp4", 1, 440) for i in range(2)]
    data = tmp_path / "server"
    client = TestClient(create_app(data, TOKENS, start_worker=False))
    worker = client.app.state.worker
    sid = "2026-11-09_test_1000"
    titled = {"activity": LOG, "private": []}
    assert upload(client, sid, clips, titled).status_code == 422             # a whole title is refused
    public = {"activity": cut_rules.without_titles(LOG, cut_rules.DEFAULT_POLICY["cut_titles"]),
              "private": [[12.0, 15.5]]}
    assert upload(client, sid, clips, public).status_code == 202
    folder = data / "sessions" / sid
    end = time.time() + 30
    while not folder.exists() and time.time() < end:
        time.sleep(0.2)
    log = json.loads((folder / "activity.json").read_text(encoding="utf-8"))
    assert log["activity"][1] == [30.0, 40.0, "msedge.exe", "posteingang"] and log["private"] == [[12.0, 15.5]]
    assert json.loads((folder / "session.json").read_text(encoding="utf-8"))["instructor_id"] == "lecturer1"
    assert not worker.runner.queued(folder)                  # the profile says nothing: nothing starts
    prefer(data, "lecturer1", after_recording="transcribe")  # since phase 5 without "pipeline": only the server works on it
    worker.arrived(folder)
    assert worker.runner.queued(folder)


class FakeTranscriber:
    name = "fake"


def test_the_pipeline_runs_on_the_server_for_its_lecturer(tmp_path, monkeypatch):
    data = tmp_path / "server"
    client = TestClient(create_app(data, TOKENS, keys=lambda service: f"{service}-key", start_worker=False))
    worker = client.app.state.worker
    sid = "2026-11-09_lifi_1400"
    folder = data / "sessions" / sid
    folder.mkdir(parents=True)
    (folder / "session.json").write_text(json.dumps({**session_json(sid), "course_name": "LiFi",
                                                     "instructor_id": "lecturer1"}), encoding="utf-8")
    store = profiles.Store(data / "profiles")
    store.save("lecturer1", {"m1": {"id": "m1", "name": "LiFi", "terms": ["Li-Fi", "VLC"], "deleted": False,
                                    "updated_at": 1.0}}, {})
    prefer(data, "lecturer1", **{"cut.pause_min": 90.0})
    seen = {}

    def transcribe_run(f, t, terms, progress=None):
        seen["terms"], seen["transcriber"] = terms, t.name
        (f / "transcript.json").write_text("{}", encoding="utf-8")

    def cut_run(f, key, listener, settings, progress=None, config=None):
        seen["cut"] = (key("openai"), settings["pause_min"], settings["auto"])
        seen["model"] = config
        e = edit.new(60.0)
        e["items"].append(edit.item("cut", 1.0, 2.0, "Mikrofontest", "agent", 0.9, "accepted", "technical"))
        edit.save(f, e)
        return {"agent": {"model": "fake"}}

    def render_run(f, video_args, progress=None, sound_dir=None):
        seen["video_args"] = video_args
        report = {"signature": pipeline_runner.render.signature(edit.load(f)), "files": ["edited.mp4"]}
        (f / "edited.mp4").write_bytes(b"video")
        (f / "render.json").write_text(json.dumps(report), encoding="utf-8")
        return report

    def summary_run(f, model, progress=None):
        (f / "summary.pdf").write_bytes(b"%PDF")
        report = {"created": "2026-11-09T15:00:00+01:00", "model": "m",
                  "signature": json.loads((f / "render.json").read_text(encoding="utf-8"))["signature"]}
        (f / "summary.json").write_text(json.dumps(report), encoding="utf-8")
        return report

    monkeypatch.setattr(worker.runner, "_transcribers", lambda: [FakeTranscriber()])
    monkeypatch.setattr(pipeline_runner.transcribe, "run", transcribe_run)
    monkeypatch.setattr(pipeline_runner.transcribe, "assemblyai_base", lambda key: None)
    monkeypatch.setattr(pipeline_runner.subtitles, "run", lambda f: None)
    monkeypatch.setattr(pipeline_runner.cut_agent, "run", cut_run)
    monkeypatch.setattr(pipeline_runner.screening, "run", lambda f, listener, settings, progress=None: (f / "privacy.json").write_text("{}", encoding="utf-8"))
    monkeypatch.setattr(pipeline_runner.outline, "run", lambda f, key, settings, progress=None, config=None: (f / "outline.json").write_text('{"phases": []}', encoding="utf-8"))
    monkeypatch.setattr(pipeline_runner.render, "run", render_run)
    monkeypatch.setattr(pipeline_runner.summary, "run", summary_run)
    worker.runner.video_args = ["-c:v", "libx264"]

    def work():
        for _ in range(20):
            if not worker.runner.queued(folder):
                break
            worker.runner._process(folder)
        return client.get(f"/sessions/{sid}/pipeline", headers=AUTH).json()

    assert client.get(f"/sessions/{sid}/pipeline", headers=AUTH2).status_code == 404     # not theirs
    assert client.post(f"/sessions/{sid}/pipeline", headers=AUTH, json={"action": "cut"}).status_code == 409
    assert client.post(f"/sessions/{sid}/pipeline", headers=AUTH, json={"action": "x"}).status_code == 422
    assert client.post(f"/sessions/{sid}/pipeline", headers=AUTH, json={"action": "transcribe"}).json()["queued"]
    state = work()
    assert state["steps"]["subtitles"]["state"] == "done" and "cut" not in state["steps"]   # no cut by itself
    assert seen["terms"] == ["Li-Fi", "VLC"]                                 # from the lecturer's module
    client.post(f"/sessions/{sid}/pipeline", headers=AUTH, json={"action": "cut"})
    state = work()
    assert seen["cut"] == ("openai-key", 90.0, False)                        # the project's key, the lecturer's settings
    assert seen["model"]["model"] == "gpt-6.1-sol"                            # no models.json: the default
    (data / "models.json").write_text(json.dumps({"cut": {"model": "gpt-6.1-mini"}}), encoding="utf-8")
    assert worker.runner.models()["cut"]["model"] == "gpt-6.1-mini"           # the server's choice, read anew
    assert state["status"] == "Schnitt vorgeschlagen, wartet auf deine Freigabe"
    r = client.post(f"/sessions/{sid}/pipeline", headers=AUTH, json={"action": "approve"})
    assert r.status_code == 200 and r.json()["queued"]
    state = work()
    assert state["steps"]["render"]["state"] == "done" and state["steps"]["summary"]["state"] == "done"
    assert seen["video_args"] == ["-c:v", "libx264"] and not state["queued"]


def test_a_recording_filed_before_the_server_is_imported_without_titles(tmp_path):
    data = tmp_path / "server"
    accounts = Accounts(data / "accounts.sqlite")
    accounts.add_user("Lecturer One", user_id="lecturer1")
    accounts.close()
    local = tmp_path / "2026-11-04 - Vorlesung"
    local.mkdir()
    sid = "2026-11-04_dup_0815"
    (local / "session.json").write_text(json.dumps(session_json(sid)), encoding="utf-8")
    (local / "recording.mp4").write_bytes(b"video")
    (local / "transcript.json").write_text("{}", encoding="utf-8")            # a local result stays behind
    (local / "activity.json").write_text(json.dumps({"activity": LOG, "private": [[1, 2]]}), encoding="utf-8")
    admin(["--data", str(data), "import", str(local), "--user", "Lecturer One"])
    target = data / "sessions" / sid
    assert sorted(p.name for p in target.iterdir()) == ["activity.json", "recording.mp4", "session.json"]
    text = (target / "activity.json").read_text(encoding="utf-8")
    assert "Outlook im Web" not in text and "Vorlesung 5" not in text and "posteingang" in text
    assert json.loads((target / "session.json").read_text(encoding="utf-8"))["instructor_id"] == "lecturer1"
    with pytest.raises(SystemExit):
        admin(["--data", str(data), "import", str(local), "--user", "lecturer1"])     # only once
