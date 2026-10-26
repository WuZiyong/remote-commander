"""Owner-bound device enrollment over restricted OpenSSH commands (Linux gateway)."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import select
import shlex
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

BEGIN = "# BEGIN REMOTE_COMMANDER_REGISTRY"
END = "# END REMOTE_COMMANDER_REGISTRY"
MAX_MESSAGE = 16384
LEASE = 90


def private_write(path: Path, value: str):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def public_key(value: str) -> str:
    # Only a plain Ed25519 public key; never accept authorized_keys options from a client.
    import base64

    parts = value.strip().split()
    if len(parts) not in (2, 3) or parts[0] != "ssh-ed25519":
        raise ValueError("An Ed25519 public key is required")
    raw = base64.b64decode(parts[1], validate=True)
    if len(raw) != 51 or raw[:19] != b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20":
        raise ValueError("Invalid Ed25519 key")
    return " ".join(parts[:2])


def validated_host_key(value: str) -> str:
    from cryptography.hazmat.primitives.serialization import load_ssh_public_key

    parts = value.strip().split()
    if len(parts) < 2 or parts[0] not in {
        "ssh-ed25519",
        "ssh-rsa",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
    }:
        raise ValueError("Unsupported SSH host key")
    result = " ".join(parts[:2])
    load_ssh_public_key(result.encode())
    return result


@dataclass
class RegisteredDevice:
    id: str
    port: int
    secret: str = field(repr=False)
    last_seen: float = 0
    connected: bool = False

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/mcp"

    @property
    def online(self):
        return self.connected and time.time() - self.last_seen < LEASE

    def token(self):
        return self.secret


class Registry:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise ValueError("Initialize the device registry first")

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @classmethod
    def initialize(cls, path, *, host, port, user, host_key, authorized_keys, python):
        path = Path(path).resolve()
        if path.exists():
            raise ValueError(
                "Registry already exists; reuse it instead of replacing account identity"
            )
        if not re.fullmatch(r"[A-Za-z0-9.-]+", host) or not re.fullmatch(r"[A-Za-z0-9_-]+", user):
            raise ValueError("Invalid SSH host or user")
        if not 1 <= port <= 65535:
            raise ValueError("Invalid SSH port")
        validated_host_key(host_key)
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.executescript("""
                CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE invites (
                  id TEXT PRIMARY KEY, device_id TEXT NOT NULL, secret_hash TEXT NOT NULL,
                  public_key TEXT NOT NULL, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE devices (
                  id TEXT PRIMARY KEY, public_key TEXT NOT NULL UNIQUE, token TEXT NOT NULL,
                  port INTEGER NOT NULL UNIQUE, revoked INTEGER NOT NULL DEFAULT 0,
                  connected INTEGER NOT NULL DEFAULT 0, last_seen REAL NOT NULL DEFAULT 0,
                  connection_id TEXT, created REAL NOT NULL);
            """)
            settings = dict(
                owner=str(uuid.uuid4()),
                host=host,
                port=str(port),
                user=user,
                host_key=validated_host_key(host_key),
                authorized_keys=str(Path(authorized_keys).resolve()),
                python=str(Path(python).absolute()),
            )
            db.executemany("INSERT INTO settings VALUES (?,?)", settings.items())
        path.chmod(0o600)
        return cls(path)

    def settings(self):
        with self.db() as db:
            return dict(db.execute("SELECT key,value FROM settings"))

    def sync_keys(self):
        import fcntl

        settings = self.settings()
        target = Path(settings["authorized_keys"])
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (target.parent / "remote-commander-registry.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            original = target.read_text() if target.exists() else ""
            if original.count(BEGIN) != original.count(END) or original.count(BEGIN) > 1:
                raise ValueError("Malformed managed SSH key block")
            if BEGIN in original:
                before, rest = original.split(BEGIN, 1)
                _, after = rest.split(END, 1)
                original = before.rstrip("\n") + "\n" + after.lstrip("\n")
            prefix = [settings["python"], "-m", "remote_commander.registry", "--db", str(self.path)]
            lines = [BEGIN]
            with self.db() as db:
                for row in db.execute(
                    "SELECT * FROM invites WHERE used=0 AND expires>?", (time.time(),)
                ):
                    command = shlex.join(prefix + ["accept", row["id"]])
                    lines.append(self.key_line(command, row["public_key"]))
                for row in db.execute("SELECT * FROM devices WHERE revoked=0"):
                    command = shlex.join(prefix + ["channel", row["id"]])
                    lines.append(self.key_line(command, row["public_key"], row["port"]))
            lines.append(END)
            backup = target.parent / "authorized_keys.before-registry"
            if not backup.exists():
                private_write(backup, target.read_text() if target.exists() else "")
            private_write(target, original.rstrip("\n") + "\n" + "\n".join(lines) + "\n")

    @staticmethod
    def key_line(command, key, port=None):
        # sshd's command option requires double-quote/backslash escaping, not shell escaping.
        escaped = command.replace("\\", "\\\\").replace('"', '\\"')
        options = "restrict"
        if port is not None:
            options += f',port-forwarding,permitopen="127.0.0.1:1",permitlisten="127.0.0.1:{port}"'
        return f'{options},command="{escaped}" {public_key(key)} remote-commander-registry'

    def invite(self, device_id, output, ttl=1800):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}", device_id):
            raise ValueError("Device ID must contain 1..48 letters, digits, underscores or hyphens")
        if not 60 <= ttl <= 3600:
            raise ValueError("Invite lifetime must be 60..3600 seconds")
        output = Path(output).resolve()
        if output.exists():
            raise ValueError("Invite output already exists")
        settings = self.settings()
        with self.db() as db:
            if db.execute("SELECT 1 FROM devices WHERE id=?", (device_id,)).fetchone():
                raise ValueError("Device ID already registered; use a new ID")
            if db.execute("SELECT count(*) FROM devices WHERE revoked=0").fetchone()[0] >= 64:
                raise ValueError("Device limit reached")
        with tempfile.TemporaryDirectory(dir=self.path.parent) as temporary:
            key = Path(temporary) / "bootstrap"
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True
            )
            pub = public_key(key.with_suffix(".pub").read_text())
            private = key.read_text()
        invite_id, secret = uuid.uuid4().hex, secrets.token_urlsafe(48)
        expires = time.time() + ttl
        with self.db() as db:
            db.execute(
                "INSERT INTO invites VALUES (?,?,?,?,?,0)",
                (invite_id, device_id, hashlib.sha256(secret.encode()).hexdigest(), pub, expires),
            )
        self.sync_keys()
        profile = dict(
            version=1,
            owner=settings["owner"],
            invite_id=invite_id,
            secret=secret,
            device_id=device_id,
            host=settings["host"],
            ssh_port=int(settings["port"]),
            ssh_user=settings["user"],
            host_key=settings["host_key"],
            bootstrap_key=private,
            expires=expires,
        )
        private_write(output, json.dumps(profile, indent=2))
        return {
            "device_id": device_id,
            "invite_file": str(output),
            "expires": expires,
            "owner": settings["owner"],
        }

    def accept(self, invite_id, request):
        key = public_key(request["public_key"])
        token = request["agent_token"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
            raise ValueError("Invalid agent token")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            invite = db.execute("SELECT * FROM invites WHERE id=?", (invite_id,)).fetchone()
            owner = db.execute("SELECT value FROM settings WHERE key='owner'").fetchone()[0]
            if not invite or invite["used"] or invite["expires"] <= time.time():
                raise PermissionError("Invite expired or already used")
            if request.get("owner") != owner or not hmac.compare_digest(
                invite["secret_hash"],
                hashlib.sha256(request.get("secret", "").encode()).hexdigest(),
            ):
                raise PermissionError("Wrong account or pairing secret")
            if db.execute("SELECT count(*) FROM devices WHERE revoked=0").fetchone()[0] >= 64:
                raise ValueError("Device limit reached")
            occupied = {r[0] for r in db.execute("SELECT port FROM devices")}
            port = next(
                (p for p in range(30000, 31000) if p not in occupied and self.port_free(p)), None
            )
            if port is None:
                raise ValueError("No available registration port")
            db.execute(
                "INSERT INTO devices (id,public_key,token,port,created) VALUES (?,?,?,?,?)",
                (invite["device_id"], key, token, port, time.time()),
            )
            db.execute("UPDATE invites SET used=1 WHERE id=?", (invite_id,))
        self.sync_keys()
        return {"owner": owner, "device_id": invite["device_id"], "port": port}

    @staticmethod
    def port_free(port):
        try:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False

    def devices(self):
        with self.db() as db:
            return {
                r["id"]: RegisteredDevice(
                    r["id"], r["port"], r["token"], r["last_seen"], bool(r["connected"])
                )
                for r in db.execute("SELECT * FROM devices WHERE revoked=0 ORDER BY created")
            }

    def identities(self):
        with self.db() as db:
            return {r[0] for r in db.execute("SELECT id FROM devices")}

    def heartbeat(self, device_id, connection_id, *, opening=False):
        with self.db() as db:
            if opening:
                cursor = db.execute(
                    "UPDATE devices SET connection_id=?,connected=1,last_seen=? "
                    "WHERE id=? AND revoked=0",
                    (connection_id, time.time(), device_id),
                )
            else:
                cursor = db.execute(
                    "UPDATE devices SET last_seen=? WHERE id=? AND connection_id=? AND revoked=0",
                    (time.time(), device_id, connection_id),
                )
            if cursor.rowcount != 1:
                raise PermissionError("Device revoked or connection replaced")

    def disconnect(self, device_id, connection_id):
        with self.db() as db:
            db.execute(
                "UPDATE devices SET connected=0 WHERE id=? AND connection_id=?",
                (device_id, connection_id),
            )

    def revoke(self, device_id):
        with self.db() as db:
            if (
                db.execute(
                    "UPDATE devices SET revoked=1,connected=0 WHERE id=?", (device_id,)
                ).rowcount
                != 1
            ):
                raise ValueError("Unknown registered device")
        self.sync_keys()


def read_message(stream, timeout=45):
    if not select.select([stream], [], [], timeout)[0]:
        raise TimeoutError("Device heartbeat timed out")
    line = stream.readline(MAX_MESSAGE + 1)
    if not line or len(line) > MAX_MESSAGE or not line.endswith("\n"):
        raise ValueError("Missing or oversized protocol message")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("Protocol message must be an object")
    return value


def make_bundle(invite, output):
    project = Path(__file__).resolve().parents[2]
    output = Path(output)
    if output.exists():
        raise ValueError("Bundle already exists")
    with zipfile.ZipFile(output, "x", zipfile.ZIP_DEFLATED) as archive:
        for name in ["pyproject.toml", "constraints.txt", "setup_device.py", ".gitignore"]:
            archive.write(project / name, name)
        for p in (project / "src/remote_commander").glob("*.py"):
            archive.write(p, str(p.relative_to(project)))
        archive.write(invite, "pairing-invite.json")
    output.chmod(0o600)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    commands = parser.add_subparsers(dest="mode", required=True)
    init = commands.add_parser("init")
    init.add_argument("--host", required=True)
    init.add_argument("--port", type=int, default=2222)
    init.add_argument("--user", required=True)
    init.add_argument("--host-key-file", required=True)
    init.add_argument("--authorized-keys", default=str(Path.home() / ".ssh/authorized_keys"))
    inv = commands.add_parser("invite")
    inv.add_argument("--device-id", required=True)
    inv.add_argument("--output", required=True)
    inv.add_argument("--ttl", type=int, default=1800)
    inv.add_argument("--bundle")
    for name in ["accept", "channel", "revoke"]:
        commands.add_parser(name).add_argument("id")
    commands.add_parser("list")
    args = parser.parse_args()
    try:
        if args.mode == "init":
            registry = Registry.initialize(
                args.db,
                host=args.host,
                port=args.port,
                user=args.user,
                host_key=Path(args.host_key_file).read_text(),
                authorized_keys=args.authorized_keys,
                python=sys.executable,
            )
            print(json.dumps({"owner": registry.settings()["owner"]}))
            return
        registry = Registry(args.db)
        if args.mode == "invite":
            result = registry.invite(args.device_id, args.output, args.ttl)
            if args.bundle:
                make_bundle(args.output, args.bundle)
                result["bundle_file"] = str(Path(args.bundle).resolve())
            print(json.dumps(result))  # Never print invitation credentials.
        elif args.mode == "accept":
            print(json.dumps(registry.accept(args.id, read_message(sys.stdin))), flush=True)
        elif args.mode == "channel":
            connection_id = uuid.uuid4().hex
            try:
                read_message(sys.stdin)
                registry.heartbeat(args.id, connection_id, opening=True)
                device = registry.devices()[args.id]
                print(
                    json.dumps(
                        {
                            "status": "ready",
                            "port": device.port,
                            "owner": registry.settings()["owner"],
                            "device_id": args.id,
                        }
                    ),
                    flush=True,
                )
                while True:
                    read_message(sys.stdin)
                    registry.heartbeat(args.id, connection_id)
                    print('{"status":"ok"}', flush=True)
            finally:
                registry.disconnect(args.id, connection_id)
        elif args.mode == "revoke":
            registry.revoke(args.id)
            print(json.dumps({"revoked": args.id}))
        else:
            print(
                json.dumps(
                    {
                        "owner": registry.settings()["owner"],
                        "devices": [
                            {"id": d.id, "online": d.online, "last_seen": d.last_seen}
                            for d in registry.devices().values()
                        ],
                    }
                )
            )
    except Exception as exc:
        if args.mode in {"accept", "channel"}:
            print(json.dumps({"error": type(exc).__name__}), flush=True)
        else:
            print(f"Registry error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
