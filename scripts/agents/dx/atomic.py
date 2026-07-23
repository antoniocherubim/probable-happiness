"""Atomic filesystem helpers for approval artifacts."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


def atomic_write_bytes(path: Path, content: bytes, mode: int = 0o600) -> None:
    """Atomically replace a file without changing its byte content."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, content: str, mode: int = 0o600) -> None:
    """Write text via temp file + replace so readers never see partial JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            if not content.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, payload: dict[str, Any], mode: int = 0o600) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True), mode=mode)


def exclusive_write_json(path: Path, payload: dict[str, Any], mode: int = 0o600) -> bool:
    """
    Publish JSON to ``path`` with exactly-one-winner semantics.

    Fully writes and fsyncs a same-directory temp file, then publishes with an
    atomic hard-link (no replace). If ``path`` already exists, returns False.
    The final pathname appears only after the payload is durable; temp files are
    always cleaned up. Concurrent losers never overwrite the winner.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, indent=2, sort_keys=True)
    if not content.endswith("\n"):
        content += "\n"

    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        try:
            os.link(tmp_name, str(path))
        except FileExistsError:
            return False
        return True
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


@contextmanager
def run_scoped_lock(
    run_dir: Path,
    lock_name: str = ".approval.lock",
    *,
    blocking: bool = True,
) -> Iterator[None]:
    """
    Exclusive flock for a run directory.

    Each acquisition opens its own FD so threads in the same process serialize
    correctly (flock is per open-file description).
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_path = run_dir / lock_name
    fd = os.open(
        str(lock_path),
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError(f"lock path is not a regular file: {lock_path}")
    try:
        operation = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(fd, operation)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def read_json(path: Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object in {path}")
    return data
