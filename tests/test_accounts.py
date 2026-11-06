"""Lecturers, invitation keys and devices: a key becomes a device token once (decision of 2026-11-06)."""
from __future__ import annotations

import datetime as dt
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT))
import server_client  # noqa: E402
from flex3_light_server import accounts as acc  # noqa: E402
from flex3_light_server.admin import main as admin  # noqa: E402
from flex3_light_server.app import create_app  # noqa: E402


@pytest.fixture
def accounts(tmp_path):
    a = acc.Accounts(tmp_path / "accounts.sqlite")
    yield a
    a.close()


def test_keys_are_forgiving_to_type_but_strict_in_form():
    key = acc.new_key()
    assert key.startswith("FLX3-") and len(key) == 24
    assert acc.normalize_key(key.lower().replace("-", " ")) == key
    assert acc.normalize_key(key.replace("FLX3-", "")) == key
    for bad in ("", "FLX3-AAAA", "FLX3-0000-0000-0000-0000"):              # too short, ambiguous characters
        with pytest.raises(acc.InvalidKey):
            acc.normalize_key(bad)
    assert acc.split_code(f"{key}@http://host:8765/") == (key, "http://host:8765")
    assert acc.split_code(key) == (key, None)


def test_a_key_works_once_and_gives_a_token_per_device(accounts):
    user = accounts.add_user("Lecturer One")
    key = accounts.invite(user["id"])
    token, who = accounts.register(key, "LAPTOP-7")
    assert who == {"device_id": "flex3-light-laptop-7", "user_id": user["id"], "user_name": "Lecturer One"}
    assert accounts.identity(token) == who
    with pytest.raises(acc.InvalidKey):
        accounts.register(key, "Office")                                    # used
    token2, who2 = accounts.register(accounts.invite(user["id"]), "Office")
    assert who2["device_id"] == "flex3-light-office" and accounts.identity(token2)["user_id"] == user["id"]
    assert token not in str(accounts.path.read_bytes())                    # only hashes are stored


def test_registering_the_same_device_again_keeps_its_id_and_drops_the_old_token(accounts):
    user = accounts.add_user("Lecturer One")
    old, first = accounts.register(accounts.invite(user["id"]), "Laptop")
    new, second = accounts.register(accounts.invite(user["id"]), "laptop")
    assert first["device_id"] == second["device_id"]
    assert accounts.identity(old) is None and accounts.identity(new)


def test_expired_keys_and_revoked_devices_stop_working(accounts):
    user = accounts.add_user("Lecturer One")
    with pytest.raises(acc.InvalidKey):
        accounts.register(accounts.invite(user["id"], days=-1), "Laptop")
    token, who = accounts.register(accounts.invite(user["id"]), "Laptop")
    assert accounts.revoke(who["device_id"]) and accounts.identity(token) is None
    assert not accounts.revoke(who["device_id"])


def test_endpoints_register_and_identify(tmp_path):
    client = TestClient(create_app(tmp_path))
    accounts = acc.Accounts(tmp_path / "accounts.sqlite")
    user = accounts.add_user("Lecturer One", user_id="lecturer1")
    key = accounts.invite(user["id"])
    assert client.post("/devices/register", json={"key": "FLX3-AAAA-AAAA-AAAA-AAAA", "device_name": "x"}).status_code == 403
    r = client.post("/devices/register", json={"key": key, "device_name": "Laptop"}).json()
    auth = {"Authorization": f"Bearer {r['token']}"}
    assert client.get("/me", headers=auth).json() == {"device_id": "flex3-light-laptop", "user_id": "lecturer1",
                                                       "user_name": "Lecturer One"}
    assert client.get("/profile", headers=auth).json()["user_id"] == "lecturer1"   # the profile is the lecturer's
    assert client.get("/me").status_code == 401
    accounts.revoke("flex3-light-laptop")
    assert client.get("/me", headers=auth).status_code == 401
    accounts.close()


def test_admin_prints_a_connection_code(tmp_path, capsys):
    admin(["--data", str(tmp_path), "--url", "http://127.0.0.1:8765", "invite", "Lecturer One"])
    code = capsys.readouterr().out.strip().splitlines()[-1]
    key, url = acc.split_code(code)
    assert url == "http://127.0.0.1:8765" and acc.normalize_key(key) == key
    admin(["--data", str(tmp_path), "invite", "Lecturer One"])              # the same lecturer, not a second one
    capsys.readouterr()
    admin(["--data", str(tmp_path), "users"])
    assert capsys.readouterr().out.count("Lecturer One") == 1


def test_the_app_registers_over_http(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(create_app(tmp_path), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    try:
        accounts = acc.Accounts(tmp_path / "accounts.sqlite")
        key = accounts.invite(accounts.add_user("Lecturer One")["id"])
        accounts.close()
        url = f"http://127.0.0.1:{port}"
        with pytest.raises(server_client.ServerError) as e:
            server_client.register(url, "FLX3-AAAA-AAAA-AAAA-AAAA", "Laptop")
        assert e.value.status == 403 and "abgelaufen" in str(e.value.detail)
        answer = server_client.register(url, key, "Laptop")
        assert server_client.ServerClient(url, answer["token"]).me()["user_name"] == "Lecturer One"
    finally:
        srv.should_exit = True


def test_the_connection_code_is_split_like_on_the_server():
    import core
    key = acc.new_key()
    assert core.server_api_split(f" {key}@https://flex3.example/ ") == (key, "https://flex3.example")
    assert core.server_api_split(key) == (key, "")


def test_dates_are_aware(accounts):
    user = accounts.add_user("Lecturer One")
    accounts.invite(user["id"])
    row = accounts.db.execute("SELECT expires FROM invites").fetchone()
    assert dt.datetime.fromisoformat(row["expires"]).tzinfo is not None
