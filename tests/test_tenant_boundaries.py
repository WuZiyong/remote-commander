"""Exercise every device-bound tool at the owner boundary, plus OAuth client separation."""

import asyncio

import pytest
from test_multiuser import client as client
from test_multiuser import (
    enrollment,
    exchange,
    oauth_approve,
    oauth_start,
    signup,
)
from test_multiuser import portal as portal

DEVICE_TOOLS = [
    "who_am_i",
    "get_config",
    "list_directory",
    "get_file_info",
    "read_file",
    "write_file",
    "edit_block",
    "create_directory",
    "move_file",
    "start_process",
    "read_process_output",
    "interact_with_process",
    "force_terminate",
    "list_sessions",
    "list_processes",
    "start_search",
    "get_more_search_results",
    "stop_search",
]


@pytest.mark.parametrize("tool", DEVICE_TOOLS)
def test_all_tools_reject_foreign_and_legacy_devices(portal, tool):
    alice = portal.accounts.register("alice", "correct horse battery staple")
    bob = portal.accounts.register("bob", "correct horse battery staple")
    a, _, _ = enrollment(portal, alice)
    for device in (a["device_id"], "mac", "aip", "comput1", "zywu"):
        with pytest.raises(ValueError, match="Unknown deviceId"):
            asyncio.run(portal.gateway.call_for_owner(bob, tool, {}, device))
    with pytest.raises(PermissionError):
        asyncio.run(portal.gateway.call_for_owner("", tool, {}, "aip"))


def test_oauth_code_cannot_be_exchanged_by_another_client(client, portal):
    signup(client)
    first, consent = oauth_start(client)
    code = oauth_approve(client, consent)
    second, _ = oauth_start(client)
    assert exchange(client, second, code).status_code == 400
    issued = exchange(client, first, code)
    assert issued.status_code == 200
    token = issued.json()["access_token"]
    owner = portal.accounts.get("access", token)["subject"]
    with portal.accounts.db() as db:
        db.execute("UPDATE users SET active=0 WHERE id=?", (owner,))
    assert asyncio.run(portal.provider.load_access_token(token)) is None


def test_issuer_resource_mismatch_and_unregistered_redirect(client):
    signup(client)
    registration, _ = oauth_start(client)
    args = {
        "client_id": registration["client_id"],
        "response_type": "code",
        "scope": "devices",
        "code_challenge": "a" * 43,
        "code_challenge_method": "S256",
        "redirect_uri": "https://attacker.example/callback",
    }
    assert client.get("/authorize", params=args).status_code == 400
    args["redirect_uri"] = "http://127.0.0.1:9009/callback"
    args["resource"] = "https://other.example/mcp"
    assert client.get("/authorize", params=args).status_code == 302
    # Error redirects to the validated callback; it never creates an approval flow.
    assert "error=" in client.get("/authorize", params=args).headers["location"]


def test_password_storage_closed_registration_and_session_separation(portal):
    password = "long and unique password"
    owner = portal.accounts.register("alice", password)
    assert password.encode() not in portal.accounts.path.read_bytes()
    assert portal.accounts.login("ALICE", password) == owner
    with pytest.raises(PermissionError):
        portal.accounts.login("alice", "a different long password")
    with pytest.raises(ValueError):
        portal.accounts.register("ALICE", password)
    portal.registration_open = False
    from starlette.testclient import TestClient

    with TestClient(portal.app(), base_url=portal.issuer) as browser:
        assert "disabled" in browser.get("/signup").text
        assert (
            browser.post("/signup", data={"username": "bob", "password": password}).status_code
            == 200
        )
        assert browser.get("/").status_code == 200


def test_secure_cookie_names_and_flags(portal):
    from starlette.testclient import TestClient

    portal.issuer = "https://gateway.example"
    portal.secure = True
    portal.cookie_prefix = "__Host-"
    with TestClient(portal.app(), base_url=portal.issuer) as browser:
        response = browser.get("/login")
        cookie = response.headers["set-cookie"]
        assert "__Host-rc_form=" in cookie
        assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
        assert "Domain=" not in cookie
