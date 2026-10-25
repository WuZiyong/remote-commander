import json
import os
import socket
import subprocess
import sys
import time

import httpx


def test_cli_http_starts_and_requires_auth(tmp_path):
    config = tmp_path / "agent.json"
    config.write_text(json.dumps({"roots": [str(tmp_path)]}))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "RC_TOKEN": "cli-test-" + "x" * 40}
    log_path = tmp_path / "cli.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "remote_commander",
                "agent",
                "--config",
                str(config),
                "--port",
                str(port),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
        )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            assert process.poll() is None, log_path.read_text()
            try:
                response = httpx.post(f"http://127.0.0.1:{port}/mcp", timeout=0.3)
                assert response.status_code == 401
                break
            except (httpx.ConnectError, httpx.TimeoutException):
                time.sleep(0.05)
        else:
            raise AssertionError("CLI HTTP startup timed out: " + log_path.read_text())
    finally:
        process.terminate()
        process.wait(timeout=10)
