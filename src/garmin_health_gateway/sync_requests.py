"""Durable, bounded sync requests shared by MCP and the single collector worker."""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


class SyncRequests:
    cooldown_seconds = 300

    def __init__(self, directory: Path):
        self.directory = directory

    @contextmanager
    def lock(self, name: str = "queue", *, blocking: bool = True):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.directory / f"{name}.lock").open("a") as handle:
            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(handle, flags)
            except BlockingIOError as error:
                raise RuntimeError("Another collector sync is already running") from error
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _read(self) -> dict[str, Any] | None:
        path = self.directory / "request.json"
        return json.loads(path.read_text()) if path.exists() else None

    def _write(self, job: dict[str, Any]) -> None:
        path = self.directory / f".{uuid4().hex}.tmp"
        try:
            with path.open("w", encoding="utf-8") as handle:
                path.chmod(0o600)
                json.dump(job, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(path, self.directory / "request.json")
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            path.unlink(missing_ok=True)

    def status(self) -> dict[str, Any] | None:
        with self.lock():
            return self._read()

    def request(self) -> dict[str, Any]:
        with self.lock():
            job = self._read()
            now = datetime.now(UTC)
            if job:
                if job["status"] in {"queued", "running"}:
                    return {**job, "accepted": False, "reason": "already_pending"}
                elapsed = (now - datetime.fromisoformat(job["finished_at"])).total_seconds()
                if elapsed < self.cooldown_seconds:
                    return {
                        **job,
                        "accepted": False,
                        "reason": "cooldown",
                        "retry_after_seconds": max(1, int(self.cooldown_seconds - elapsed) + 1),
                    }
            job = {
                "request_id": str(uuid4()),
                "status": "queued",
                "days": 3,
                "requested_at": now.isoformat(),
            }
            self._write(job)
            return {**job, "accepted": True}

    def claim(self) -> dict[str, Any] | None:
        with self.lock():
            job = self._read()
            if not job or job["status"] != "queued":
                return None
            job.update(status="running", started_at=datetime.now(UTC).isoformat())
            self._write(job)
            return job

    def finish(self, result: dict[str, Any]) -> None:
        with self.lock():
            job = self._read()
            if job and job["status"] == "running":
                job.update(result, finished_at=datetime.now(UTC).isoformat())
                self._write(job)

    def recover(self) -> None:
        self.finish({"status": "error", "error": "Collector restarted during synchronization"})
