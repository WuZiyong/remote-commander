"""Exercise a real MCP endpoint; never prints credentials or file contents."""

import argparse
import asyncio
import base64
import json
import os
import shlex
import uuid

import httpx
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def command_for(info, code):
    payload = base64.b64encode(code.encode()).decode()
    script = f"import base64; exec(base64.b64decode('{payload}'))"
    executable = info["python_executable"]
    if info["system"] == "Windows":
        return "& '" + executable.replace("'", "''") + "' -u -c '" + script.replace("'", "''") + "'"
    return f"{shlex.quote(executable)} -u -c {shlex.quote(script)}"


async def smoke(url, token, exercise=False, device_id=None):
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, timeout=httpx.Timeout(30, connect=10)
    ) as client:
        async with streamable_http_client(url, http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                async def call(name, **arguments):
                    if device_id and name != "list_devices":
                        arguments["deviceId"] = device_id
                    result = await session.call_tool(name, arguments)
                    if result.isError or result.structuredContent is None:
                        raise RuntimeError(f"{name} failed: {result.content}")
                    return result.structuredContent

                tools = (await session.list_tools()).tools
                devices = await call("list_devices")
                info = await call("who_am_i")
                config = await call("get_config")
                await call("list_directory")
                report = {
                    "tool_count": len(tools),
                    "devices": devices,
                    "identity": info,
                    "read_only_checks": "passed",
                }
                if exercise:
                    if not config["allow_shell"] or config["read_only"]:
                        raise RuntimeError("Exercise needs writable roots and allow_shell=true")
                    folder = "rc-smoke-" + uuid.uuid4().hex
                    await call("create_directory", path=folder)

                    async def run(code):
                        job = await call(
                            "start_process", command=command_for(info, code), timeout_ms=1000
                        )
                        for _ in range(30):
                            if job["state"] == "finished":
                                if job["exit_code"] != 0:
                                    raise RuntimeError(f"Smoke command failed: {job}")
                                return job
                            update = await call(
                                "read_process_output",
                                session_id=job["session_id"],
                                cursor=job["cursor"],
                                timeout_ms=1000,
                            )
                            job.update(update)
                        raise RuntimeError("Smoke command timeout")

                    try:
                        path = folder + "/check.txt"
                        await call("write_file", path=path, content="remote MCP works\n")
                        assert (await call("read_file", path=path))[
                            "content"
                        ] == "remote MCP works\n"
                        await call("edit_block", path=path, old_text="works", new_text="verified")
                        await call("move_file", source=path, destination=folder + "/moved.txt")
                        search = await call("start_search", path=folder, pattern="verified")
                        for _ in range(50):
                            found = await call(
                                "get_more_search_results", session_id=search["session_id"]
                            )
                            if found["complete"]:
                                assert found["results"] and not found["error"]
                                break
                            await asyncio.sleep(0.05)
                        else:
                            raise RuntimeError("Search timed out")
                        # Multiple independent gateway calls exercise runtime session persistence.
                        await run(
                            'import time; print("begin", flush=True); time.sleep(1); print("end")'
                        )
                        job = await call(
                            "start_process",
                            command=command_for(info, "import time; time.sleep(60)"),
                            timeout_ms=0,
                        )
                        await call("force_terminate", session_id=job["session_id"])
                        for _ in range(15):
                            result = await call(
                                "read_process_output", session_id=job["session_id"], timeout_ms=1000
                            )
                            if result["state"] == "finished":
                                break
                        else:
                            raise RuntimeError("Termination failed")
                        report["file_search_process_checks"] = "passed"
                    finally:
                        # Resolve and verify the exact unique target before recursive cleanup.
                        cleanup = (
                            "from pathlib import Path; import shutil; "
                            f"root=Path({config['roots'][0]!r}).resolve(); "
                            f"target=(root / {folder!r}).resolve(); "
                            f"assert target.parent == root and target.name == {folder!r}; "
                            "shutil.rmtree(target)"
                        )
                        await run(cleanup)
                return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-env", default="RC_TOKEN")
    parser.add_argument("--env-file")
    parser.add_argument("--device-id")
    parser.add_argument("--exercise", action="store_true")
    args = parser.parse_args()
    if args.env_file:
        load_dotenv(args.env_file)
    result = asyncio.run(smoke(args.url, os.environ[args.token_env], args.exercise, args.device_id))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
