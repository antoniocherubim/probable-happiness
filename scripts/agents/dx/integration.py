"""Safe local integration of an immutable reviewed snapshot."""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .approval import utc_now_iso, verify_reviewed_snapshot
from .atomic import atomic_write_json, read_json, run_scoped_lock
from .control_adapter import ControlAdapterError, assert_candidate_transport
from .runstate import RunStateError, validate_run
from .snapshot import MANIFEST_FILENAME, SnapshotError, build_snapshot_manifest


INTEGRATION_FILENAME = "integration.json"
INTEGRATION_LOCK = ".integration.lock"
_REPO_LOCK = "agent-loop-integration.lock"
_ALLOWED_MODES = {"100644", "100755", "120000"}
_HEX_64 = re.compile(r"[0-9a-f]{64}")


class IntegrationError(ValueError):
    """The approved snapshot cannot be integrated without weakening safety."""


def _git_environment(*, index_file: Path | None = None) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AGENT_TELEGRAM_")
    }
    environment.update(
        {
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_PROTOCOL_FROM_USER": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
        }
    )
    if index_file is not None:
        environment["GIT_INDEX_FILE"] = str(index_file)
    else:
        environment.pop("GIT_INDEX_FILE", None)
    return environment


def _git(
    repo: Path,
    *args: str,
    input_bytes: bytes | None = None,
    index_file: Path | None = None,
) -> bytes:
    command = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-C",
        str(repo),
        *args,
    ]
    try:
        completed = subprocess.run(
            command,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=_git_environment(index_file=index_file),
        )
    except OSError as exc:
        raise IntegrationError(
            f"cannot execute local Git command: {args[0]}"
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        if detail:
            detail = detail.splitlines()[0][:300]
        raise IntegrationError(
            f"local Git command failed: {args[0]}"
            + (f": {detail}" if detail else "")
        )
    return completed.stdout


def _git_text(
    repo: Path,
    *args: str,
    input_bytes: bytes | None = None,
    index_file: Path | None = None,
) -> str:
    return _git(
        repo,
        *args,
        input_bytes=input_bytes,
        index_file=index_file,
    ).decode("utf-8", errors="strict").strip()


def _safe_relative(value: object) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise IntegrationError("reviewed manifest contains an invalid path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path.parts[0] == ".git":
        raise IntegrationError(
            f"reviewed manifest contains unsafe path: {value!r}"
        )
    return value


def _validate_manifest(
    run_dir: Path,
    *,
    base_commit: str,
    worktree: Path,
    reviewed_hash: str,
) -> dict[str, Any]:
    try:
        manifest = read_json(run_dir / MANIFEST_FILENAME)
    except (OSError, ValueError) as exc:
        raise IntegrationError(
            "reviewed manifest is missing or invalid"
        ) from exc
    if (
        manifest.get("schema_version") != 1
        or manifest.get("base_commit") != base_commit
        or manifest.get("snapshot_hash") != reviewed_hash
        or not isinstance(manifest.get("entries"), list)
        or not manifest["entries"]
    ):
        raise IntegrationError("reviewed manifest binding is invalid")

    try:
        current = build_snapshot_manifest(worktree, base_commit)
    except SnapshotError as exc:
        raise IntegrationError(str(exc)) from exc
    if (
        current.get("snapshot_hash") != reviewed_hash
        or current.get("entries") != manifest.get("entries")
    ):
        raise IntegrationError(
            "reviewed manifest does not match the live snapshot"
        )

    seen: set[str] = set()
    for entry in manifest["entries"]:
        if not isinstance(entry, dict):
            raise IntegrationError(
                "reviewed manifest entry must be an object"
            )
        relative = _safe_relative(entry.get("path"))
        if relative in seen:
            raise IntegrationError(
                f"duplicate reviewed manifest path: {relative}"
            )
        seen.add(relative)
        operation = entry.get("operation")
        if operation == "delete":
            if set(entry) != {"path", "operation"}:
                raise IntegrationError(
                    f"invalid deletion entry: {relative}"
                )
            continue
        if operation != "upsert" or set(entry) != {
            "path",
            "operation",
            "kind",
            "mode",
            "sha256",
            "size_bytes",
        }:
            raise IntegrationError(f"invalid upsert entry: {relative}")
        mode = entry.get("mode")
        kind = entry.get("kind")
        if (
            mode not in _ALLOWED_MODES
            or (mode == "120000") != (kind == "symlink")
            or (mode != "120000") != (kind == "regular")
            or not _HEX_64.fullmatch(str(entry.get("sha256", "")))
            or type(entry.get("size_bytes")) is not int
            or entry["size_bytes"] < 0
        ):
            raise IntegrationError(
                f"invalid reviewed entry metadata: {relative}"
            )
    return manifest


def _entry_path(worktree: Path, relative: str) -> Path:
    candidate = worktree / relative
    current = worktree
    for part in Path(relative).parts[:-1]:
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise IntegrationError(
                f"reviewed entry parent is unavailable: {relative}"
            ) from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise IntegrationError(
                "reviewed entry traverses a non-directory parent: "
                f"{relative}"
            )
    return candidate


def _read_reviewed_bytes(
    worktree: Path,
    entry: dict[str, Any],
) -> bytes:
    relative = str(entry["path"])
    path = _entry_path(worktree, relative)
    kind = entry["kind"]
    if kind == "symlink":
        try:
            info = path.lstat()
            if not stat.S_ISLNK(info.st_mode):
                raise IntegrationError(
                    f"reviewed symlink changed: {relative}"
                )
            content = os.fsencode(os.readlink(path))
        except OSError as exc:
            raise IntegrationError(
                f"cannot read reviewed symlink: {relative}"
            ) from exc
    else:
        try:
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode):
                raise IntegrationError(
                    f"reviewed file changed type: {relative}"
                )
            expected_mode = (
                "100755" if before.st_mode & 0o111 else "100644"
            )
            if expected_mode != entry["mode"]:
                raise IntegrationError(
                    f"reviewed file mode changed: {relative}"
                )
            fd = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                opened = os.fstat(fd)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or (opened.st_dev, opened.st_ino)
                    != (before.st_dev, before.st_ino)
                ):
                    raise IntegrationError(
                        "reviewed file changed while opening: "
                        f"{relative}"
                    )
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(fd, 1024 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                after = os.fstat(fd)
                if (
                    after.st_size != opened.st_size
                    or after.st_mtime_ns != opened.st_mtime_ns
                ):
                    raise IntegrationError(
                        "reviewed file changed while reading: "
                        f"{relative}"
                    )
                content = b"".join(chunks)
            finally:
                os.close(fd)
        except OSError as exc:
            raise IntegrationError(
                f"cannot read reviewed file: {relative}"
            ) from exc
    if (
        len(content) != entry["size_bytes"]
        or hashlib.sha256(content).hexdigest() != entry["sha256"]
    ):
        raise IntegrationError(
            f"reviewed content hash changed: {relative}"
        )
    return content


def _repo_clean(repo: Path) -> bool:
    return not _git(
        repo,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )


@contextmanager
def _repository_lock(repo: Path) -> Iterator[None]:
    common = Path(
        _git_text(
            repo,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        )
    ).resolve()
    lock_path = common / _REPO_LOCK
    fd = os.open(
        lock_path,
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise IntegrationError(
            "repository integration lock is not a regular file"
        )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _build_tree_and_commit(
    *,
    repo: Path,
    worktree: Path,
    base_commit: str,
    manifest: dict[str, Any],
    message: str,
) -> tuple[str, str]:
    with tempfile.TemporaryDirectory(
        prefix="agent-loop-integrate-"
    ) as temp:
        index = Path(temp) / "index"
        _git(repo, "read-tree", base_commit, index_file=index)
        for entry in manifest["entries"]:
            relative = str(entry["path"])
            if entry["operation"] == "delete":
                _git(
                    repo,
                    "update-index",
                    "--force-remove",
                    "--",
                    relative,
                    index_file=index,
                )
                continue
            content = _read_reviewed_bytes(worktree, entry)
            blob = _git_text(
                repo,
                "hash-object",
                "-w",
                "--stdin",
                input_bytes=content,
            )
            _git(
                repo,
                "update-index",
                "--add",
                "--cacheinfo",
                str(entry["mode"]),
                blob,
                relative,
                index_file=index,
            )
        _reject_checkout_filters(
            repo,
            manifest["entries"],
            index_file=index,
        )
        tree = _git_text(repo, "write-tree", index_file=index)
        # Unlike ``git diff --check`` in a dirty worktree, comparing complete
        # tree objects also covers files that were untracked during review.
        _git(repo, "diff", "--check", base_commit, tree, "--")
    commit = _git_text(
        repo,
        "commit-tree",
        tree,
        "-p",
        base_commit,
        input_bytes=(message + "\n").encode("utf-8"),
    )
    return tree, commit


def _reject_checkout_filters(
    repo: Path,
    entries: list[dict[str, Any]],
    *,
    index_file: Path,
) -> None:
    """Reject attributes that could execute a configured smudge process."""
    paths = [
        os.fsencode(str(entry["path"]))
        for entry in entries
        if entry["operation"] == "upsert"
    ]
    if not paths:
        return
    output = _git(
        repo,
        "check-attr",
        "--cached",
        "-z",
        "--stdin",
        "filter",
        input_bytes=b"\0".join(paths) + b"\0",
        index_file=index_file,
    )
    fields = output.split(b"\0")
    if fields and not fields[-1]:
        fields.pop()
    if len(fields) % 3:
        raise IntegrationError("cannot validate checkout filter attributes")
    for offset in range(0, len(fields), 3):
        path, attribute, value = fields[offset : offset + 3]
        if attribute != b"filter":
            raise IntegrationError("unexpected checkout attribute response")
        if value not in {b"unspecified", b"unset"}:
            relative = os.fsdecode(path)
            raise IntegrationError(
                "reviewed snapshot activates a checkout filter for "
                f"{relative!r}; local integration refused"
            )


def _default_message(task_file: str) -> str:
    task_id = Path(task_file).stem.strip()
    if not task_id:
        raise IntegrationError(
            "cannot derive task id for integration commit"
        )
    return f"{task_id}: integrate approved snapshot"


def _validate_message(message: str) -> str:
    if (
        not isinstance(message, str)
        or not message.strip()
        or "\0" in message
        or "\n" in message
        or "\r" in message
        or len(message) > 240
    ):
        raise IntegrationError(
            "commit message must be one non-empty line (max 240 chars)"
        )
    return message.strip()


def integrate_reviewed_snapshot(
    run_dir: Path,
    *,
    message: str | None = None,
) -> dict[str, Any]:
    """Create one local commit and fast-forward the current branch.

    The operation never invokes a Git remote, never pushes, never runs hooks,
    and never stages or commits from the mutable target repository checkout.
    """
    run_dir = Path(run_dir).expanduser()
    with run_scoped_lock(run_dir, lock_name=INTEGRATION_LOCK):
        try:
            metadata = validate_run(run_dir)
            verification = verify_reviewed_snapshot(run_dir)
        except (RunStateError, OSError, ValueError) as exc:
            raise IntegrationError(str(exc)) from exc
        if not verification.get("matches"):
            raise IntegrationError(
                "live worktree no longer matches reviewed snapshot"
            )
        profile = metadata.get("profile")
        approval = profile.get("approval") if isinstance(profile, dict) else None
        if isinstance(approval, dict) and approval.get("mode") in {
            "github_branch",
            "github_pr",
        }:
            raise IntegrationError(
                "manual integration is disabled for remote branch approval mode"
            )

        repo = Path(str(metadata["repo"])).resolve()
        worktree = Path(str(metadata["worktree"])).resolve()
        base_commit = str(metadata["base_commit"])
        reviewed_hash = str(verification["reviewed_diff_hash"])
        manifest = _validate_manifest(
            run_dir,
            base_commit=base_commit,
            worktree=worktree,
            reviewed_hash=reviewed_hash,
        )
        try:
            assert_candidate_transport(run_dir, metadata, manifest, worktree)
        except ControlAdapterError as exc:
            raise IntegrationError(str(exc)) from exc
        commit_message = _validate_message(
            message
            if message is not None
            else _default_message(str(metadata["task_file"]))
        )

        with _repository_lock(repo):
            if not _repo_clean(repo):
                raise IntegrationError(
                    "target repository worktree is not clean"
                )
            try:
                branch = _git_text(
                    repo,
                    "symbolic-ref",
                    "--quiet",
                    "HEAD",
                )
            except IntegrationError as exc:
                raise IntegrationError(
                    "target repository must have a checked-out local branch"
                ) from exc
            if not branch.startswith("refs/heads/"):
                raise IntegrationError(
                    "target repository is not on a local branch"
                )

            existing_path = run_dir / INTEGRATION_FILENAME
            if existing_path.is_file():
                existing = read_json(existing_path)
                existing_commit = str(existing.get("commit", ""))
                if (
                    existing.get("schema_version") == 1
                    and existing.get("run_id") == run_dir.name
                    and existing.get("base_commit") == base_commit
                    and existing.get("reviewed_diff_hash")
                    == reviewed_hash
                    and existing.get("branch") == branch
                    and _git_text(repo, "rev-parse", "HEAD")
                    == existing_commit
                    and _git_text(
                        repo,
                        "rev-parse",
                        f"{existing_commit}^",
                    )
                    == base_commit
                ):
                    return {
                        **existing,
                        "result": "already_integrated",
                    }
                raise IntegrationError(
                    "integration record conflicts with repository state"
                )

            if _git_text(repo, "rev-parse", "HEAD") != base_commit:
                raise IntegrationError(
                    "target branch HEAD is not the approved base commit"
                )

            tree, commit = _build_tree_and_commit(
                repo=repo,
                worktree=worktree,
                base_commit=base_commit,
                manifest=manifest,
                message=commit_message,
            )

            # Close the mutable-worktree race immediately before the only ref
            # update. The commit itself was built from hash-checked bytes.
            final_verification = verify_reviewed_snapshot(run_dir)
            if (
                not final_verification.get("matches")
                or final_verification.get("current_diff_hash")
                != reviewed_hash
            ):
                raise IntegrationError(
                    "reviewed snapshot changed while preparing integration"
                )
            if (
                not _repo_clean(repo)
                or _git_text(repo, "rev-parse", "HEAD") != base_commit
            ):
                raise IntegrationError(
                    "target repository changed while preparing integration"
                )

            _git(
                repo,
                "merge",
                "--ff-only",
                "--no-edit",
                "--no-stat",
                commit,
            )
            if (
                _git_text(repo, "rev-parse", "HEAD") != commit
                or not _repo_clean(repo)
            ):
                raise IntegrationError(
                    "local fast-forward did not finish in a clean state"
                )

            record = {
                "schema_version": 1,
                "run_id": run_dir.name,
                "result": "integrated",
                "repo": str(repo),
                "branch": branch,
                "base_commit": base_commit,
                "commit": commit,
                "tree": tree,
                "reviewed_diff_hash": reviewed_hash,
                "integrated_at": utc_now_iso(),
                "remote_operations": False,
            }
            atomic_write_json(
                run_dir / INTEGRATION_FILENAME,
                record,
            )
            return record
