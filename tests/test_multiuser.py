"""Adversarial isolation tests against the actual ASGI OAuth and MCP endpoints."""

import asyncio
import base64
import hashlib
import json
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient
from test_registry import key

from remote_commander.accounts import Accounts
from remote_commander.config import Device, GatewayConfig
from remote_commander.portal import Portal
from remote_commander.registry import Registry


@pytest.fixture
def portal(tmp_path, monkeypatch):
    if sys.platform == "win32":
        monkeypatch.setattr(Registry, "sync_keys", lambda self: None)
    registry = Registry.initialize(
        tmp_path / "devices.sqlite",
        host="gateway.example",
        port=2222,
        user="gateway",
        host_key=key(3),
        authorized_keys=tmp_path / "authorized_keys",
        python=sys.executable,
    )
    config = GatewayConfig(
        registry_path=str(registry.path),
        devices=[Device(id="aip", url="http://127.0.0.1:8765/mcp", token_env="RC_LEGACY_TOKEN")],
    )
    app = Portal(config, tmp_path / "accounts", "http://127.0.0.1", registration_open=True)
    yield app


@pytest.fixture
def client(portal):
    with TestClient(portal.app(), base_url=portal.issuer, follow_redirects=False) as client:
        yield client


def signup(client, username="alice"):
    response = client.get("/signup")
    csrf = re.search('name="csrf" value="([^"]+)"', response.text)[1]
    response = client.post(
        "/signup",
        data={"csrf": csrf, "username": username, "password": "correct horse battery staple"},
    )
    assert response.status_code == 303, response.text


def form(client, path, data=None):
    response = client.get("/")
    csrf = re.search('name="csrf" value="([^"]+)"', response.text)[1]
    return client.post(path, data={"csrf": csrf, **(data or {})})


def enrollment(portal, owner, name="laptop", seed=4):
    with __import__("tempfile").TemporaryDirectory() as temp:
        target = Path(temp) / "invite.json"
        portal.registry.invite(name, target, owner=owner)
        invite = json.loads(target.read_text())
    request = dict(
        owner=owner, secret=invite["secret"], public_key=key(seed), agent_token=str(seed) * 48
    )
    accepted = portal.registry.accept(invite["invite_id"], request)
    portal.registry.heartbeat(accepted["device_id"], "connection", opening=True)
    return accepted, invite, request


def test_separate_users_and_legacy_devices(portal, monkeypatch):
    alice = portal.accounts.register("alice", "correct horse battery staple")
    bob = portal.accounts.register("bob", "correct horse battery staple")
    a, invite, request = enrollment(portal, alice)
    b, _, _ = enrollment(portal, bob, seed=5)
    assert a["device_id"] != b["device_id"]
    with pytest.raises(PermissionError):
        portal.registry.accept(invite["invite_id"], {**request, "owner": bob})
    assert set(portal.registry.devices(alice)) == {a["device_id"]}
    assert set(portal.registry.devices(bob)) == {b["device_id"]}
    # Legacy gateway never discovers new users, including default routing.
    assert set(portal.gateway.devices) == {"aip"}

    async def remote(device, name, arguments):
        return {"name": device.id}

    monkeypatch.setattr(portal.gateway, "remote", remote)
    for attacker in (alice, bob):
        for legacy in ("aip", "mac", "comput1", "zywu"):
            with pytest.raises(ValueError, match="Unknown deviceId"):
                asyncio.run(
                    portal.gateway.call_for_owner(
                        attacker, "read_file", {"path": "/secret"}, legacy
                    )
                )
    with pytest.raises(ValueError):
        asyncio.run(
            portal.gateway.call_for_owner(
                bob, "force_terminate", {"session_id": "stolen"}, a["device_id"]
            )
        )
    assert (
        asyncio.run(portal.gateway.call_for_owner(alice, "who_am_i", {}, a["device_id"]))["name"]
        == a["device_id"]
    )
    with pytest.raises(ValueError):
        portal.registry.revoke(a["device_id"], owner=bob)
    assert a["device_id"] in portal.registry.devices(alice)
    portal.registry.revoke(a["device_id"], owner=alice)
    assert not portal.registry.devices(alice)
    assert b["device_id"] in portal.registry.devices(bob)


