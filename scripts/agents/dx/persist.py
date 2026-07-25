"""Secure, durable filesystem persistence for run/project state (DX-08).

All durable reads and writes for run metadata, status, failure, heartbeat,
snapshots, approval, rejection, manifesto, outbox, evidence, and iteration
budget must go through this module (or thin wrappers in ``atomic`` that
delegate here).

Baseline trust: the state root is private (``0700``) and processes with the
same UID are trusted. Locks and content hashes detect corruption and drift;
they do not authenticate a hostile peer under the same UID.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping

from .schemas import RUNNER_VERSION

# Default limits for JSON state artifacts (not evidence blobs).
DEFAULT_MAX_JSON_BYTES = 1024 * 1024
STATUS_MAX_BYTES = 128
TMP_NAME_RE = re.compile(r"^\.[^/]+\.tmp(?:-|\.|$)")


class PersistError(ValueError):
    """A path failed secure open, ownership, mode, type, or containment checks."""


def apply_secure_umask() -> int:
    """Set process umask to ``0o077`` and return the previous mask."""
    return os.umask(0o077)


def ensure_secure_umask() -> None:
    """Idempotent entrypoint helper: always enforce ``umask 077``."""
    apply_secure_umask()


def canonical_json_bytes(payload: Mapping[str, Any] | dict[str, Any]) -> bytes:
    """Deterministic JSON encoding used for hashes and durable writes."""
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.encode("utf-8")


def canonical_json_hash(payload: Mapping[str, Any] | dict[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def fsync_directory(path: Path | str) -> None:
    """Durably persist directory metadata after replace/link/unlink."""
    directory = Path(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(str(directory), flags)
    except OSError as exc:
        raise PersistError(f"cannot fsync directory {directory}: {exc}") from exc
    try:
        os.fsync(fd)
    except OSError as exc:
        # Some network/FUSE filesystems reject directory fsync. Fail closed with
        # a diagnostic rather than silently claiming durability.
        raise PersistError(
            f"directory fsync unsupported or failed for {directory}: {exc}. "
            "Use a local POSIX filesystem (ext4/xfs/btrfs); NFS/FUSE may not "
            "provide the crash guarantees required by agent-loop."
        ) from exc
    finally:
        os.close(fd)


def assert_contained(path: Path | str, root: Path | str) -> Path:
    """Require ``path`` stay under ``root`` without following any symlinks.

    Walks each path component with ``lstat`` and refuses leaf or intermediate
    symlinks. Returns the lexically normalized path (never a symlink referent)
    so callers keep ``O_NOFOLLOW`` meaningful on the original pathname.
    """
    root_path = Path(root).expanduser()
    try:
        root_info = root_path.lstat()
    except OSError as exc:
        raise PersistError(f"containment root missing: {root_path}") from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise PersistError(f"containment root may not be a symlink: {root_path}")

    root_abs = root_path if root_path.is_absolute() else (Path.cwd() / root_path)
    root_norm = Path(os.path.normpath(str(root_abs)))

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate_norm = Path(os.path.normpath(str(candidate)))
    try:
        relative = candidate_norm.relative_to(root_norm)
    except ValueError as exc:
        raise PersistError(
            f"path escapes containment root: {candidate_norm} not under {root_norm}"
        ) from exc

    current = root_norm
    parts = relative.parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            # Remaining components are not yet created (first publish).
            break
        except OSError as exc:
            raise PersistError(f"cannot inspect containment path {current}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise PersistError(f"refusing symlink in containment path: {current}")
        is_last = index == len(parts) - 1
        if not is_last and not stat.S_ISDIR(info.st_mode):
            raise PersistError(f"containment intermediate is not a directory: {current}")
    return candidate_norm


def ensure_private_dir(path: Path | str, *, mode: int = 0o700) -> Path:
    """Create or validate a directory with owner-only access."""
    directory = Path(path)
    if directory.exists():
        info = directory.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise PersistError(f"expected private directory: {directory}")
        _check_owner_mode(info, directory, require_private=True, allow_exec=True)
        return directory
    directory.mkdir(mode=mode, parents=True, exist_ok=True)
    os.chmod(directory, mode)
    parent = directory.parent
    if parent.is_dir() and not parent.is_symlink():
        fsync_directory(parent)
    return directory


def _check_owner_mode(
    info: os.stat_result,
    path: Path,
    *,
    require_private: bool,
    allow_exec: bool,
    expected_owner: int | None = None,
) -> None:
    owner = os.geteuid() if expected_owner is None else expected_owner
    if info.st_uid != owner:
        raise PersistError(
            f"unexpected owner for {path}: uid={info.st_uid}, expected={owner}"
        )
    mode = stat.S_IMODE(info.st_mode)
    if require_private:
        # Group/other must have no access. Owner may be rw or rwx for dirs.
        if mode & 0o077:
            raise PersistError(
                f"insecure mode for {path}: {oct(mode)}; group/other access forbidden"
            )
        if allow_exec:
            if mode & 0o700 != 0o700 and mode & 0o700 != 0o500:
                # Directories should be 0700 (or at least owner r-x).
                if not (mode & stat.S_IRUSR and mode & stat.S_IXUSR):
                    raise PersistError(f"directory mode lacks owner r-x: {path}")
        else:
            # Regular files: owner must be able to read; write optional for RO.
            if not (mode & stat.S_IRUSR):
                raise PersistError(f"file is not owner-readable: {path}")


def _reject_special(info: os.stat_result, path: Path) -> None:
    if stat.S_ISLNK(info.st_mode):
        raise PersistError(f"refusing symlink: {path}")
    if not stat.S_ISREG(info.st_mode):
        kind = "directory" if stat.S_ISDIR(info.st_mode) else "special"
        if stat.S_ISFIFO(info.st_mode):
            kind = "FIFO"
        elif stat.S_ISSOCK(info.st_mode):
            kind = "socket"
        elif stat.S_ISCHR(info.st_mode) or stat.S_ISBLK(info.st_mode):
            kind = "device"
        raise PersistError(f"refusing {kind} path: {path}")
    if info.st_nlink > 1:
        raise PersistError(f"unexpected hard link (nlink={info.st_nlink}): {path}")


def secure_lstat_regular(
    path: Path | str,
    *,
    max_bytes: int | None = None,
    require_private: bool = True,
    expected_owner: int | None = None,
    allow_missing: bool = False,
) -> os.stat_result | None:
    """``lstat`` a regular private file or raise ``PersistError``."""
    target = Path(path)
    try:
        info = target.lstat()
    except FileNotFoundError:
        if allow_missing:
            return None
        raise PersistError(f"missing path: {target}") from None
    except OSError as exc:
        raise PersistError(f"cannot lstat {target}: {exc}") from exc
    _reject_special(info, target)
    _check_owner_mode(
        info,
        target,
        require_private=require_private,
        allow_exec=False,
        expected_owner=expected_owner,
    )
    if max_bytes is not None and info.st_size > max_bytes:
        raise PersistError(f"file exceeds {max_bytes} bytes: {target}")
    return info


def secure_open_read(
    path: Path | str,
    *,
    max_bytes: int,
    require_private: bool = True,
    expected_owner: int | None = None,
    containment_root: Path | str | None = None,
) -> tuple[int, os.stat_result]:
    """Open with ``O_NOFOLLOW``, verify inode match, return ``(fd, fstat)``."""
    target = Path(path)
    if containment_root is not None:
        target = assert_contained(target, containment_root)
    before = secure_lstat_regular(
        target,
        max_bytes=max_bytes,
        require_private=require_private,
        expected_owner=expected_owner,
    )
    assert before is not None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(target), flags)
    except OSError as exc:
        raise PersistError(f"cannot open {target}: {exc}") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise PersistError(f"type changed while opening: {target}")
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise PersistError(f"inode changed while opening: {target}")
        if opened.st_nlink > 1:
            raise PersistError(f"unexpected hard link after open: {target}")
        if opened.st_size > max_bytes:
            raise PersistError(f"file exceeds {max_bytes} bytes: {target}")
        _check_owner_mode(
            opened,
            target,
            require_private=require_private,
            allow_exec=False,
            expected_owner=expected_owner,
        )
        return fd, opened
    except Exception:
        os.close(fd)
        raise


def secure_read_bytes(
    path: Path | str,
    *,
    max_bytes: int,
    require_private: bool = True,
    expected_owner: int | None = None,
    containment_root: Path | str | None = None,
) -> bytes:
    fd, _opened = secure_open_read(
        path,
        max_bytes=max_bytes,
        require_private=require_private,
        expected_owner=expected_owner,
        containment_root=containment_root,
    )
    try:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(65536, max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise PersistError(f"file exceeds {max_bytes} bytes while reading: {path}")
            chunks.append(chunk)
        after = os.fstat(fd)
        if after.st_size != total:
            # Size drift during read is corruption / race.
            raise PersistError(f"size changed while reading: {path}")
        return b"".join(chunks)
    finally:
        os.close(fd)


def secure_read_text(
    path: Path | str,
    *,
    max_bytes: int,
    require_private: bool = True,
    expected_owner: int | None = None,
    containment_root: Path | str | None = None,
) -> str:
    raw = secure_read_bytes(
        path,
        max_bytes=max_bytes,
        require_private=require_private,
        expected_owner=expected_owner,
        containment_root=containment_root,
    )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PersistError(f"file is not UTF-8: {path}") from exc


def secure_read_json(
    path: Path | str,
    *,
    max_bytes: int = DEFAULT_MAX_JSON_BYTES,
    require_private: bool = True,
    expected_owner: int | None = None,
    allowed_keys: set[str] | frozenset[str] | None = None,
    containment_root: Path | str | None = None,
) -> dict[str, Any]:
    """Read a JSON object with closed schema when ``allowed_keys`` is set."""
    text = secure_read_text(
        path,
        max_bytes=max_bytes,
        require_private=require_private,
        expected_owner=expected_owner,
        containment_root=containment_root,
    )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PersistError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PersistError(f"expected JSON object in {path}")
    if allowed_keys is not None and set(data) != set(allowed_keys):
        raise PersistError(f"JSON schema mismatch in {path}")
    return data


def _prepare_private_parent(path: Path) -> None:
    parent = path.parent
    if not parent.exists():
        ensure_private_dir(parent)
    else:
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise PersistError(f"parent is not a directory: {parent}")


def _refuse_unsafe_existing_target(target: Path) -> None:
    """Fail closed before replace when the final pathname is already unsafe.

    Unsafe files must not be silently repaired by overwrite; operators inspect
    first. Missing targets are fine (first publish).
    """
    try:
        info = target.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise PersistError(f"cannot inspect existing target {target}: {exc}") from exc
    _reject_special(info, target)
    _check_owner_mode(info, target, require_private=True, allow_exec=False)


def secure_write_bytes(
    path: Path | str,
    content: bytes,
    *,
    mode: int = 0o600,
    fsync_dir: bool = True,
    containment_root: Path | str | None = None,
) -> None:
    """Atomically replace a file, chmod before publish, fsync file and dir."""
    target = Path(path)
    if containment_root is not None:
        # Containment uses non-strict resolve for not-yet-created leaf names.
        parent = assert_contained(target.parent, containment_root)
        target = parent / target.name
    _prepare_private_parent(target)
    _refuse_unsafe_existing_target(target)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, target)
        if fsync_dir:
            fsync_directory(target.parent)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def secure_write_text(
    path: Path | str,
    content: str,
    *,
    mode: int = 0o600,
    fsync_dir: bool = True,
    ensure_trailing_newline: bool = True,
    containment_root: Path | str | None = None,
) -> None:
    if ensure_trailing_newline and not content.endswith("\n"):
        content = content + "\n"
    secure_write_bytes(
        path,
        content.encode("utf-8"),
        mode=mode,
        fsync_dir=fsync_dir,
        containment_root=containment_root,
    )


def secure_write_json(
    path: Path | str,
    payload: dict[str, Any],
    *,
    mode: int = 0o600,
    fsync_dir: bool = True,
    pretty: bool = True,
    containment_root: Path | str | None = None,
) -> None:
    if pretty:
        text = json.dumps(payload, indent=2, sort_keys=True)
        if not text.endswith("\n"):
            text += "\n"
        secure_write_text(
            path,
            text,
            mode=mode,
            fsync_dir=fsync_dir,
            ensure_trailing_newline=False,
            containment_root=containment_root,
        )
    else:
        secure_write_bytes(
            path,
            canonical_json_bytes(payload) + b"\n",
            mode=mode,
            fsync_dir=fsync_dir,
            containment_root=containment_root,
        )


def secure_exclusive_write_json(
    path: Path | str,
    payload: dict[str, Any],
    *,
    mode: int = 0o600,
    fsync_dir: bool = True,
    containment_root: Path | str | None = None,
) -> bool:
    """Publish JSON via fsynced temp + hard-link; exactly one winner."""
    target = Path(path)
    if containment_root is not None:
        parent = assert_contained(target.parent, containment_root)
        target = parent / target.name
    _prepare_private_parent(target)
    # Existing pathname must not be a symlink/special that we would "repair"
    # by linking over — FileExistsError covers regular files; refuse specials.
    try:
        info = target.lstat()
    except FileNotFoundError:
        info = None
    except OSError as exc:
        raise PersistError(f"cannot inspect exclusive target {target}: {exc}") from exc
    if info is not None:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            _reject_special(info, target)
        return False
    content = json.dumps(payload, indent=2, sort_keys=True)
    if not content.endswith("\n"):
        content += "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        try:
            os.link(str(tmp_path), str(target))
        except FileExistsError:
            return False
        if fsync_dir:
            fsync_directory(target.parent)
        return True
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
            if fsync_dir and target.parent.is_dir():
                try:
                    fsync_directory(target.parent)
                except PersistError:
                    pass
        except OSError:
            pass


def secure_unlink(
    path: Path | str,
    *,
    fsync_dir: bool = True,
    require_private: bool = True,
    containment_root: Path | str | None = None,
) -> None:
    target = Path(path)
    if containment_root is not None:
        target = assert_contained(target, containment_root)
    secure_lstat_regular(target, require_private=require_private)
    target.unlink()
    if fsync_dir:
        fsync_directory(target.parent)


def cleanup_orphan_temps(
    directory: Path | str,
    *,
    expected_owner: int | None = None,
    name_predicate: Callable[[str], bool] | None = None,
) -> list[str]:
    """Remove orphan ``.*.tmp`` files only after name/owner/containment checks."""
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise PersistError(f"cleanup root must be a regular directory: {root}")
    removed: list[str] = []
    owner = os.geteuid() if expected_owner is None else expected_owner
    predicate = name_predicate or (lambda name: bool(TMP_NAME_RE.match(name)))
    for entry in root.iterdir():
        name = entry.name
        if not predicate(name):
            continue
        try:
            assert_contained(entry, root)
            info = entry.lstat()
        except (OSError, PersistError):
            continue
        if info.st_uid != owner:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            continue
        try:
            entry.unlink()
            removed.append(name)
        except OSError:
            continue
    if removed:
        fsync_directory(root)
    return removed


def hash_file_sha256(
    path: Path | str,
    *,
    max_bytes: int,
    require_private: bool = True,
) -> str:
    import hashlib

    fd, _opened = secure_open_read(
        path, max_bytes=max_bytes, require_private=require_private
    )
    try:
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise PersistError(f"file exceeds {max_bytes} bytes: {path}")
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(fd)


def runner_stamp() -> dict[str, str]:
    """Fields stamped onto newly written run envelopes."""
    return {"runner_version": RUNNER_VERSION}
