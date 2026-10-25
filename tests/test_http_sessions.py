import asyncio

from test_processes_search import python_command

from remote_commander.config import Device, GatewayConfig
from remote_commander.runtime import Gateway


def test_process_survives_independent_mcp_requests(live_agent, monkeypatch):
    url, token = live_agent
    monkeypatch.setenv("RC_SESSION_TEST", token)
    gateway = Gateway(GatewayConfig(devices=[Device(id="a", url=url, token_env="RC_SESSION_TEST")]))

    async def scenario():
        job = await gateway.call(
            "start_process",
            {
                "command": python_command(
                    'import time; print("started",flush=True); time.sleep(60)'
                ),
                "timeout_ms": 0,
            },
        )
        try:
            for _ in range(10):
                read = await gateway.call(
                    "read_process_output",
                    {
                        "session_id": job["session_id"],
                        "timeout_ms": 1000,
                    },
                )
                assert read["state"] == "running", read
                if "started" in read["output"]:
                    break
            else:
                raise AssertionError("Process did not start")
        finally:
            await gateway.call("force_terminate", {"session_id": job["session_id"]})

    asyncio.run(scenario())
