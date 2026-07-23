"""Run metadata, safe resume planning, and untrusted evidence attachment."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

from .approval import (
    compute_diff_hash,
    read_status,
    utc_now_iso,
    validate_decision_matches_request,
)
from .atomic import atomic_write_json, read_json, run_scoped_lock
from .profile import ProfileError, load_project_profile


RUN_METADATA = "run.json"
EVIDENCE_MANIFEST = "evidence.json"
MAX_EVIDENCE_BYTES = 1024 * 1024


class RunStateError(ValueError):
    """A run cannot be safely resumed or modified."""


def write_run_metadata(run_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    required = {"repo", "task_file", "base_commit", "worktree", "max_iterations"}
    missing = required - set(payload)
    if missing:
        raise RunStateError(f"run metadata missing: {', '.join(sorted(missing))}")
    document = {"schema_version": 1, "run_id": Path(run_dir).name, **payload}
    atomic_write_json(Path(run_dir) / RUN_METADATA, document)
    return document


def load_run_metadata(run_dir: Path) -> dict[str, Any]:
    path = Path(run_dir) / RUN_METADATA
    try:
        data = read_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RunStateError(f"invalid run metadata: {exc}") from exc
    if data.get("schema_version") != 1 or data.get("run_id") != Path(run_dir).name:
        raise RunStateError("run metadata binding mismatch")
    return data


def _git_output(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], stderr=subprocess.STDOUT, text=True).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RunStateError(f"git validation failed: {' '.join(args)}") from exc


def validate_run(run_dir: Path) -> dict[str, Any]:
    run_candidate = Path(run_dir).expanduser()
    if run_candidate.is_symlink() or not run_candidate.is_dir():
        raise RunStateError("run directory must be a regular directory")
    run_dir = run_candidate.resolve()
    metadata = load_run_metadata(run_dir)
    repo_candidate = Path(str(metadata.get("repo", ""))).expanduser()
    worktree_candidate = Path(str(metadata.get("worktree", ""))).expanduser()
    if repo_candidate.is_symlink() or worktree_candidate.is_symlink():
        raise RunStateError("repository/worktree metadata may not name a symlink")
    repo = repo_candidate.resolve()
    worktree = worktree_candidate.resolve()
    task = str(metadata.get("task_file", ""))
    base = str(metadata.get("base_commit", ""))
    max_iterations = metadata.get("max_iterations")
    if type(max_iterations) is not int or not 1 <= max_iterations <= 5:
        raise RunStateError("invalid max_iterations in run metadata")
    if not repo.is_dir() or not worktree.is_dir() or worktree.is_symlink():
        raise RunStateError("repository or worktree is missing")
    if Path(task).is_absolute() or ".." in Path(task).parts:
        raise RunStateError("unsafe task path in run metadata")
    if _git_output("-C", str(repo), "rev-parse", "--show-toplevel") != str(repo):
        raise RunStateError("repository path no longer matches metadata")
    common_repo = Path(_git_output("-C", str(worktree), "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    expected_common = Path(_git_output("-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    if common_repo != expected_common:
        raise RunStateError("worktree does not belong to the recorded repository")
    if _git_output("-C", str(worktree), "rev-parse", "HEAD") != base:
        raise RunStateError("worktree HEAD/base commit mismatch")
    try:
        subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"{base}:{task}"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError as exc:
        raise RunStateError("task is not tracked in the recorded base commit") from exc
    recorded_profile = metadata.get("profile")
    if not isinstance(recorded_profile, dict):
        raise RunStateError("frozen project profile missing from run metadata")
    try:
        live_profile = load_project_profile(worktree).public_dict()
    except ProfileError as exc:
        raise RunStateError(f"current project profile is invalid: {exc}") from exc
    frozen = dict(recorded_profile)
    frozen["profile_path"] = None
    live_profile["profile_path"] = None
    if live_profile != frozen:
        raise RunStateError("project profile changed after run creation")
    return {**metadata, "run_dir": str(run_dir), "status": read_status(run_dir)}


def _review_hash(run_dir: Path, iteration: int) -> str | None:
    path = run_dir / f"review-{iteration}-snapshot.json"
    if not path.is_file():
        return None
    try:
        data = read_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RunStateError(f"invalid review snapshot: {exc}") from exc
    value = data.get("diff_hash")
    return str(value) if value else None


def plan_resume(run_dir: Path, *, review_only: bool = False) -> dict[str, Any]:
    run_dir = Path(run_dir).expanduser().resolve()
    metadata = validate_run(run_dir)
    status = metadata["status"]
    worktree = Path(metadata["worktree"])
    base = str(metadata["base_commit"])
    try:
        iteration = int((run_dir / "iteration").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        iteration = 1
    if iteration < 1 or iteration > int(metadata["max_iterations"]):
        raise RunStateError("invalid iteration in run")

    if status == "HUMAN_APPROVED":
        validate_decision_matches_request(run_dir)
        phase = "complete"
    elif status == "AWAITING_HUMAN_APPROVAL":
        request = read_json(run_dir / "human_approval_request.json")
        if compute_diff_hash(worktree, base) != request.get("diff_hash"):
            raise RunStateError("worktree changed after reviewed approval request")
        phase = "awaiting_human"
    elif status == "APPROVED":
        # A bare technical approval is not resumable into the human gate. Run a
        # fresh reviewer so copied/standalone reports can never promote state.
        snapshot = _review_hash(run_dir, iteration)
        if snapshot and compute_diff_hash(worktree, base) != snapshot:
            raise RunStateError("worktree changed after technical review")
        phase = "reviewer"
    elif status == "CHANGES_REQUESTED":
        snapshot = _review_hash(run_dir, iteration)
        if snapshot and compute_diff_hash(worktree, base) != snapshot:
            raise RunStateError("worktree changed after CHANGES_REQUESTED review")
        phase = "reviewer" if review_only else "executor"
        if not review_only:
            iteration += 1
    elif status in {"EXECUTING", "REVIEWING", "BLOCKED"}:
        cursor_report = run_dir / f"cursor-{iteration}.json"
        review_report = run_dir / f"review-{iteration}.json"
        snapshot = _review_hash(run_dir, iteration)
        current = compute_diff_hash(worktree, base)
        if snapshot and current != snapshot:
            raise RunStateError("worktree changed during or after interrupted review")
        reviewer_result = run_dir / f"reviewer-{iteration}-result.json"
        if review_only:
            phase = "reviewer"
        elif snapshot and cursor_report.is_file() and cursor_report.stat().st_size > 0:
            # The pre-review hash binds the snapshot. Whether the reviewer was
            # interrupted, timed out, or left an empty file, rerun that review.
            phase = "reviewer"
        elif status == "REVIEWING" and cursor_report.is_file() and cursor_report.stat().st_size > 0:
            phase = "reviewer"
        elif reviewer_result.is_file() and review_report.is_file() and review_report.stat().st_size > 0:
            phase = "reviewer"
        else:
            phase = "executor"
    else:
        raise RunStateError(f"run status is not resumable: {status!r}")
    return {**metadata, "resume_phase": phase, "iteration": iteration, "review_only": review_only}


def attach_evidence(run_dir: Path, source: Path, *, max_bytes: int = MAX_EVIDENCE_BYTES) -> dict[str, Any]:
    if max_bytes < 1:
        raise RunStateError("evidence size limit must be positive")
    run_dir = Path(run_dir).expanduser().resolve()
    validate_run(run_dir)
    source = Path(source).expanduser()
    try:
        before = source.lstat()
    except OSError as exc:
        raise RunStateError(f"evidence file not found: {source}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise RunStateError("evidence must be a regular non-symlink file")
    if before.st_size > max_bytes:
        raise RunStateError(f"evidence exceeds {max_bytes} bytes")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(source, flags)
    except OSError as exc:
        raise RunStateError(f"cannot safely open evidence: {exc}") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise RunStateError("evidence type changed while opening")
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise RunStateError("evidence inode changed while opening")
        if opened.st_size > max_bytes:
            raise RunStateError(f"evidence exceeds {max_bytes} bytes")
        chunks: list[bytes] = []
        total = 0
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, min(65536, max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise RunStateError(f"evidence exceeds {max_bytes} bytes")
            digest.update(chunk)
            chunks.append(chunk)
        after = os.fstat(fd)
        if (
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        ):
            raise RunStateError("evidence changed while reading")
    finally:
        os.close(fd)
    sha256 = digest.hexdigest()
    safe_name = "".join(char if char.isalnum() or char in "._-" else "-" for char in source.name)[:80]
    safe_name = safe_name.strip(".-") or "evidence"
    evidence_dir = run_dir / "evidence"
    destination = evidence_dir / f"{sha256[:16]}-{safe_name}"
    with run_scoped_lock(run_dir, lock_name=".resume.lock"):
        evidence_dir.mkdir(mode=0o700, exist_ok=True)
        try:
            destination.lstat()
            destination_present = True
        except FileNotFoundError:
            destination_present = False
        if destination_present:
            try:
                existing_fd = os.open(
                    destination,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                )
            except OSError as exc:
                raise RunStateError("evidence destination was tampered with") from exc
            try:
                existing_stat = os.fstat(existing_fd)
                if not stat.S_ISREG(existing_stat.st_mode):
                    raise RunStateError("evidence destination was tampered with")
                existing_digest = hashlib.sha256()
                while True:
                    chunk = os.read(existing_fd, 65536)
                    if not chunk:
                        break
                    existing_digest.update(chunk)
                existing = existing_digest.hexdigest()
            finally:
                os.close(existing_fd)
            if existing != sha256:
                raise RunStateError("evidence destination hash mismatch")
        else:
            temp = evidence_dir / f".{destination.name}.tmp-{os.getpid()}"
            try:
                with temp.open("xb") as handle:
                    os.chmod(temp, 0o600)
                    for chunk in chunks:
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, destination)
            finally:
                temp.unlink(missing_ok=True)
        manifest_path = run_dir / EVIDENCE_MANIFEST
        if manifest_path.is_file():
            manifest = read_json(manifest_path)
        else:
            manifest = {"schema_version": 1, "items": []}
        items = manifest.get("items")
        if not isinstance(items, list):
            raise RunStateError("invalid evidence manifest")
        entry = {
            "name": destination.name,
            "sha256": sha256,
            "size_bytes": total,
            "attached_at": utc_now_iso(),
            "trust": "untrusted",
        }
        if not any(isinstance(item, dict) and item.get("sha256") == sha256 for item in items):
            items.append(entry)
            atomic_write_json(manifest_path, manifest)
        return entry
