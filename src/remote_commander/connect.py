"""Pair once, then reconnect a local MCP agent to its owner's AIP gateway."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import plistlib
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil
from dotenv import dotenv_values

from .registry import private_write, public_key, validated_host_key


def secure_ssh_key(path):
    path.chmod(0o600)
    if sys.platform == "win32":
        import csv

        identity = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True
        )
        sid = next(csv.reader(identity.stdout.splitlines()))[1]
        # Windows chmod is insufficient for OpenSSH, especially inherited OWNER RIGHTS ACEs.
        subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"*{sid}:F",
                "/remove:g",
                "*S-1-3-4",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )


def ssh_command(profile, directory, key, remote="channel", port=None):
    command = [
        shutil.which("ssh") or "ssh",
        "-T",
        "-p",
        str(profile["ssh_port"]),
        "-i",
        str(key),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "ForwardAgent=no",
        "-o",
        f'UserKnownHostsFile="{(directory / "known_hosts").as_posix()}"',
    ]
    if port is not None:
        command += ["-R", f"127.0.0.1:{profile['port']}:127.0.0.1:{port}"]
    return command + [f"{profile['ssh_user']}@{profile['host']}", remote]


def protocol_result(output):
    for line in output.splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict) and ("device_id" in value or "error" in value):
                return value
        except ValueError:
            pass
    raise ValueError("No enrollment reply; preserve this profile for recovery")


def pair(
    invite_file,
    directory,
    *,
    roots=None,
    allow_shell=False,
    agent_env=None,
    agent_config=None,
    agent_port=8765,
):
    invite_file, directory = Path(invite_file), Path(directory).resolve()
    if (directory / "connection.json").exists():
        raise ValueError("This profile is already paired")
    if invite_file.stat().st_size > 20000:
        raise ValueError("Oversized invitation")
    invite_file.chmod(0o600)
    profile = json.loads(invite_file.read_text())
    import re

    if profile.get("version") != 1 or profile["expires"] <= time.time():
        raise ValueError("Invitation expired or unsupported")
    if (
        not re.fullmatch(r"[A-Za-z0-9.-]+", profile["host"])
        or not re.fullmatch(r"[A-Za-z0-9_-]+", profile["ssh_user"])
        or not 1 <= int(profile["ssh_port"]) <= 65535
    ):
        raise ValueError("Invalid invitation destination")
    if not 1024 <= agent_port <= 65535:
        raise ValueError("Invalid local agent port")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    private_write(
        directory / "known_hosts",
        f"[{profile['host']}]:{profile['ssh_port']} {validated_host_key(profile['host_key'])}\n",
    )
    key = directory / "device_ed25519"
    if not key.exists():
        subprocess.run(
            [
                shutil.which("ssh-keygen") or "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                str(key),
            ],
            check=True,
        )
    secure_ssh_key(key)
    if agent_env is not None:
        if agent_config is None:
            raise ValueError("An existing agent needs --agent-config and --agent-env")
        env, config = Path(agent_env).resolve(strict=True), Path(agent_config).resolve(strict=True)
        token = dotenv_values(env)["RC_TOKEN"]
        reuse = True
    else:
        env, config = directory / "agent.env", directory / "agent.json"
        token = dotenv_values(env)["RC_TOKEN"] if env.exists() else secrets.token_urlsafe(48)
        private_write(env, "RC_TOKEN=" + token + "\n")
        private_write(
            config,
            json.dumps(
                {
                    "name": profile["device_id"],
                    "roots": roots or [str(Path.home())],
                    "allow_shell": allow_shell,
                }
            ),
        )
        reuse = False
    request = {
        "owner": profile["owner"],
        "secret": profile["secret"],
        "public_key": public_key(key.with_suffix(".pub").read_text()),
        "agent_token": token,
    }
    with tempfile.TemporaryDirectory(dir=directory) as temporary:
        bootstrap = Path(temporary) / "bootstrap"
        private_write(bootstrap, profile["bootstrap_key"])
        secure_ssh_key(bootstrap)
        response = subprocess.run(
            ssh_command(profile, directory, bootstrap, "enroll"),
            input=json.dumps(request) + "\n",
            capture_output=True,
            text=True,
            timeout=40,
        )
    if "Host key verification failed" in response.stderr:
        raise PermissionError("Gateway host key mismatch; verify the endpoint before pairing")
    try:
        result = protocol_result(response.stdout)
    except ValueError:
        # Recover a committed enrollment whose reply was lost, using only the new device key.
        recovery = subprocess.run(
            ssh_command(profile, directory, key),
            input="{}\n",
            capture_output=True,
            text=True,
            timeout=40,
        )
        try:
            result = protocol_result(recovery.stdout)
        except ValueError:
            detail = (response.stderr + recovery.stderr)[-1200:]
            raise ConnectionError("SSH pairing failed: " + detail) from None
    if result.get("owner") != profile["owner"] or result.get("device_id") != profile["device_id"]:
        raise PermissionError("Pairing rejected or account identity mismatch")
    for name in ["secret", "bootstrap_key", "invite_id", "expires", "version", "host_key"]:
        profile.pop(name, None)
    profile.update(
        port=result["port"],
        agent_env=str(env),
        agent_config=str(config),
        agent_port=agent_port,
        reuse_agent=reuse,
    )
    private_write(directory / "connection.json", json.dumps(profile, indent=2))
    invite_file.unlink()  # One-use credential must not remain in the installation directory.
    return {"device_id": profile["device_id"], "owner": profile["owner"], "profile": str(directory)}


@contextlib.contextmanager
def instance_lock(directory):
    with (directory / "connector.lock").open("a+b") as stream:
        if sys.platform == "win32":
            import msvcrt

            stream.write(b"0")
            stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


async def run(directory):
    profile = json.loads((directory / "connection.json").read_text())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    agent = None
    log_path = directory / "connector.log"
    if log_path.exists() and log_path.stat().st_size > 5 * 1024 * 1024:
        os.replace(log_path, directory / "connector.previous.log")
    with log_path.open("ab", buffering=0) as log:
        try:
            if not profile["reuse_agent"]:
                saved = directory / "agent-process.json"
                if saved.exists():
                    old = json.loads(saved.read_text())
                    try:
                        candidate = psutil.Process(old["pid"])
                        if (
                            candidate.create_time() == old["created"]
                            and profile["agent_config"] in candidate.cmdline()
                            and "remote_commander" in candidate.cmdline()
                            and candidate.is_running()
                        ):
                            agent = candidate
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                executable = Path(sys.executable)
                if sys.platform == "win32" and executable.name.lower() == "pythonw.exe":
                    executable = executable.with_name("python.exe")
                if agent is None:
                    child = subprocess.Popen(
                        [
                            str(executable),
                            "-m",
                            "remote_commander",
                            "agent",
                            "--config",
                            profile["agent_config"],
                            "--env-file",
                            profile["agent_env"],
                            "--port",
                            str(profile["agent_port"]),
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=log,
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                    )
                    agent = psutil.Process(child.pid)
                    private_write(
                        saved, json.dumps({"pid": agent.pid, "created": agent.create_time()})
                    )
            failures = 0
            while not stop.is_set():
                if (directory / "revoked").exists():
                    await stop.wait()
                    break
                if agent is not None and (
                    not agent.is_running() or agent.status() == psutil.STATUS_ZOMBIE
                ):
                    raise RuntimeError("Local agent exited; check connector.log")
                process = None
                try:
                    command = ssh_command(
                        profile, directory, directory / "device_ed25519", port=profile["agent_port"]
                    )
                    process = await asyncio.create_subprocess_exec(
                        *command,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=log,
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                    )
                    while not stop.is_set():
                        process.stdin.write(b"{}\n")
                        await process.stdin.drain()
                        line = await asyncio.wait_for(process.stdout.readline(), 35)
                        if not line:
                            raise ConnectionError("SSH channel closed")
                        reply = json.loads(line)
                        if "error" in reply:
                            if reply["error"] == "PermissionError":
                                private_write(directory / "revoked", "Device access revoked\n")
                            raise ConnectionError("Gateway rejected channel")
                        if reply.get("status") not in {"ready", "ok"}:
                            raise ValueError("Invalid gateway reply")
                        if (
                            reply.get("status") == "ready"
                            and reply.get("owner") != profile["owner"]
                        ):
                            private_write(
                                directory / "revoked", "Gateway account identity mismatch\n"
                            )
                            raise PermissionError("Gateway account identity mismatch")
                        failures = 0
                        private_write(
                            directory / "status.json",
                            json.dumps(
                                {
                                    "device_id": profile["device_id"],
                                    "connected": True,
                                    "last_seen": time.time(),
                                    "gateway": profile["host"],
                                }
                            ),
                        )
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(stop.wait(), 15)
                except (OSError, ValueError, ConnectionError, TimeoutError):
                    failures += 1
                    private_write(
                        directory / "status.json",
                        json.dumps(
                            {
                                "device_id": profile["device_id"],
                                "connected": False,
                                "retry": failures,
                                "revoked": (directory / "revoked").exists(),
                            }
                        ),
                    )
                finally:
                    if process is not None and process.returncode is None:
                        with contextlib.suppress(ProcessLookupError):
                            process.terminate()
                        try:
                            await asyncio.wait_for(process.wait(), 5)
                        except TimeoutError:
                            process.kill()
                            await process.wait()
                if not stop.is_set():
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), min(30, 2 ** min(failures, 5)))
        finally:
            if agent is not None and agent.is_running():
                agent.terminate()
                try:
                    agent.wait(timeout=10)
                except psutil.TimeoutExpired:
                    agent.kill()
                    agent.wait()


def windows_task_xml(command, sid):
    import datetime
    import xml.etree.ElementTree as et

    root = et.Element(
        "Task", {"xmlns": "http://schemas.microsoft.com/windows/2004/02/mit/task", "version": "1.2"}
    )
    triggers = et.SubElement(root, "Triggers")
    trigger = et.SubElement(triggers, "LogonTrigger")
    et.SubElement(trigger, "Enabled").text = "true"
    et.SubElement(trigger, "UserId").text = sid
    watchdog = et.SubElement(triggers, "TimeTrigger")
    repetition = et.SubElement(watchdog, "Repetition")
    et.SubElement(repetition, "Interval").text = "PT5M"
    et.SubElement(repetition, "StopAtDurationEnd").text = "false"
    et.SubElement(watchdog, "StartBoundary").text = (
        datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=1)
    ).isoformat(timespec="seconds")
    et.SubElement(watchdog, "Enabled").text = "true"
    principal = et.SubElement(et.SubElement(root, "Principals"), "Principal", {"id": "Owner"})
    for key, value in [
        ("UserId", sid),
        ("LogonType", "InteractiveToken"),
        ("RunLevel", "LeastPrivilege"),
    ]:
        et.SubElement(principal, key).text = value
    settings = et.SubElement(root, "Settings")
    for key, value in [
        ("MultipleInstancesPolicy", "IgnoreNew"),
        ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"),
        ("StartWhenAvailable", "true"),
        ("AllowStartOnDemand", "true"),
        ("Enabled", "true"),
        ("Hidden", "true"),
        ("ExecutionTimeLimit", "PT0S"),
    ]:
        et.SubElement(settings, key).text = value
    restart = et.SubElement(settings, "RestartOnFailure")
    et.SubElement(restart, "Interval").text = "PT1M"
    et.SubElement(restart, "Count").text = "3"
    execute = et.SubElement(et.SubElement(root, "Actions", {"Context": "Owner"}), "Exec")
    et.SubElement(execute, "Command").text = command[0]
    et.SubElement(execute, "Arguments").text = subprocess.list2cmdline(command[1:])
    return et.tostring(root, encoding="unicode")


def install(directory):
    tag = hashlib.sha256(str(directory).encode()).hexdigest()[:12]
    command = [sys.executable, "-m", "remote_commander.connect", "run", "--profile", str(directory)]
    log = directory / "launcher.log"
    if sys.platform == "darwin":
        label = "local.remote-commander.connector." + tag
        target = Path.home() / "Library/LaunchAgents" / (label + ".plist")
        data = dict(
            Label=label,
            ProgramArguments=command,
            RunAtLoad=True,
            KeepAlive=True,
            ThrottleInterval=15,
            StandardOutPath=str(log),
            StandardErrorPath=str(log),
        )
        private_write(target, plistlib.dumps(data).decode())
        loaded = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"], capture_output=True
        )
        if loaded.returncode:
            subprocess.run(
                ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)], check=True
            )
    elif sys.platform == "linux":
        import shlex

        state = directory / "service"
        state.mkdir(mode=0o700, exist_ok=True)
        supervised = [
            sys.executable,
            "-m",
            "remote_commander.supervisor",
            "--state",
            str(state),
            "--",
            *command,
        ]
        launch = "/usr/bin/nohup " + shlex.join(supervised)
        launch += " >> " + shlex.quote(str(log)) + " 2>&1 </dev/null &"
        launch = launch.replace("%", r"\%")
        previous = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if previous.returncode and "no crontab" not in previous.stderr.lower():
            raise RuntimeError("Cannot read existing crontab")
        before = previous.stdout
        begin, end = (
            f"# BEGIN REMOTE_COMMANDER_CONNECTOR_{tag}",
            f"# END REMOTE_COMMANDER_CONNECTOR_{tag}",
        )
        if before.count(begin) != before.count(end) or before.count(begin) > 1:
            raise ValueError("Malformed connector crontab block")
        if begin in before:
            left, rest = before.split(begin, 1)
            _, right = rest.split(end, 1)
            before = left.rstrip("\n") + "\n" + right.lstrip("\n")
        private_write(state / f"crontab.before.{time.time_ns()}", previous.stdout)
        updated = (
            before.rstrip("\n") + f"\n{begin}\n@reboot {launch}\n*/5 * * * * {launch}\n{end}\n"
        )
        subprocess.run(["crontab", "-"], input=updated, text=True, check=True)
        with log.open("ab") as stream:
            subprocess.Popen(
                supervised,
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    elif sys.platform == "win32":
        import csv

        pythonw = Path(sys.executable).with_name("pythonw.exe")
        state = directory / "service"
        state.mkdir(exist_ok=True)
        task_command = [
            str(pythonw if pythonw.exists() else Path(sys.executable)),
            "-m",
            "remote_commander.supervisor",
            "--state",
            str(state),
            "--",
            *command,
        ]
        name = "RemoteCommander-" + tag
        identity = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True
        )
        sid = next(csv.reader(identity.stdout.splitlines()))[1]
        task = directory / "startup-task.xml"
        private_write(task, windows_task_xml(task_command, sid))
        subprocess.run(["schtasks", "/Create", "/TN", name, "/XML", str(task), "/F"], check=True)
        subprocess.run(["schtasks", "/Run", "/TN", name], check=True)
    else:
        raise ValueError("Unsupported startup platform")
    return {
        "device_id": json.loads((directory / "connection.json").read_text())["device_id"],
        "startup_installed": True,
        "profile": str(directory),
    }


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    for name in ["join", "run", "install"]:
        sub = modes.add_parser(name)
        sub.add_argument("--profile", required=True)
        if name == "join":
            sub.add_argument("--invite", required=True)
            sub.add_argument("--root", action="append")
            sub.add_argument("--allow-shell", action="store_true")
            sub.add_argument("--agent-env")
            sub.add_argument("--agent-config")
            sub.add_argument("--agent-port", type=int, default=8765)
    args = parser.parse_args()
    directory = Path(args.profile).resolve()
    try:
        if args.mode == "join":
            print(
                json.dumps(
                    pair(
                        args.invite,
                        directory,
                        roots=args.root,
                        allow_shell=args.allow_shell,
                        agent_env=args.agent_env,
                        agent_config=args.agent_config,
                        agent_port=args.agent_port,
                    )
                )
            )
        elif args.mode == "install":
            print(json.dumps(install(directory)))
        else:
            try:
                with instance_lock(directory):
                    asyncio.run(run(directory))
            except BlockingIOError:
                return
    except Exception as exc:
        print(f"Device connection failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
