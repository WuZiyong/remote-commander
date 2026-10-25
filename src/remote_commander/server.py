"""Official MCP SDK tool definitions shared by agents and gateways."""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .runtime import Gateway, Runtime

log = logging.getLogger("remote_commander.audit")


def create_server(runtime: Runtime | Gateway, hosts: list[str] | None = None) -> FastMCP:
    @asynccontextmanager
    async def lifespan(server):
        try:
            yield {}
        finally:
            runtime.close()

    mcp = FastMCP(
        "Remote Commander",
        instructions=(
            "Self-hosted file and terminal tools. Call list_devices first. "
            "Use deviceId to select a machine. Paths use that machine's syntax. "
            "Process output cursors are absolute byte offsets; poll until state=finished. "
            "Shell execution has the agent OS account's permissions."
        ),
        stateless_http=True,
        json_response=True,
        lifespan=lifespan,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts or ["127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=[],
        ),
    )

    async def call(name, device_id, **kwargs):
        start = time.monotonic()
        try:
            result = await runtime.call(name, kwargs, device_id)
        except Exception:
            log.info(
                "tool=%s device=%s status=error seconds=%.3f",
                name,
                device_id,
                time.monotonic() - start,
            )
            raise
        log.info(
            "tool=%s device=%s status=ok seconds=%.3f", name, device_id, time.monotonic() - start
        )
        return result

    @mcp.tool()
    async def list_devices() -> dict[str, Any]:
        """List configured machines, identity and current online/offline status."""
        return await call("list_devices", None)

    @mcp.tool()
    async def who_am_i(deviceId: str | None = None) -> dict[str, Any]:
        """Get remote machine name, OS and account identity."""
        return await call("who_am_i", deviceId)

    @mcp.tool()
    async def get_config(deviceId: str | None = None) -> dict[str, Any]:
        """Inspect roots, permissions and size limits; never returns tokens."""
        return await call("get_config", deviceId)

    @mcp.tool()
    async def list_directory(
        path: str = ".", offset: int = 0, limit: int = 200, deviceId: str | None = None
    ) -> dict[str, Any]:
        """List directory entries with pagination. Order follows the filesystem."""
        return await call("list_directory", deviceId, path=path, offset=offset, limit=limit)

    @mcp.tool()
    async def get_file_info(path: str, deviceId: str | None = None) -> dict[str, Any]:
        """Get file size, type and modification time."""
        return await call("get_file_info", deviceId, path=path)

    @mcp.tool()
    async def read_file(
        path: str,
        offset: int = 0,
        length: int = 200,
        encoding: str = "utf-8",
        deviceId: str | None = None,
    ) -> dict[str, Any]:
        """Read UTF-8 lines (zero-based offset), or a whole bounded file as base64."""
        return await call(
            "read_file", deviceId, path=path, offset=offset, length=length, encoding=encoding
        )

    @mcp.tool()
    async def write_file(
        path: str,
        content: str,
        mode: str = "create",
        encoding: str = "utf-8",
        deviceId: str | None = None,
    ) -> dict[str, Any]:
        """Write UTF-8 or base64 content; mode=create, overwrite (atomic), or append."""
        return await call(
            "write_file", deviceId, path=path, content=content, mode=mode, encoding=encoding
        )

    @mcp.tool()
    async def edit_block(
        path: str,
        old_text: str,
        new_text: str,
        expected_replacements: int = 1,
        deviceId: str | None = None,
    ) -> dict[str, Any]:
        """Replace literal text atomically only when the match count equals the expectation."""
        return await call(
            "edit_block",
            deviceId,
            path=path,
            old_text=old_text,
            new_text=new_text,
            expected_replacements=expected_replacements,
        )

    @mcp.tool()
    async def create_directory(path: str, deviceId: str | None = None) -> dict[str, Any]:
        """Create a directory and missing parents within allowed roots."""
        return await call("create_directory", deviceId, path=path)

    @mcp.tool()
    async def move_file(
        source: str, destination: str, deviceId: str | None = None
    ) -> dict[str, Any]:
        """Move a regular file within the same filesystem; never overwrite a destination."""
        return await call("move_file", deviceId, source=source, destination=destination)

    @mcp.tool()
    async def start_process(
        command: str, cwd: str = ".", timeout_ms: int = 1000, deviceId: str | None = None
    ) -> dict[str, Any]:
        """Start a shell command; return a session ID, initial output and cursor.

        Requires allow_shell. Shell has OS account permissions, beyond file roots.
        timeout_ms (0..10000) only controls initial wait; it does not kill the job.
        """
        return await call(
            "start_process", deviceId, command=command, cwd=cwd, timeout_ms=timeout_ms
        )

    @mcp.tool()
    async def read_process_output(
        session_id: str, cursor: int = 0, timeout_ms: int = 0, deviceId: str | None = None
    ) -> dict[str, Any]:
        """Read/replay bounded output after an absolute byte cursor; wait up to 10 seconds."""
        return await call(
            "read_process_output",
            deviceId,
            session_id=session_id,
            cursor=cursor,
            timeout_ms=timeout_ms,
        )

    @mcp.tool()
    async def interact_with_process(
        session_id: str, input: str, deviceId: str | None = None
    ) -> dict[str, Any]:
        """Send a line of input to a managed process (pipe stdin; no terminal emulation)."""
        return await call("interact_with_process", deviceId, session_id=session_id, input=input)

    @mcp.tool()
    async def force_terminate(session_id: str, deviceId: str | None = None) -> dict[str, Any]:
        """Kill a managed session and its children; accepts session IDs, never arbitrary PIDs."""
        return await call("force_terminate", deviceId, session_id=session_id)

    @mcp.tool()
    async def list_sessions(deviceId: str | None = None) -> dict[str, Any]:
        """List this agent's managed process sessions and exit status."""
        return await call("list_sessions", deviceId)

    @mcp.tool()
    async def list_processes(deviceId: str | None = None) -> dict[str, Any]:
        """List up to 2000 OS processes (PID, name, user and state; no command-line secrets)."""
        return await call("list_processes", deviceId)

    @mcp.tool()
    async def start_search(
        path: str,
        pattern: str,
        search_type: str = "content",
        max_results: int = 1000,
        deviceId: str | None = None,
    ) -> dict[str, Any]:
        """Start a background literal content search or filename glob (search_type=files)."""
        return await call(
            "start_search",
            deviceId,
            path=path,
            pattern=pattern,
            search_type=search_type,
            max_results=max_results,
        )

    @mcp.tool()
    async def get_more_search_results(
        session_id: str, offset: int = 0, length: int = 100, deviceId: str | None = None
    ) -> dict[str, Any]:
        """Page search results. Continue while complete=false; inspect truncated/skipped."""
        return await call(
            "get_more_search_results", deviceId, session_id=session_id, offset=offset, length=length
        )

    @mcp.tool()
    async def stop_search(session_id: str, deviceId: str | None = None) -> dict[str, Any]:
        """Cancel a background search without losing results already collected."""
        return await call("stop_search", deviceId, session_id=session_id)

    return mcp
