"""Bounded UTF-8 and binary file operations within configured roots.

Roots constrain file tools; they are not an OS sandbox for shell commands.
Only trusted OS users should be able to mutate the roots concurrently.
"""

from __future__ import annotations

import base64
import os
import stat
import tempfile
from pathlib import Path

from .config import AgentConfig


class Files:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.roots = [Path(p) for p in config.roots]

    def path(self, value: str, *, write: bool = False) -> Path:
        if write and self.config.read_only:
            raise PermissionError("File writes disabled by read_only")
        raw = Path(value).expanduser()
        if not raw.is_absolute():
            raw = self.roots[0] / raw
        path = raw.resolve()
        if not any(path.is_relative_to(root) for root in self.roots):
            raise PermissionError("Path is outside configured roots")
        # NTFS alternate data streams bypass normal file semantics.
        if os.name == "nt" and any(":" in part for part in path.parts[1:]):
            raise PermissionError("NTFS alternate streams are not supported")
        return path

    def list_directory(self, path: str = ".", offset: int = 0, limit: int = 200) -> dict:
        if offset < 0 or not 1 <= limit <= 1000:
            raise ValueError("offset >= 0; limit must be 1..1000")
        directory = self.path(path)
        # Bound traversal even when a directory has millions of entries.
        entries = []
        with os.scandir(directory) as scan:
            for index, item in enumerate(scan):
                if index < offset:
                    continue
                if len(entries) == limit:
                    return {"entries": entries, "next_offset": offset + limit}
                entries.append(
                    {
                        "name": item.name,
                        "is_directory": item.is_dir(follow_symlinks=False),
                        "is_symlink": item.is_symlink(),
                    }
                )
        return {"entries": entries, "next_offset": None}

    def get_file_info(self, path: str) -> dict:
        target = self.path(path)
        stat = target.stat()
        return {
            "path": str(target),
            "size": stat.st_size,
            "is_directory": target.is_dir(),
            "modified": stat.st_mtime,
        }

    def read_file(
        self, path: str, offset: int = 0, length: int = 200, encoding: str = "utf-8"
    ) -> dict:
        target = self.path(path)
        if not stat.S_ISREG(target.stat().st_mode):
            raise ValueError("Only regular files can be read")
        if offset < 0 or not 1 <= length <= 10000:
            raise ValueError("offset >= 0; length must be 1..10000")
        with target.open("rb") as stream:
            data = stream.read(self.config.max_file_bytes + 1)
        if len(data) > self.config.max_file_bytes:
            raise ValueError("File exceeds max_file_bytes; use shell tools for larger files")
        if encoding == "base64":
            return {
                "content": base64.b64encode(data).decode(),
                "encoding": encoding,
                "size": len(data),
            }
        if encoding != "utf-8":
            raise ValueError("encoding must be utf-8 or base64")
        lines = data.decode("utf-8").splitlines(keepends=True)
        return {
            "content": "".join(lines[offset : offset + length]),
            "total_lines": len(lines),
            "next_offset": offset + length if offset + length < len(lines) else None,
        }

    def write_file(
        self, path: str, content: str, mode: str = "create", encoding: str = "utf-8"
    ) -> dict:
        target = self.path(path, write=True)
        if target.exists() and not stat.S_ISREG(target.stat().st_mode):
            raise ValueError("Only regular files can be written")
        if encoding == "utf-8":
            data = content.encode("utf-8")
        elif encoding == "base64":
            data = base64.b64decode(content, validate=True)
        else:
            raise ValueError("encoding must be utf-8 or base64")
        if len(data) > self.config.max_file_bytes:
            raise ValueError("Content exceeds max_file_bytes")
        if mode == "create":
            with target.open("xb") as stream:
                stream.write(data)
        elif mode == "append":
            if target.stat().st_size + len(data) > self.config.max_file_bytes:
                raise ValueError("Result exceeds max_file_bytes")
            with target.open("ab") as stream:
                stream.write(data)
        elif mode == "overwrite":
            self.atomic_write(target, data)
        else:
            raise ValueError("mode must be create, overwrite or append")
        return {"path": str(target), "bytes_written": len(data)}

    @staticmethod
    def atomic_write(target: Path, data: bytes):
        permissions = target.stat().st_mode & 0o777 if target.exists() else 0o600
        fd, temporary = tempfile.mkstemp(prefix=".rc-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, permissions)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def edit_block(
        self, path: str, old_text: str, new_text: str, expected_replacements: int = 1
    ) -> dict:
        target = self.path(path, write=True)
        if not old_text or expected_replacements < 1:
            raise ValueError("old_text must be nonempty and expected_replacements >= 1")
        original = self.read_file(path, encoding="base64")["content"]
        data = base64.b64decode(original).decode("utf-8")
        count = data.count(old_text)
        if count != expected_replacements:
            raise ValueError(
                f"Expected {expected_replacements} matches, found {count}; no edit made"
            )
        replacement = data.replace(old_text, new_text).encode("utf-8")
        if len(replacement) > self.config.max_file_bytes:
            raise ValueError("Result exceeds max_file_bytes")
        self.atomic_write(target, replacement)
        return {"path": str(target), "replacements": count}

    def create_directory(self, path: str) -> dict:
        target = self.path(path, write=True)
        target.mkdir(parents=True, exist_ok=True)
        return {"path": str(target)}

    def move_file(self, source: str, destination: str) -> dict:
        origin = self.path(source, write=True)
        target = self.path(destination, write=True)
        if origin in self.roots or not stat.S_ISREG(origin.stat().st_mode):
            raise ValueError("Only files can be moved; configured roots cannot be moved")
        if target.exists():
            raise FileExistsError("Destination exists")
        # Hard link + unlink provides an atomic no-clobber move on the same filesystem.
        os.link(origin, target)
        origin.unlink()
        return {"source": str(origin), "destination": str(target)}
