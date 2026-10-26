"""Exercise separate real HTTP agents through the shared gateway and OAuth boundary."""

import json
import socket
import threading
import time
from contextlib import contextmanager

import uvicorn
from test_multiuser import client as client
from test_multiuser import (
    enrollment,
    exchange,
    form,
    oauth_approve,
    oauth_start,
    rpc,
    signup,
)
from test_multiuser import portal as portal

from remote_commander.config import AgentConfig
from remote_commander.runtime import Runtime
from remote_commander.security import Guard
from remote_commander.server import create_http_app, create_server


@contextmanager
def agent(root, name):
    root.mkdir()
    runtime = Runtime(AgentConfig(name=name, roots=[str(root)], allow_shell=True))
    token = name * 48
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    mcp = create_server(runtime)
    server = uvicorn.Server(
        uvicorn.Config(
            Guard(create_http_app(mcp, runtime), token),
            log_level="error",
            access_log=False,
            timeout_graceful_shutdown=2,
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    try:
        yield sock.getsockname()[1], token
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()


def get_token(client, username):
    signup(client, username)
    registration, consent = oauth_start(client)
    code = oauth_approve(client, consent)
    return exchange(client, registration, code).json()["access_token"]


def test_real_agents_do_not_share_files_process_sessions_or_discovery(client, portal, tmp_path):
    alice_token = get_token(client, "alice")
    alice = portal.accounts.get("access", alice_token)["subject"]
    assert form(client, "/logout").status_code == 303
    bob_token = get_token(client, "bob")
    bob = portal.accounts.get("access", bob_token)["subject"]
    a, _, _ = enrollment(portal, alice, seed=4)
    b, _, _ = enrollment(portal, bob, seed=5)
    root_a, root_b = tmp_path / "alice-files", tmp_path / "bob-files"
    with agent(root_a, "alice") as agent_a, agent(root_b, "bob") as agent_b:
        with portal.registry.db() as db:
            for device, (port, token) in [(a, agent_a), (b, agent_b)]:
                db.execute(
                    "UPDATE devices SET port=?,token=? WHERE id=?",
                    (port, token, device["device_id"]),
                )

        def result(token, name, args=None):
            response = rpc(client, token, name, args)
            assert response.status_code == 200, response.text
            return response.json()["result"]

        for token, own, foreign in [(alice_token, a, b), (bob_token, b, a)]:
            listed = result(token, "list_devices")["structuredContent"]["devices"]
            assert [d["id"] for d in listed] == [own["device_id"]]
            assert listed[0]["online"]
            assert result(token, "who_am_i", {"deviceId": foreign["device_id"]})["isError"]
        response = result(
            alice_token,
            "write_file",
            {
                "deviceId": a["device_id"],
                "path": str(root_a / "private.txt"),
                "content": "alice only",
            },
        )
        assert not response.get("isError")
        assert (root_a / "private.txt").read_text() == "alice only"
        assert result(
            bob_token,
            "read_file",
            {"deviceId": a["device_id"], "path": str(root_a / "private.txt")},
        )["isError"]
        assert result(
            bob_token,
            "read_file",
            {"deviceId": b["device_id"], "path": str(root_a / "private.txt")},
        )["isError"]
        started = result(
            alice_token,
            "start_process",
            {
                "deviceId": a["device_id"],
                "command": "echo isolated-session",
                "cwd": str(root_a),
                "timeout_ms": 1000,
            },
        )
        session_id = started["structuredContent"]["session_id"]
        assert result(
            bob_token, "read_process_output", {"deviceId": a["device_id"], "session_id": session_id}
        )["isError"]
        assert result(
            bob_token, "force_terminate", {"deviceId": b["device_id"], "session_id": session_id}
        )["isError"]
        search = result(
            alice_token,
            "start_search",
            {"deviceId": a["device_id"], "path": str(root_a), "pattern": "alice"},
        )
        search_id = search["structuredContent"]["session_id"]
        assert result(
            bob_token,
            "get_more_search_results",
            {"deviceId": b["device_id"], "session_id": search_id},
        )["isError"]
        portal.registry.revoke(a["device_id"], owner=alice)
        assert result(alice_token, "list_devices")["structuredContent"]["devices"] == []
        assert result(
            alice_token,
            "read_file",
            {"deviceId": a["device_id"], "path": str(root_a / "private.txt")},
        )["isError"]
        assert result(bob_token, "list_devices")["structuredContent"]["devices"][0]["online"]
    # Neither token nor pairing bootstrap credentials are written into audit output.
    assert "bootstrap_key" not in json.dumps(portal.gateway.devices, default=str)
