"""Device runtime and MCP-to-MCP routing; no external relay service."""

from __future__ import annotations

import asyncio
import getpass
import platform
import socket
import sys

import anyio
import httpx
import psutil
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .config import AgentConfig, GatewayConfig
from .files import Files
from .processes import Processes
from .search import Searches


class Runtime:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.files = Files(config)
        self.processes = Processes(self.files)
        self.searches = Searches(self.files)
        self.methods = {
            name: getattr(owner, name)
            for owner, names in [
                (
                    self.files,
                    [
                        "list_directory",
                        "get_file_info",
                        "read_file",
                        "write_file",
                        "edit_block",
                        "create_directory",
                        "move_file",
                    ],
                ),
                (
                    self.processes,
                    [
                        "start_process",
                        "read_process_output",
                        "interact_with_process",
                        "force_terminate",
                        "list_sessions",
                    ],
                ),
                (self.searches, ["start_search", "get_more_search_results", "stop_search"]),
                (self, ["who_am_i", "get_config", "list_processes"]),
            ]
            for name in names
        }

    def who_am_i(self) -> dict:
        return {
            "name": self.config.name,
            "hostname": socket.gethostname(),
            "user": getpass.getuser(),
            "system": platform.system(),
            "python": platform.python_version(),
            "python_executable": sys.executable,
        }

    def get_config(self) -> dict:
        return self.config.model_dump()

    def list_processes(self) -> dict:
        processes = []
        for process in psutil.process_iter(["pid", "name", "username", "status"]):
            try:
                processes.append(process.info)
                if len(processes) == 2000:
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return {"processes": processes, "limit": 2000}

    async def call(self, name: str, arguments: dict, device_id: str | None = None) -> dict:
        if device_id is not None and device_id != self.config.name:
            raise ValueError(f"Unknown deviceId: {device_id}")
        if name == "list_devices":
            return {"devices": [{"id": self.config.name, "online": True, **self.who_am_i()}]}
        return await anyio.to_thread.run_sync(lambda: self.methods[name](**arguments))

    def close(self):
        self.searches.close()
        self.processes.close()


class Gateway:
    def __init__(self, config: GatewayConfig):
        self.devices = {device.id: device for device in config.devices}

    async def remote(self, device, name: str, arguments: dict) -> dict:
        # Agent sessions live in the runtime, independent of HTTP/MCP session lifetime.
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {device.token()}"},
            timeout=httpx.Timeout(40, connect=10),
            follow_redirects=False,
        ) as client:
            async with streamable_http_client(device.url, http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(name, arguments)
        if result.isError:
            detail = " ".join(item.text for item in result.content if item.type == "text")
            raise ValueError(f"{device.id}: {detail}")
        if result.structuredContent is not None:
            return result.structuredContent
        raise ValueError("Remote agent did not return structured content")

    async def call(self, name: str, arguments: dict, device_id: str | None = None) -> dict:
        if name == "list_devices":

            async def probe(device):
                try:
                    async with asyncio.timeout(8):
                        info = await self.remote(device, "who_am_i", {})
                    return {"id": device.id, "online": True, **info}
                except Exception:
                    return {
                        "id": device.id,
                        "online": False,
                        "error": "Connection failed; check URL, token and agent service",
                    }

            return {"devices": await asyncio.gather(*(probe(d) for d in self.devices.values()))}
        if device_id is None:
            if len(self.devices) != 1:
                raise ValueError("deviceId is required with multiple configured devices")
            device_id = next(iter(self.devices))
        if device_id not in self.devices:
            raise ValueError(f"Unknown deviceId: {device_id}")
        return await self.remote(self.devices[device_id], name, arguments)

    def close(self):
        pass
