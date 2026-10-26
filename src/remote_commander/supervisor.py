"""Single-instance user service supervisor, suitable for cron @reboot on Linux."""

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("A command after -- is required")
    os.umask(0o077)
    state = Path(args.state).resolve()
    state.mkdir(parents=True, exist_ok=True)
    lock = (state / "supervisor.lock").open("a+")
    try:
        if sys.platform == "win32":
            import msvcrt

            lock.write("0")
            lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, PermissionError):
        return
    (state / "supervisor.pid").write_text(str(os.getpid()))
    child = None
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        if child is not None and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopping:
            path = state / "service.log"
            if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
                os.replace(path, state / "service.previous.log")
            with path.open("ab", buffering=0) as log:
                child = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
                (state / "child.pid").write_text(str(child.pid))
                while child.poll() is None:
                    if stopping:
                        try:
                            child.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait()
                        break
                    time.sleep(0.2)
            if not stopping:
                for _ in range(25):
                    if stopping:
                        break
                    time.sleep(0.2)
    finally:
        (state / "supervisor.pid").unlink(missing_ok=True)
        (state / "child.pid").unlink(missing_ok=True)
        lock.close()


if __name__ == "__main__":
    main()
