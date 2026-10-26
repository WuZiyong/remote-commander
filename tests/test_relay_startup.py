import asyncio
import importlib.util
from pathlib import Path

import httpx
import pytest
from cryptography.exceptions import InvalidTag

from remote_commander.relay import Bridge, decrypt, encrypt


def test_crontab_preserves_existing_jobs():
    source = Path(__file__).parents[1] / "scripts/install_linux_service.py"
    spec = importlib.util.spec_from_file_location("installer", source)
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    original = "# Existing jobs\n@reboot /safe/existing-job\n*/3 * * * * /scientific/scheduler\n"
    updated = installer.merge_crontab(original, "/our/supervisor")
    assert updated.startswith(original)
    assert updated.count("@reboot /our/supervisor") == 1
    assert installer.merge_crontab(updated, "/our/supervisor") == updated
    bridge = installer.merge_crontab(updated, "/our/bridge", "# BEGIN BRIDGE", "# END BRIDGE")
    assert "@reboot /our/supervisor" in bridge and "@reboot /our/bridge" in bridge
    assert (
        installer.merge_crontab(bridge, "/our/bridge", "# BEGIN BRIDGE", "# END BRIDGE") == bridge
    )
    tunnel = installer.merge_crontab(bridge, "/our/tunnel", "# BEGIN TUNNEL", "# END TUNNEL")
    replaced = installer.merge_crontab(tunnel, "/our/new-tunnel", "# BEGIN TUNNEL", "# END TUNNEL")
    assert original in replaced and "@reboot /our/bridge" in replaced
    assert "@reboot /our/tunnel" not in replaced
    assert replaced.count("@reboot /our/new-tunnel") == 1
    with pytest.raises(ValueError):
        installer.merge_crontab(updated + installer.BEGIN, "/x")


def test_bridge_aead_and_delivery_retries_do_not_execute_twice():
    key = b"\x11" * 32
    value = {"name": "list_devices", "arguments": {}}
    payload = encrypt(key, value, "job:request")
    assert decrypt(key, payload, "job:request") == value
    with pytest.raises(InvalidTag):
        decrypt(key, payload, "wrong-job:request")

    class Gateway:
        calls = 0

        async def call(self, name, arguments, device_id):
            self.calls += 1
            return {"devices": []}

    gateway = Gateway()
    deliveries = []

    def transport(request):
        deliveries.append(request)
        if len(deliveries) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"accepted": True})

    async def scenario():
        bridge = Bridge("https://private.example", gateway, key, {})
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await bridge.handle(client, {"id": "job", "claim": "claim", "payload": payload})

    asyncio.run(scenario())
    assert gateway.calls == 1 and len(deliveries) == 2
    assert deliveries[0].content == deliveries[1].content
