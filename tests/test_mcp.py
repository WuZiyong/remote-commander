import asyncio

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from remote_commander.config import Device, GatewayConfig
from remote_commander.runtime import Gateway


def test_auth_origin_and_host(live_agent):
    url, token = live_agent
    assert httpx.post(url).status_code == 401
    assert httpx.post(url, headers={"Authorization": "Bearer wrong"}).status_code == 401
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    assert httpx.post(url, headers={**headers, "Origin": "https://evil.test"}).status_code == 403
    assert httpx.post(url, headers={**headers, "Host": "evil.test"}).status_code == 421
    assert httpx.post(url, headers=headers, content=b"x" * (4 * 1024 * 1024 + 1)).status_code == 413


def test_official_mcp_client_and_gateway(live_agent, monkeypatch):
    url, token = live_agent
    monkeypatch.setenv("RC_TEST_AGENT", token)

    async def scenario():
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as client:
            async with streamable_http_client(url, http_client=client) as (r, w, _):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    names = {t.name for t in (await session.list_tools()).tools}
                    assert {"read_file", "start_process", "start_search", "list_devices"} <= names
                    created = await session.call_tool(
                        "write_file", {"path": "mcp.txt", "content": "protocol works"}
                    )
                    assert not created.isError
                    denied = await session.call_tool("read_file", {"path": "../not-allowed"})
                    assert denied.isError
        gateway = Gateway(
            GatewayConfig(devices=[Device(id="a", url=url, token_env="RC_TEST_AGENT")])
        )
        read = await gateway.call("read_file", {"path": "mcp.txt"}, "a")
        assert read["content"] == "protocol works"
        devices = await gateway.call("list_devices", {})
        assert devices["devices"][0]["online"]
        with pytest.raises(ValueError, match="Unknown deviceId"):
            await gateway.call("read_file", {}, "missing")
        gateway.devices["b"] = Device(id="b", url=url, token_env="RC_TEST_AGENT")
        with pytest.raises(ValueError, match="deviceId is required"):
            await gateway.call("who_am_i", {})

    asyncio.run(scenario())


def test_stdio_agent(tmp_path):
    import json
    import os
    import sys

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"roots": [str(tmp_path)]}))

    async def scenario():
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "remote_commander",
                "agent",
                "--config",
                str(config),
                "--transport",
                "stdio",
            ],
            env={"PYTHONPATH": os.path.abspath("src")},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("who_am_i", {})
                assert not result.isError and result.structuredContent["name"] == "local"

    asyncio.run(scenario())
