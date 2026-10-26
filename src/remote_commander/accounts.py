"""Persistent identities and hashed, expiring credentials for the shared AIP gateway."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
import uuid
from pathlib import Path


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Accounts:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Create privately before sqlite opens the file, including on a permissive umask.
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                  id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                  salt TEXT NOT NULL, password TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS records (
                  kind TEXT NOT NULL, hash TEXT NOT NULL,
                  value TEXT NOT NULL, expires REAL NOT NULL,
                  PRIMARY KEY(kind,hash));
                CREATE TABLE IF NOT EXISTS attempts (
                  bucket TEXT PRIMARY KEY, count INTEGER NOT NULL, expires REAL NOT NULL);
            """)

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

    def rate(self, bucket, maximum=10, seconds=300):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM attempts WHERE expires<?", (time.time(),))
            db.execute(
                "INSERT INTO attempts VALUES (?,1,?) ON CONFLICT(bucket) "
                "DO UPDATE SET count=count+1",
                (digest(bucket), time.time() + seconds),
            )
            count = db.execute(
                "SELECT count FROM attempts WHERE bucket=?", (digest(bucket),)
            ).fetchone()[0]
        if count > maximum:
            raise PermissionError("Too many attempts; try again later")

    @staticmethod
    def password_hash(password, salt):
        if not isinstance(password, str) or not 8 <= len(password) <= 1024:
            raise ValueError("Password must contain 8..1024 characters")
        return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()

    def register(self, username, password):
        username = username.strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{2,63}", username):
            raise ValueError(
                "Username needs 3..64 ASCII letters, digits, dots, underscores or hyphens"
            )
        salt = secrets.token_hex(16)
        hashed = self.password_hash(password, salt)
        owner = uuid.uuid4().hex
        try:
            with self.db() as db:
                db.execute(
                    "INSERT INTO users(id,username,salt,password) VALUES (?,?,?,?)",
                    (owner, username, salt, hashed),
                )
        except sqlite3.IntegrityError:
            raise ValueError("Username unavailable") from None
        return owner

    def login(self, username, password):
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM users WHERE username=?", (username.strip().lower(),)
            ).fetchone()
        # Equal work for unknown usernames. Never fall back to gateway's legacy owner.
        salt = row["salt"] if row else "0" * 32
        try:
            hashed = self.password_hash(password, salt)
        except ValueError:
            raise PermissionError("Invalid credentials") from None
        if not row or not row["active"] or not hmac.compare_digest(hashed, row["password"]):
            raise PermissionError("Invalid credentials")
        return row["id"]

    def active(self, owner):
        with self.db() as db:
            return (
                db.execute("SELECT 1 FROM users WHERE id=? AND active=1", (owner,)).fetchone()
                is not None
            )

    def put(self, kind, token, value, ttl):
        with self.db() as db:
            db.execute("DELETE FROM records WHERE expires<?", (time.time(),))
            db.execute(
                "INSERT INTO records VALUES (?,?,?,?)",
                (kind, digest(token), json.dumps(value), time.time() + ttl),
            )

    def get(self, kind, token, *, consume=False):
        if not isinstance(token, str) or not 1 <= len(token) <= 1024:
            return None
        with self.db() as db:
            if consume:
                db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT value FROM records WHERE kind=? AND hash=? AND expires>?",
                (kind, digest(token), time.time()),
            ).fetchone()
            if row and consume:
                db.execute("DELETE FROM records WHERE kind=? AND hash=?", (kind, digest(token)))
            return json.loads(row[0]) if row else None

    def issue(self, kind, value, ttl):
        token = secrets.token_urlsafe(48)
        self.put(kind, token, value, ttl)
        return token

    def revoke_family(self, family):
        with self.db() as db:
            db.execute(
                "DELETE FROM records WHERE kind IN ('access','refresh') "
                "AND json_extract(value,'$.family')=?",
                (family,),
            )

    def revoke_authorizations(self, owner):
        with self.db() as db:
            db.execute(
                "DELETE FROM records WHERE kind IN ('access','refresh','code') "
                "AND json_extract(value,'$.subject')=?",
                (owner,),
            )
