"""Install an isolated user cron startup block without altering existing jobs."""

import argparse
import datetime
import os
import shlex
import subprocess
import sys
from pathlib import Path

BEGIN = "# BEGIN REMOTE_COMMANDER_SERVICE"
END = "# END REMOTE_COMMANDER_SERVICE"


def merge_crontab(existing: str, command: str, begin: str = BEGIN, end: str = END) -> str:
    if existing.count(begin) != existing.count(end) or existing.count(begin) > 1:
        raise ValueError("Malformed existing service block; refusing to change crontab")
    if begin in existing:
        before, rest = existing.split(begin, 1)
        _, after = rest.split(end, 1)
        existing = before.rstrip("\n") + "\n" + after.lstrip("\n")
    return (
        existing.rstrip("\n")
        + "\n"
        + "\n".join(
            [
                begin,
                "@reboot " + command,
                "*/5 * * * * " + command,
                end,
                "",
            ]
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=["agent", "bridge"], default="agent")
    parser.add_argument("--config")
    parser.add_argument("--site")
    parser.add_argument("--gateway-config")
    parser.add_argument("--env-file", required=True)
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("This installer requires Linux")
    os.umask(0o077)
    project = Path(__file__).resolve().parent.parent
    state = project / (".runtime/service" if args.kind == "agent" else ".runtime/bridge-service")
    state.mkdir(parents=True, exist_ok=True)
    secret = Path(args.env_file).resolve(strict=True)
    secret.chmod(0o600)
    if args.kind == "agent":
        if not args.config:
            parser.error("--config is required for an agent")
        config = Path(args.config).resolve(strict=True)
        child = [sys.executable, "-m", "remote_commander", "agent", "--config", str(config)]
    else:
        if not args.site or not args.gateway_config:
            parser.error("A bridge needs --site and --gateway-config")
        config = Path(args.gateway_config).resolve(strict=True)
        child = [
            sys.executable,
            "-m",
            "remote_commander.relay",
            "--site",
            args.site,
            "--gateway-config",
            str(config),
        ]
    command = [
        sys.executable,
        "-m",
        "remote_commander.supervisor",
        "--state",
        str(state),
        "--",
        *child,
        "--env-file",
        str(secret),
    ]
    launch = "/usr/bin/nohup " + shlex.join(command)
    launch += " >> " + shlex.quote(str(state / "launcher.log")) + " 2>&1 </dev/null &"
    # cron treats unescaped percent characters specially, including inside shell quotes.
    launch = launch.replace("%", r"\%")
    previous = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if previous.returncode != 0 and "no crontab" not in previous.stderr.lower():
        raise RuntimeError("Cannot read existing crontab: " + previous.stderr)
    original = previous.stdout
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    (state / ("crontab.before." + stamp)).write_text(original)
    updated = (
        merge_crontab(original, launch)
        if args.kind == "agent"
        else merge_crontab(
            original, launch, "# BEGIN REMOTE_COMMANDER_BRIDGE", "# END REMOTE_COMMANDER_BRIDGE"
        )
    )
    subprocess.run(["crontab", "-"], input=updated, text=True, check=True)
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=(state / "launcher.log").open("ab"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    print("Installed @reboot startup, five-minute supervisor recovery and a single-instance lock")


if __name__ == "__main__":
    main()
