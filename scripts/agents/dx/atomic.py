"""Atomic filesystem helpers for approval artifacts.

DX-08: all durable writes fsync the parent directory after replace/link.
Reads go through the secure no-follow API in ``persist``.
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .persist import (
    PersistError,
    fsync_directory,
    secure_exclusive_write_json,
    secure_read_json,
    secure_write_bytes,
    secure_write_json,
    secure_write_text,
)


def atomic_write_bytes(path: Path, content: bytes, mode: int = 0o600) -> None:
    """Atomically replace a file without changing its byte content."""
    secure_write_bytes(path, content, mode=mode, fsync_dir=True)


def atomic_write_text(path: Path, content: str, mode: int = 0o600) -> None:
    """Write text via temp file + replace so readers never see partial JSON."""
    secure_write_text(path, content, mode=mode, fsync_dir=True)


def atomic_write_json(path: Path, payload: dict[str, Any], mode: int = 0o600) -> None:
    secure_write_json(path, payload, mode=mode, fsync_dir=True)


def exclusive_write_json(path: Path, payload: dict[str, Any], mode: int = 0o600) -> bool:
    """
    Publish JSON to ``path`` with exactly-one-winner semantics.

    Fully writes and fsyncs a same-directory temp file, then publishes with an
    atomic hard-link (no replace). If ``path`` already exists, returns False.
    The final pathname appears only after the payload is durable; temp files are
    always cleaned up. Concurrent losers never overwrite the winner. Parent
    directory is fsynced after a successful link.
    """
    return secure_exclusive_write_json(path, payload, mode=mode, fsync_dir=True)


@contextmanager
def run_scoped_lock(
    run_dir: Path,
    lock_name: str = ".approval.lock",
    *,
    blocking: bool = True,
) -> Iterator[None]:
    """
    Exclusive flock for a run directory.

    Validates the directory and lock via ``lstat`` / ``O_NOFOLLOW`` / ``fstat``
    before ``flock``, then revalidates the pathname inode after locking.
    Insecure existing locks (mode, owner, hard link, symlink, inode swap) are
    refused without ``chmod``, replace, or other mutation. Newly created locks
    are ``0600`` and the parent directory is fsynced.
    """
    from .persist import PersistError, revalidate_lock_fd, secure_acquire_lock_fd

    run_dir = Path(run_dir)
    try:
        fd = secure_acquire_lock_fd(run_dir, lock_name)
    except PersistError as exc:
        raise ValueError(str(exc)) from exc
    try:
        operation = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(fd, operation)
        try:
            revalidate_lock_fd(run_dir, lock_name, fd)
        except PersistError as exc:
            raise ValueError(str(exc)) from exc
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def read_json(
    path: Path,
    *,
    require_private: bool = True,
    allowed_keys: set[str] | frozenset[str] | None = None,
    containment_root: Path | str | None = None,
) -> dict[str, Any]:
    """Secure JSON object read (no symlink follow; owner/mode/type-safe).

    Production callers must keep ``require_private=True`` (fail closed on
    group/other bits). Inspect/migrate of legacy artifacts may pass
    ``require_private=False`` explicitly — never silently repair modes.
    """
    try:
        return secure_read_json(
            path,
            require_private=require_private,
            allowed_keys=allowed_keys,
            containment_root=containment_root,
        )
    except PersistError as exc:
        raise ValueError(str(exc)) from exc


# Re-exports for callers that need durability helpers.
__all__ = [
    "PersistError",
    "atomic_write_bytes",
    "atomic_write_json",
    "atomic_write_text",
    "exclusive_write_json",
    "fsync_directory",
    "read_json",
    "run_scoped_lock",
]
