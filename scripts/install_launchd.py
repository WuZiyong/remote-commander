"""Install a user-level macOS agent with a private env file; no sudo required."""

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("This installer is for macOS only")
    project = Path(__file__).resolve().parent.parent
    command = project / ".venv/bin/remote-commander"
    if not command.exists():
        parser.error("Install the package in .venv first")
    config, env = Path(args.config).resolve(strict=True), Path(args.env_file).resolve(strict=True)
    env.chmod(0o600)
    label = "local.remote-commander.agent"
    target = Path.home() / "Library/LaunchAgents" / (label + ".plist")
    if target.exists():
        parser.error(f"Service already exists: {target}; review it before replacing")
    target.parent.mkdir(parents=True, exist_ok=True)
    logs = project / ".runtime"
    logs.mkdir(exist_ok=True)
    payload = {
        "Label": label,
        "RunAtLoad": True,
        "KeepAlive": True,
        "WorkingDirectory": str(project),
        "ThrottleInterval": 10,
        "ProgramArguments": [
            str(command),
            "agent",
            "--config",
            str(config),
            "--env-file",
            str(env),
            "--port",
            str(args.port),
        ],
        "StandardOutPath": str(logs / "agent.stdout.log"),
        "StandardErrorPath": str(logs / "agent.stderr.log"),
    }
    with target.open("xb") as stream:
        plistlib.dump(payload, stream)
    target.chmod(0o600)
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)], check=True)
    print(f"Installed {label} at {target}")
    print(f"Stop: launchctl bootout gui/{os.getuid()} {target}")


if __name__ == "__main__":
    main()
