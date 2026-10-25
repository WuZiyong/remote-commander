import asyncio
import os
import sys
import time

import pytest
from pydantic import ValidationError
from test_processes_search import finish, python_command

from remote_commander.config import Device, GatewayConfig
from remote_commander.runtime import Gateway


def test_kill_entire_process_tree(runtime, tmp_path):
    marker = tmp_path / "child-marker"
    # The child would write a marker if it survived the session termination.
    child = f"import time; from pathlib import Path; time.sleep(2); Path({str(marker)!r}).touch()"
    code = (
        f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); "
        "print('spawned', flush=True); time.sleep(60)"
    )
    started = runtime.processes.start_process(python_command(code), timeout_ms=1000)
    deadline = time.monotonic() + 5
    cursor, output = started["cursor"], started["output"]
    while "spawned" not in output and time.monotonic() < deadline:
        result = runtime.processes.read_process_output(started["session_id"], cursor, 1000)
        output += result["output"]
        cursor = result["cursor"]
    assert "spawned" in output
    runtime.processes.force_terminate(started["session_id"])
    finish(runtime.processes, started["session_id"], cursor)
    time.sleep(2.3)
    assert not marker.exists()


def test_bounded_input_does_not_block(runtime):
    started = runtime.processes.start_process(
        python_command("import time; time.sleep(60)"), timeout_ms=0
    )
    start = time.monotonic()
    for _ in range(3):
        try:
            runtime.processes.interact_with_process(started["session_id"], "a" * 65535)
        except ValueError as error:
            assert "queue full" in str(error)
    assert time.monotonic() - start < 1
    runtime.processes.force_terminate(started["session_id"])


def test_search_cap_and_special_file(runtime, tmp_path):
    runtime.files.write_file("many.txt", "hit\n" * 50)
    if hasattr(os, "mkfifo"):
        os.mkfifo(tmp_path / "pipe")
        with pytest.raises(ValueError, match="regular"):
            runtime.files.read_file("pipe")
    search = runtime.searches.start_search(".", "hit", max_results=2)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = runtime.searches.get_more_search_results(search["session_id"])
        if result["complete"]:
            break
        time.sleep(0.01)
    assert result["truncated"] and len(result["results"]) == 2


def test_config_validation_and_offline_probe(monkeypatch):
    with pytest.raises(ValidationError):
        Device(id="a", url="https://user:secret@example.com/mcp", token_env="RC_A")
    with pytest.raises(ValidationError):
        Device(id="a", url="http://localhost:1/mcp", token_env="SECRET")
    device = Device(id="offline", url="http://127.0.0.1:1/mcp", token_env="RC_OFFLINE")
    with pytest.raises(ValidationError):
        GatewayConfig(devices=[device, device])
    monkeypatch.setenv("RC_OFFLINE", "z" * 40)
    gateway = Gateway(GatewayConfig(devices=[device]))
    result = asyncio.run(gateway.call("list_devices", {}))
    assert result["devices"][0]["online"] is False
    assert "z" * 40 not in str(result)


def test_utf8_split_across_polls(runtime):
    code = (
        "import os,time; os.write(1,bytes([228])); time.sleep(0.2); "
        "os.write(1,bytes([189,160,229,165,189])); time.sleep(0.2)"
    )
    started = runtime.processes.start_process(python_command(code), timeout_ms=0)
    _, output = finish(runtime.processes, started["session_id"])
    assert output == "你好"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows symlink privileges vary")
def test_search_does_not_follow_outside_symlink(runtime, tmp_path):
    outside = tmp_path.parent / "outside-search.txt"
    outside.write_text("sensitive-secret")
    (tmp_path / "symlink.txt").symlink_to(outside)
    search = runtime.searches.start_search(".", "sensitive-secret")
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = runtime.searches.get_more_search_results(search["session_id"])
        if result["complete"]:
            break
        time.sleep(0.01)
    assert result["complete"] and not result["results"] and result["skipped"] == 1
