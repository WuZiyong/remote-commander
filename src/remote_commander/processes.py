"""Managed processes with bounded output and replayable absolute byte cursors."""

from __future__ import annotations

import codecs
import os
import queue
import signal
import subprocess
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field

import psutil

from .files import Files


@dataclass
class Job:
    process: subprocess.Popen
    identity: psutil.Process
    limit: int
    started: float = field(default_factory=time.time)
    output: bytearray = field(default_factory=bytearray)
    base: int = 0
    complete: bool = False
    condition: threading.Condition = field(default_factory=threading.Condition)
    inputs: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=1))

    def feed(self):
        while self.process.poll() is None:
            try:
                data = self.inputs.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self.process.stdin.write(data)
                self.process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                return

    def collect(self):
        try:
            while chunk := os.read(self.process.stdout.fileno(), 16384):
                with self.condition:
                    self.output.extend(chunk)
                    removed = max(0, len(self.output) - self.limit)
                    if removed:
                        del self.output[:removed]
                        self.base += removed
                    self.condition.notify_all()
        finally:
            self.process.wait()
            self.process.stdout.close()
            with suppress(BrokenPipeError, OSError, ValueError):
                self.process.stdin.close()
            with self.condition:
                self.complete = True
                self.condition.notify_all()


class Processes:
    def __init__(self, files: Files):
        self.files = files
        self.jobs: dict[str, Job] = {}
        self.lock = threading.RLock()

    def start_process(self, command: str, cwd: str = ".", timeout_ms: int = 1000) -> dict:
        if not self.files.config.allow_shell or self.files.config.read_only:
            raise PermissionError("Shell execution disabled; enable allow_shell explicitly")
        if not command.strip() or len(command) > 65536:
            raise ValueError("Command must contain 1..65536 characters")
        if not 0 <= timeout_ms <= 10000:
            raise ValueError("timeout_ms must be 0..10000")
        directory = self.files.path(cwd)
        with self.lock:
            if len(self.jobs) >= self.files.config.max_sessions:
                # Completed sessions are evicted only when capacity is needed.
                for key in list(self.jobs):
                    if self.jobs[key].complete:
                        del self.jobs[key]
                        break
                else:
                    raise ValueError("Too many active sessions")
            shell = self.files.config.shell or (
                "powershell.exe" if os.name == "nt" else "/bin/bash"
            )
            if os.name == "nt":
                command += (
                    "\n$rcSucceeded = $?; "
                    "if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }; "
                    "if (-not $rcSucceeded) { exit 1 }"
                )
                args = [shell, "-NoProfile", "-NonInteractive", "-Command", command]
            else:
                args = [shell, "-c", command]
            environment = os.environ.copy()
            # The child must never inherit agent/gateway MCP authentication secrets.
            for key in list(environment):
                if key.startswith("RC_"):
                    del environment[key]
            environment["PYTHONUNBUFFERED"] = "1"
            environment["PYTHONIOENCODING"] = "utf-8"
            process = subprocess.Popen(
                args,
                cwd=directory,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=environment,
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            job = Job(process, psutil.Process(process.pid), self.files.config.max_output_bytes)
            session = uuid.uuid4().hex
            self.jobs[session] = job
            threading.Thread(target=job.collect, daemon=True).start()
            threading.Thread(target=job.feed, daemon=True).start()
        return {"session_id": session, **self.read_process_output(session, 0, timeout_ms)}

    def get(self, session_id: str) -> Job:
        with self.lock:
            if session_id not in self.jobs:
                raise ValueError("Unknown or expired session_id")
            return self.jobs[session_id]

    def read_process_output(self, session_id: str, cursor: int = 0, timeout_ms: int = 0) -> dict:
        if cursor < 0 or not 0 <= timeout_ms <= 10000:
            raise ValueError("cursor >= 0; timeout_ms must be 0..10000")
        job = self.get(session_id)
        deadline = time.monotonic() + timeout_ms / 1000
        with job.condition:
            if cursor > job.base + len(job.output):
                raise ValueError("Cursor is beyond current output")
            while cursor >= job.base + len(job.output) and not job.complete:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                job.condition.wait(remaining)
            offset = max(cursor, job.base) - job.base
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            decoded = decoder.decode(bytes(job.output[offset:]), final=job.complete)
            pending, _ = decoder.getstate()
            return {
                "output": decoded,
                "cursor": job.base + len(job.output) - len(pending),
                "truncated": cursor < job.base,
                "state": "finished" if job.complete else "running",
                "exit_code": job.process.poll(),
                "pid": job.process.pid,
            }

    def interact_with_process(self, session_id: str, input: str) -> dict:
        if len(input.encode()) > 65536:
            raise ValueError("Input exceeds 64 KiB")
        job = self.get(session_id)
        if job.process.poll() is not None:
            raise ValueError("Process has finished")
        try:
            job.inputs.put_nowait((input + "\n").encode())
        except queue.Full:
            raise ValueError("Input queue full; process may not be reading stdin") from None
        return {"session_id": session_id, "queued": True}

    def force_terminate(self, session_id: str) -> dict:
        job = self.get(session_id)
        try:
            if os.name != "nt":
                # A separate process group also includes children whose shell exited.
                if not job.complete:
                    os.killpg(job.process.pid, signal.SIGKILL)
            elif job.identity.is_running():
                children = job.identity.children(recursive=True)
                for process in reversed(children):
                    try:
                        process.kill()
                    except psutil.NoSuchProcess:
                        pass
                job.identity.kill()
        except (ProcessLookupError, psutil.NoSuchProcess):
            pass
        return {"session_id": session_id, "termination_requested": True}

    def list_sessions(self) -> dict:
        with self.lock:
            return {
                "sessions": [
                    {
                        "session_id": key,
                        "pid": job.process.pid,
                        "started": job.started,
                        "exit_code": job.process.poll(),
                        "state": "finished" if job.complete else "running",
                    }
                    for key, job in self.jobs.items()
                ]
            }

    def close(self):
        with self.lock:
            for session_id in self.jobs:
                self.force_terminate(session_id)
