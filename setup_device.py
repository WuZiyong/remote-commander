"""Run from an owner-issued installation bundle; no GitHub login or AIP password needed."""

import argparse
import os
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append")
    parser.add_argument("--allow-shell", action="store_true")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if sys.version_info < (3, 11):  # noqa: UP036 - this installer is run before package installation
        parser.error("Python 3.11+ is required")
    os.umask(0o077)
    root = Path(__file__).resolve().parent
    invite = root / "pairing-invite.json"
    if invite.exists():
        invite.chmod(0o600)
    python = root / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    if not python.exists():
        subprocess.run([sys.executable, "-m", "venv", str(root / ".venv")], check=True)
    subprocess.run(
        [str(python), "-m", "pip", "install", "-c", str(root / "constraints.txt"), "-e", str(root)],
        check=True,
    )
    profile = root / ".runtime/device"
    if not (profile / "connection.json").exists():
        command = [
            str(python),
            "-m",
            "remote_commander.connect",
            "join",
            "--profile",
            str(profile),
            "--invite",
            str(root / "pairing-invite.json"),
            "--agent-port",
            str(args.port),
        ]
        for path in args.root or []:
            command += ["--root", path]
        if args.allow_shell:
            command.append("--allow-shell")
        subprocess.run(command, check=True)
    subprocess.run(
        [str(python), "-m", "remote_commander.connect", "install", "--profile", str(profile)],
        check=True,
    )
    print("Device paired. It will appear automatically in Remote Commander.")


if __name__ == "__main__":
    main()