def test_eight_character_password_signup_and_login(client):
    password = "Ab3!xYz9"
    page = client.get("/signup")
    assert 'minlength="8"' in page.text
    csrf = re.search('name="csrf" value="([^"]+)"', page.text)[1]
    response = client.post(
        "/signup", data={"csrf": csrf, "username": "eightchars", "password": password[:-1]}
    )
    assert response.status_code == 400
    response = client.post(
        "/signup", data={"csrf": csrf, "username": "eightchars", "password": password}
    )
    assert response.status_code == 303, response.text
    assert form(client, "/logout").status_code == 303

    page = client.get("/login")
    assert 'minlength="8"' in page.text
    csrf = re.search('name="csrf" value="([^"]+)"', page.text)[1]
    response = client.post(
        "/login", data={"csrf": csrf, "username": "eightchars", "password": password}
    )
    assert response.status_code == 303, response.text
    assert client.get("/").status_code == 200


def test_self_service_csrf_and_foreign_revoke(client, portal):
    signup(client)
    owner = portal.session(
        __import__("starlette").requests.Request(
            {
                "type": "http",
                "headers": [(b"cookie", ("rc_session=" + client.cookies["rc_session"]).encode())],
            }
        )
    )["owner"]
    response = client.post("/devices", data={"device": "laptop"})
    assert response.status_code == 403
    response = form(client, "/devices", {"device": "laptop", "owner": "forged"})
    assert response.status_code == 200 and response.headers["content-disposition"]
    invite = response.json()
    assert invite["owner"] == owner
    portal.registry.accept(
        invite["invite_id"],
        {"owner": owner, "secret": invite["secret"], "public_key": key(9), "agent_token": "a" * 48},
    )
    foreign = portal.accounts.register("bob", "correct horse battery staple")
    b, _, _ = enrollment(portal, foreign, seed=6)
    assert form(client, "/devices/revoke", {"device": b["device_id"]}).status_code == 400
    assert b["device_id"] in portal.registry.devices(foreign)
    assert form(client, "/devices/revoke", {"device": invite["device_id"]}).status_code == 303


def oauth_start(client, verifier="a" * 64):
    registered = client.post(
        "/register",
        json={
            "redirect_uris": ["http://127.0.0.1:9009/callback"],
            "client_name": "Test client",
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": "devices",
        },
    )
    assert registered.status_code == 201, registered.text
    registered = registered.json()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    response = client.get(
        "/authorize",
        params={
            "client_id": registered["client_id"],
            "redirect_uri": "http://127.0.0.1:9009/callback",
            "response_type": "code",
            "scope": "devices",
            "state": "state-test",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": "http://127.0.0.1/mcp",
        },
    )
    assert response.status_code == 302, response.text
    return registered, response.headers["location"]


def oauth_approve(client, consent):
    page = client.get(consent)
    assert page.status_code == 200, page.text
    csrf = re.search('name="csrf" value="([^"]+)"', page.text)[1]
    redirect = client.post(consent, data={"csrf": csrf, "approve": "yes"})
    assert redirect.status_code == 303, redirect.text
    params = parse_qs(urlsplit(redirect.headers["location"]).query)
    assert params["state"] == ["state-test"]
    return params["code"][0]


def exchange(client, registration, code, verifier="a" * 64):
    return client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": registration["client_id"],
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": "http://127.0.0.1:9009/callback",
            "resource": "http://127.0.0.1/mcp",
        },
    )


def rpc(client, token, name, args=None):
    return client.post(
        "/mcp",
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args or {}},
        },
    )


