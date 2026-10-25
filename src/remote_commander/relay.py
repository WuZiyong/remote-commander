"""Outbound encrypted bridge between an owner-private Sites MCP plugin and a gateway."""

import argparse
import asyncio
import base64
import json
import logging
import os
import signal

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dotenv import load_dotenv

from .config import GatewayConfig, read_config
from .runtime import Gateway

log = logging.getLogger("remote_commander.relay")


def encrypt(key: bytes, value: dict, aad: str) -> str:
    nonce = os.urandom(12)
    data = json.dumps(value, ensure_ascii=False).encode()
    return base64.b64encode(nonce + AESGCM(key).encrypt(nonce, data, aad.encode())).decode()


def decrypt(key: bytes, payload: str, aad: str) -> dict:
    data = base64.b64decode(payload, validate=True)
    return json.loads(AESGCM(key).decrypt(data[:12], data[12:], aad.encode()))


class Bridge:
    def __init__(self, site: str, gateway: Gateway, key: bytes, headers: dict):
        self.site = site.rstrip("/")
        self.gateway = gateway
        self.key = key
        self.headers = headers
        self.stopping = asyncio.Event()

    async def handle(self, client: httpx.AsyncClient, job: dict):
        try:
            request = decrypt(self.key, job["payload"], job["id"] + ":request")
            arguments = request.get("arguments", {})
            device_id = arguments.pop("deviceId", None)
            result = await self.gateway.call(request["name"], arguments, device_id)
            response = {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                "structuredContent": result,
                "isError": False,
            }
        except Exception as exc:
            # Actual tool validation errors are useful; do not include transport credentials.
            detail = (
                str(exc) if isinstance(exc, (ValueError, PermissionError)) else type(exc).__name__
            )
            response = {"content": [{"type": "text", "text": detail}], "isError": True}
        data = {
            "id": job["id"],
            "claim": job["claim"],
            "payload": encrypt(self.key, response, job["id"] + ":response"),
        }
        # Retry delivery of the SAME result, never the underlying file/command operation.
        for attempt in range(4):
            try:
                reply = await client.post(self.site + "/relay/complete", json=data)
                reply.raise_for_status()
                return
            except httpx.HTTPError:
                if attempt == 3:
                    log.error("Result delivery failed operation=%s", job["id"])
                    return
                await asyncio.sleep(1 + attempt)

    async def run(self):
        tasks: set[asyncio.Task] = set()
        async with httpx.AsyncClient(
            headers=self.headers, timeout=20, follow_redirects=False
        ) as client:
            failures = 0
            try:
                while not self.stopping.is_set():
                    try:
                        response = await client.post(
                            self.site + "/relay/poll", json={"capacity": 4 - len(tasks)}
                        )
                        response.raise_for_status()
                        for job in response.json().get("jobs", []):
                            task = asyncio.create_task(self.handle(client, job))
                            tasks.add(task)
                            task.add_done_callback(tasks.discard)
                        failures = 0
                        await asyncio.sleep(0.75)
                    except httpx.HTTPError:
                        failures += 1
                        log.warning("Relay unavailable; retry=%d", failures)
                        await asyncio.sleep(min(30, 2 ** min(failures, 5)))
            finally:
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                self.gateway.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", required=True)
    parser.add_argument("--gateway-config", required=True)
    parser.add_argument("--env-file", required=True)
    args = parser.parse_args()
    if not args.site.startswith("https://"):
        parser.error("Relay Site must use HTTPS")
    load_dotenv(args.env_file)
    key = base64.b64decode(os.environ["RC_RELAY_KEY"], validate=True)
    if len(key) != 32:
        parser.error("RC_RELAY_KEY must encode 32 bytes")
    gateway = Gateway(read_config(args.gateway_config, GatewayConfig))
    for device in gateway.devices.values():
        device.token()
    headers = {
        "Authorization": "Bearer " + os.environ["RC_RELAY_TOKEN"],
        "OAI-Sites-Authorization": "Bearer " + os.environ["RC_SITES_BYPASS_TOKEN"],
    }
    bridge = Bridge(args.site, gateway, key, headers)
    logging.basicConfig(level=logging.WARNING)

    async def serve():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, bridge.stopping.set)
            except NotImplementedError:
                pass
        await bridge.run()

    asyncio.run(serve())


if __name__ == "__main__":
    main()
