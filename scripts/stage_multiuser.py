"""Deploy an isolated loopback-only portal without touching the production registry or SSH keys."""

import argparse
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

from remote_commander.registry import Registry, private_write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("--port", type=int, default=18890)
    args = parser.parse_args()
    os.umask(0o077)
    state = Path(args.state).resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    project = Path(__file__).resolve().parent.parent
    if (state / "registry.sqlite").exists():
        parser.error("Use a new state directory; do not replace an existing registry")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", args.port))
    host_key = state / "host_ed25519"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(host_key)], check=True)
    Registry.initialize(
        state / "registry.sqlite",
        host="127.0.0.1",
        port=2222,
        user="stage",
        host_key=host_key.with_suffix(".pub").read_text(),
        authorized_keys=state / "authorized_keys.NOT_LIVE",
        python=sys.executable,
    )
    private_write(
        state / "gateway.json", json.dumps({"registry_path": str(state / "registry.sqlite")})
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(project / "src")
    command = [
        sys.executable,
        "-m",
        "remote_commander.supervisor",
        "--state",
        str(state / "service"),
        "--",
        sys.executable,
        "-m",
        "remote_commander.portal",
        "--gateway-config",
        str(state / "gateway.json"),
        "--state",
        str(state / "accounts"),
        "--issuer",
        "http://127.0.0.1:" + str(args.port),
        "--port",
        str(args.port),
        "--allow-local-http",
        "--registration-open",
    ]
    process = subprocess.Popen(
        command,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=(state / "launcher.log").open("ab"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    print(
        json.dumps(
            {
                "supervisor_pid": process.pid,
                "loopback_port": args.port,
                "isolated_registry": str(state / "registry.sqlite"),
                "production_ssh_keys_changed": False,
            }
        )
    )


if __name__ == "__main__":
    main()