def test_real_oauth_pkce_mcp_and_revocation(client, portal, monkeypatch):
    signup(client)
    registration, consent = oauth_start(client)
    code = oauth_approve(client, consent)
    assert exchange(client, registration, code, "b" * 64).status_code == 400
    token_response = exchange(client, registration, code)
    assert token_response.status_code == 200, token_response.text
    token = token_response.json()
    assert exchange(client, registration, code).status_code == 400  # no code replay
    response = rpc(client, token["access_token"], "list_devices")
    assert response.status_code == 200, response.text
    assert response.json()["result"]["structuredContent"]["devices"] == []
    for name, args in [
        ("read_file", {"deviceId": "mac", "path": "/secret"}),
        ("start_process", {"deviceId": "aip", "command": "id"}),
        ("get_more_search_results", {"deviceId": "comput1", "session_id": "stolen"}),
        ("read_process_output", {"deviceId": "zywu", "session_id": "stolen"}),
    ]:
        response = rpc(client, token["access_token"], name, args)
        assert response.status_code == 200
        assert response.json()["result"]["isError"]
    assert (
        client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1}).status_code
        == 401
    )
    forged = client.post(
        "/mcp",
        headers={
            "Authorization": "Bearer " + token["access_token"],
            "x-owner-id": portal.registry.settings()["owner"],
        },
        json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
    )
    assert forged.status_code == 403
    refreshed = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "client_id": registration["client_id"],
            "refresh_token": token["refresh_token"],
            "resource": "http://127.0.0.1/mcp",
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    assert rpc(client, token["access_token"], "list_devices").status_code == 401
    assert form(client, "/disconnect").status_code == 303
    assert rpc(client, refreshed.json()["access_token"], "list_devices").status_code == 401


def test_origin_host_body_and_login_redirect(client, portal):
    assert client.get("/", headers={"host": "evil.example"}).status_code == 421
    assert client.post("/login", headers={"origin": "https://evil.example"}).status_code == 403
    assert client.post("/login", content="x" * 65537).status_code == 413
    assert client.post("/devices").status_code == 403
    assert portal.next_path("https://evil.example") == "/"
    assert portal.next_path("//evil.example") == "/"
    assert client.get("/login").headers["content-security-policy"]
    csrf = client.cookies["rc_form"]
    assert "httponly" in client.get("/login").headers["set-cookie"].lower()
    response = client.post(
        "/signup",
        data={"csrf": csrf, "username": "someone", "password": "correct horse battery staple"},
    )
    assert response.status_code == 403  # cookie/form token mismatch


def test_tokens_hashed_expire_and_atomic_consumption(tmp_path):
    accounts = Accounts(tmp_path / "accounts.sqlite")
    token = accounts.issue("code", {"subject": "alice"}, 60)
    assert token.encode() not in accounts.path.read_bytes()
    with ThreadPoolExecutor(2) as executor:
        found = list(executor.map(lambda _: accounts.get("code", token, consume=True), range(2)))
    assert sum(value is not None for value in found) == 1
    expired = accounts.issue("session", {"owner": "alice"}, -1)
    assert accounts.get("session", expired) is None
    for _ in range(3):
        accounts.rate("ip", 3)
    with pytest.raises(PermissionError):
        accounts.rate("ip", 3)


def test_migration_preserves_legacy_and_is_idempotent(tmp_path):
    # Exact pre-migration layout, not a database created by the new implementation.
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE invites(id TEXT,device_id TEXT,secret_hash TEXT,public_key TEXT,
                                 expires REAL,used INTEGER);
            CREATE TABLE devices(id TEXT,public_key TEXT,token TEXT,port INTEGER,
                                 revoked INTEGER,connected INTEGER,last_seen REAL,
                                 connection_id TEXT,created REAL);
            INSERT INTO settings VALUES ('owner','legacy-owner');
            INSERT INTO devices VALUES ('mac','public','secret',30001,0,1,9999999999,'old',1);
        """)
    registry = Registry(path)
    registry.migrate_owners()
    registry.migrate_owners()
    assert registry.devices()["mac"].token() == "secret"
    assert registry.devices()["mac"].port == 30001
    assert registry.devices()["mac"].online
    assert registry.devices("someone-else") == {}
