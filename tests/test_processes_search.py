import base64
import os
import shlex
import sys
import time

import pytest

from remote_commander.config import AgentConfig
from remote_commander.files import Files
from remote_commander.processes import Processes


def python_command(code):
    payload = base64.b64encode(code.encode()).decode()
    code = f"import base64; exec(base64.b64decode('{payload}'))"
    if os.name == "nt":
        return (
            "& '" + sys.executable.replace("'", "''") + "' -u -c '" + code.replace("'", "''") + "'"
        )
    return f"{shlex.quote(sys.executable)} -u -c {shlex.quote(code)}"


def finish(manager, session, cursor=0):
    deadline = time.monotonic() + 15
    parts = []
    while time.monotonic() < deadline:
        result = manager.read_process_output(session, cursor, 1000)
        parts.append(result["output"])
        cursor = result["cursor"]
        if result["state"] == "finished":
            return result, "".join(parts)
    pytest.fail("Job did not finish")


def test_process_incremental_input_and_exit(runtime):
    manager = runtime.processes
    result = manager.start_process(
        python_command('print("ready"); print(input()); exit(7)'), timeout_ms=0
    )
    manager.interact_with_process(result["session_id"], "hello")
    final, output = finish(manager, result["session_id"])
    assert "ready" in output and "hello" in output
    assert final["exit_code"] == 7
    assert manager.read_process_output(result["session_id"], final["cursor"])["output"] == ""


def test_process_output_limit_and_termination(runtime):
    manager = runtime.processes
    job = manager.start_process(python_command('print("x" * 10000)'), timeout_ms=0)
    finish(manager, job["session_id"])
    result = manager.read_process_output(job["session_id"])
    assert result["truncated"] and len(result["output"].encode()) <= 1024
    slow = manager.start_process(python_command("import time; time.sleep(60)"), timeout_ms=0)
    manager.force_terminate(slow["session_id"])
    final, _ = finish(manager, slow["session_id"])
    assert final["state"] == "finished" and final["exit_code"] != 0


def test_shell_is_explicit_and_secrets_not_inherited(runtime, monkeypatch):
    processes = Processes(Files(AgentConfig(roots=runtime.config.roots)))
    with pytest.raises(PermissionError):
        processes.start_process("echo forbidden")
    monkeypatch.setenv("RC_TOKEN", "private-secret")
    result = runtime.processes.start_process(
        python_command('import os; print(os.environ.get("RC_TOKEN", "absent"))'), timeout_ms=0
    )
    _, output = finish(runtime.processes, result["session_id"])
    assert "absent" in output and "private-secret" not in output


def test_search_pagination_and_limits(runtime):
    runtime.files.write_file("test.txt", "needle\nother\nneedle again\n")
    runtime.files.write_file("other.bin", "AA==", encoding="base64")
    search = runtime.searches.start_search(".", "needle")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = runtime.searches.get_more_search_results(search["session_id"], 0, 1)
        if result["complete"]:
            break
        time.sleep(0.01)
    assert result["complete"] and result["results"][0]["line"] == 1
    assert result["skipped"] == 1
    second = runtime.searches.get_more_search_results(search["session_id"], 1)
    assert second["results"][0]["line"] == 3
    runtime.searches.stop_search(search["session_id"])
