import socket
import threading
import time

import pytest
import uvicorn

from remote_commander.config import AgentConfig
from remote_commander.runtime import Runtime
from remote_commander.security import Guard
from remote_commander.server import create_http_app, create_server


@pytest.fixture
def runtime(tmp_path):
    runtime = Runtime(AgentConfig(roots=[str(tmp_path)], allow_shell=True, max_output_bytes=1024))
    yield runtime
    runtime.close()


@pytest.fixture
def live_agent(runtime):
    token = "test-secret-" + "a" * 40
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    mcp = create_server(runtime)
    server = uvicorn.Server(
        uvicorn.Config(
            Guard(create_http_app(mcp, runtime), token),
            log_level="error",
            timeout_graceful_shutdown=2,
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            pytest.fail("Agent startup timeout")
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}/mcp", token
    server.should_exit = True
    thread.join(timeout=10)
    sock.close()
    assert not thread.is_alive()
