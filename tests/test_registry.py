import asyncio
import base64
import json
import sys
import time
from pathlib import Path

import pytest

from remote_commander.config import Device, GatewayConfig
from remote_commander.connect import protocol_result, ssh_command, windows_task_xml
from remote_commander.registry import LEASE, Registry, public_key
from remote_commander.runtime import Gateway


def key(seed=0):
    return (
        "ssh-ed25519 "
        + base64.b64encode(
            b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes([seed]) * 32
        ).decode()
    )


@pytest.fixture
def registry(tmp_path, monkeypatch):
    if sys.platform == "win32":
        monkeypatch.setattr(Registry, "sync_keys", lambda self: None)
    authorized = tmp_path / "authorized_keys"
    authorized.write_text("# Existing administrator key\n" + key(2) + "\n")
    return Registry.initialize(
        tmp_path / "registry.sqlite",
        host="gateway.example",
        port=2222,
        user="owner",
        host_key=key(3),
        authorized_keys=authorized,
        python=sys.executable,
    )


def issue(registry, device_id="laptop"):
    output = registry.path.parent / (device_id + ".json")
    registry.invite(device_id, output)
    invite = json.loads(output.read_text())
    request = {
        "owner": invite["owner"],
        "secret": invite["secret"],
        "agent_token": "a" * 48,
        "public_key": key(4),
    }
    return invite, request


def test_enrollment_is_owner_bound_one_use_and_expires(registry):
    invite, request = issue(registry)
    with pytest.raises(PermissionError):
        registry.accept(invite["invite_id"], {**request, "owner": "another-account"})
    with pytest.raises(PermissionError):
        registry.accept(invite["invite_id"], {**request, "secret": "wrong"})
    accepted = registry.accept(invite["invite_id"], request)
    assert accepted["owner"] == registry.settings()["owner"]
    assert 30000 <= accepted["port"] < 31000
    with pytest.raises(PermissionError):
        registry.accept(invite["invite_id"], request)
    other, request = issue(registry, "expired")
    with registry.db() as db:
        db.execute("UPDATE invites SET expires=0 WHERE id=?", (other["invite_id"],))
    with pytest.raises(PermissionError):
        registry.accept(other["invite_id"], request)


def test_key_options_and_preservation(registry):
    if sys.platform == "win32":
        pytest.skip("authorized_keys mutation runs on the Linux gateway")
    target = Path(registry.settings()["authorized_keys"])
    original = target.read_text()
    invite, request = issue(registry)
    keys = target.read_text()
    assert keys.startswith(original)
    assert 'restrict,command="' in keys and " accept " in keys
    assert "port-forwarding" not in keys
    registry.accept(invite["invite_id"], request)
    keys = target.read_text()
    assert keys.startswith(original) and " accept " not in keys
    assert 'permitlisten="127.0.0.1:' in keys and 'permitopen="127.0.0.1:1"' in keys
    registry.sync_keys()
    assert target.read_text() == keys
    registry.revoke("laptop")
    assert target.read_text().startswith(original) and " channel " not in target.read_text()


def test_live_gateway_discovery_revoke_and_fencing(registry):
    gateway = Gateway(
        GatewayConfig(
            registry_path=str(registry.path),
            devices=[
                Device(id="laptop", url="http://127.0.0.1:8765/mcp", token_env="RC_OLD_TOKEN")
            ],
        )
    )
    invite, request = issue(registry)
    registry.accept(invite["invite_id"], request)
    # Same gateway instance observes enrollment, no restart or device config edit.
    device = gateway.devices["laptop"]
    assert not device.online and device.token() == request["agent_token"]
    registry.heartbeat("laptop", "old", opening=True)
    assert gateway.devices["laptop"].online
    registry.heartbeat("laptop", "new", opening=True)
    registry.disconnect("laptop", "old")
    assert gateway.devices["laptop"].online
    with pytest.raises(PermissionError):
        registry.heartbeat("laptop", "old")
    registry.revoke("laptop")
    assert "laptop" not in gateway.devices  # Revocation cannot resurrect the static route.
    with pytest.raises(PermissionError):
        registry.heartbeat("laptop", "new")
    with pytest.raises(ValueError, match="Unknown deviceId"):
        asyncio.run(gateway.call("who_am_i", {}, "laptop"))


def test_offline_lease_and_disconnect(registry):
    invite, request = issue(registry)
    registry.accept(invite["invite_id"], request)
    registry.heartbeat("laptop", "connection", opening=True)
    with registry.db() as db:
        db.execute("UPDATE devices SET last_seen=?", (time.time() - LEASE - 1,))
    assert not registry.devices()["laptop"].online
    gateway = Gateway(GatewayConfig(registry_path=str(registry.path)))
    with pytest.raises(ValueError, match="offline"):
        asyncio.run(gateway.call("who_am_i", {}, "laptop"))
    registry.heartbeat("laptop", "connection")
    registry.disconnect("laptop", "connection")
    assert not registry.devices()["laptop"].online


def test_reject_key_option_injection_and_unsafe_ids(registry):
    with pytest.raises(ValueError):
        public_key('command="sh" ' + key())
    with pytest.raises(ValueError):
        registry.invite("--bad\nname", registry.path.parent / "unsafe")
    assert registry.settings()["python"] == str(Path(sys.executable).absolute())


def test_client_pins_host_and_does_not_share_ssh_sessions(tmp_path):
    profile = dict(host="gateway.example", ssh_port=2222, ssh_user="owner", port=30001)
    command = ssh_command(profile, tmp_path, tmp_path / "device key", port=8765)
    assert "StrictHostKeyChecking=yes" in command
    assert "ControlPath=none" in command and "ForwardAgent=no" in command
    assert "127.0.0.1:30001:127.0.0.1:8765" in command
    assert (
        protocol_result('banner\n{"device_id":"laptop","owner":"account"}\n')["owner"] == "account"
    )


def test_windows_startup_is_per_user_and_has_no_runtime_limit():
    import xml.etree.ElementTree as et

    root = et.fromstring(
        windows_task_xml(["C:/Python/pythonw.exe", "-m", "module"], "S-1-5-21-123")
    )
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.find("t:Triggers/t:LogonTrigger/t:UserId", ns).text == "S-1-5-21-123"
    assert root.find("t:Principals/t:Principal/t:LogonType", ns).text == "InteractiveToken"
    assert root.find("t:Settings/t:ExecutionTimeLimit", ns).text == "PT0S"
    assert root.find("t:Settings/t:StopIfGoingOnBatteries", ns).text == "false"
    assert root.find("t:Settings/t:RestartOnFailure/t:Count", ns).text == "3"
    assert root.find("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", ns).text == "PT5M"
