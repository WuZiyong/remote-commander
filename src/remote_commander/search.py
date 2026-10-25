"""Cancelable background literal/name searches with bounded retained results."""

from __future__ import annotations

import fnmatch
import os
import stat
import threading
import uuid
from dataclasses import dataclass, field

from .files import Files


@dataclass
class Search:
    results: list = field(default_factory=list)
    stop: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    complete: bool = False
    truncated: bool = False
    scanned: int = 0
    skipped: int = 0
    error: str | None = None


class Searches:
    def __init__(self, files: Files):
        self.files = files
        self.sessions: dict[str, Search] = {}
        self.lock = threading.Lock()

    def start_search(
        self, path: str, pattern: str, search_type: str = "content", max_results: int = 1000
    ) -> dict:
        root = self.files.path(path)
        if not root.is_dir():
            raise ValueError("Search path must be a directory")
        if not pattern or len(pattern) > 1024 or search_type not in {"content", "files"}:
            raise ValueError("Supply pattern (1..1024 chars) and search_type content or files")
        if not 1 <= max_results <= 5000:
            raise ValueError("max_results must be 1..5000")
        with self.lock:
            if len(self.sessions) >= 32:
                for key in list(self.sessions):
                    if self.sessions[key].complete:
                        del self.sessions[key]
                        break
                else:
                    raise ValueError("Too many active searches")
            session_id = uuid.uuid4().hex
            search = Search()
            self.sessions[session_id] = search

        def worker():
            try:
                for directory, dirs, files in os.walk(root, followlinks=False):
                    dirs[:] = [d for d in dirs if d not in {".git", ".venv", "node_modules"}]
                    for name in files:
                        if search.stop.is_set():
                            return
                        with search.lock:
                            search.scanned += 1
                            if search.scanned > 20000 or len(search.results) >= max_results:
                                search.truncated = True
                                return
                        try:
                            target = self.files.path(str(os.path.join(directory, name)))
                            if not stat.S_ISREG(target.stat().st_mode):
                                with search.lock:
                                    search.skipped += 1
                                continue
                            if search_type == "files":
                                matches = (
                                    [{"path": str(target)}]
                                    if fnmatch.fnmatch(name, pattern)
                                    else []
                                )
                            else:
                                with target.open("rb") as stream:
                                    data = stream.read(
                                        min(self.files.config.max_file_bytes, 1048576) + 1
                                    )
                                if (
                                    len(data) > min(self.files.config.max_file_bytes, 1048576)
                                    or b"\0" in data
                                ):
                                    with search.lock:
                                        search.skipped += 1
                                    continue
                                # Keep result text bounded independently of line length.
                                matches = []
                                for line_number, line in enumerate(
                                    data.decode("utf-8").splitlines(), 1
                                ):
                                    if search.stop.is_set():
                                        return
                                    if pattern in line:
                                        matches.append(
                                            {
                                                "path": str(target),
                                                "line": line_number,
                                                "text": line[:1000],
                                            }
                                        )
                                        if len(matches) >= max_results:
                                            break
                            with search.lock:
                                available = max_results - len(search.results)
                                search.results.extend(matches[:available])
                                if len(search.results) >= max_results:
                                    search.truncated = True
                                    return
                        except (OSError, ValueError, UnicodeError):
                            with search.lock:
                                search.skipped += 1
            except Exception as exc:
                search.error = str(exc)
            finally:
                with search.lock:
                    search.complete = True

        threading.Thread(target=worker, daemon=True).start()
        return {"session_id": session_id}

    def get_more_search_results(self, session_id: str, offset: int = 0, length: int = 100) -> dict:
        if offset < 0 or not 1 <= length <= 1000:
            raise ValueError("offset >= 0; length must be 1..1000")
        with self.lock:
            search = self.sessions[session_id]
        with search.lock:
            end = min(offset + length, len(search.results))
            return {
                "results": search.results[offset:end],
                "next_offset": end,
                "complete": search.complete,
                "truncated": search.truncated,
                "scanned": search.scanned,
                "skipped": search.skipped,
                "error": search.error,
                "canceled": search.stop.is_set(),
            }

    def stop_search(self, session_id: str) -> dict:
        with self.lock:
            self.sessions[session_id].stop.set()
        return {"session_id": session_id, "cancellation_requested": True}

    def close(self):
        with self.lock:
            for search in self.sessions.values():
                search.stop.set()
