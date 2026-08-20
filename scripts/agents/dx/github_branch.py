"""Publish an approved snapshot to a dedicated GitHub branch."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Sequence

from .approval import utc_now_iso, verify_reviewed_snapshot
from .atomic import atomic_write_json, read_json, run_scoped_lock
from .control_adapter import ControlAdapterError, assert_candidate_transport
from .integration import (
    INTEGRATION_LOCK,
    IntegrationError,
    _build_tree_and_commit,
    _default_message,
    _git,
    _git_text,
    _repo_clean,
    _repository_lock,
    _validate_manifest,
    _validate_message,
)
from .profile import sanitize_text
from .runstate import RunStateError, validate_run


BRANCH_PUBLICATION_FILENAME = "github_branch.json"
BRANCH_PUBLICATION_SCHEMA_VERSION = 1
LEGACY_PULL_REQUEST_FILENAME = "github_pull_request.json"
_GITHUB_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class GitHubBranchError(ValueError):
    """The reviewed snapshot cannot be published safely to GitHub."""


def _external_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AGENT_TELEGRAM_")
    }
    environment.update(
        {
            "GIT_PROTOCOL_FROM_USER": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
        }
    )
    environment.pop("GIT_INDEX_FILE", None)
    return environment


def _run_external(
    command: Sequence[str],
    *,
    cwd: Path,
    input_bytes: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=_external_environment(),
        )
    except OSError as exc:
        raise GitHubBranchError(
            f"cannot execute required command: {Path(command[0]).name}"
        ) from exc
    if check and completed.returncode != 0:
        detail = sanitize_text(
            completed.stderr.decode("utf-8", errors="replace")
        ).strip()
        detail = detail.splitlines()[0][:300] if detail else ""
        label = Path(command[0]).name
        raise GitHubBranchError(
            f"{label} command failed"
            + (f": {detail}" if detail else "")
        )
    return completed


def _github_repository(remote_url: str) -> str:
    value = remote_url.strip()
    match = re.fullmatch(
        r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?",
        value,
    )
    if match is None:
        match = re.fullmatch(
            r"git@github\.com:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?",
            value,
        )
    if match is None:
        match = re.fullmatch(
            r"ssh://git@github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?",
            value,
        )
    if match is None:
        raise GitHubBranchError(
            "approval remote must be an uncredentialed github.com repository URL"
        )
    repository = f"{match.group(1)}/{match.group(2)}"
    if not _GITHUB_REPOSITORY.fullmatch(repository):
        raise GitHubBranchError("GitHub repository name is invalid")
    return repository


def _approval_configuration(metadata: dict[str, Any]) -> tuple[str, str]:
    profile = metadata.get("profile")
    approval = profile.get("approval") if isinstance(profile, dict) else None
    if not isinstance(approval, dict) or approval.get("mode") not in {
        "github_branch",
        "github_pr",
    }:
        raise GitHubBranchError(
            "run is not configured for github_branch approval mode"
        )
    remote = approval.get("remote")
    base_branch = approval.get("base_branch")
    if not isinstance(remote, str) or not isinstance(base_branch, str):
        raise GitHubBranchError("frozen GitHub branch configuration is invalid")
    return remote, base_branch


def _branch_name(run_dir: Path, task_file: str) -> str:
    task = re.sub(r"[^a-z0-9._-]+", "-", Path(task_file).stem.lower()).strip("-.")
    run_id = re.sub(r"[^a-z0-9._-]+", "-", run_dir.name.lower()).strip("-.")
    if not task or not run_id:
        raise GitHubBranchError("cannot derive a safe publication branch")
    value = f"agent-loop/{task}/{run_id}"
    if len(value) > 220:
        raise GitHubBranchError("derived publication branch is too long")
    return value


def _remote_ref(repo: Path, remote: str, ref: str) -> str | None:
    completed = _run_external(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(repo),
            "ls-remote",
            "--refs",
            remote,
            ref,
        ],
        cwd=repo,
    )
    lines = completed.stdout.decode("utf-8", errors="strict").splitlines()
    if not lines:
        return None
    if len(lines) != 1:
        raise GitHubBranchError("remote ref lookup returned multiple values")
    fields = lines[0].split("\t")
    if (
        len(fields) != 2
        or fields[1] != ref
        or not re.fullmatch(r"[0-9a-f]{40,64}", fields[0])
    ):
        raise GitHubBranchError("remote ref lookup returned invalid data")
    return fields[0]


def _push_branch(repo: Path, remote: str, commit: str, branch: str) -> None:
    _run_external(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(repo),
            "push",
            "--porcelain",
            remote,
            f"{commit}:refs/heads/{branch}",
        ],
        cwd=repo,
    )


def _prepared_record_matches(
    record: dict[str, Any],
    *,
    run_dir: Path,
    repo: Path,
    remote: str,
    github_repository: str,
    base_branch: str,
    head_branch: str,
    base_commit: str,
    reviewed_hash: str,
) -> bool:
    return (
        record.get("schema_version") == BRANCH_PUBLICATION_SCHEMA_VERSION
        and record.get("run_id") == run_dir.name
        and record.get("repo") == str(repo)
        and record.get("remote") == remote
        and record.get("github_repository") == github_repository
        and record.get("base_branch") == base_branch
        and record.get("head_branch") == head_branch
        and record.get("base_commit") == base_commit
        and record.get("reviewed_diff_hash") == reviewed_hash
        and isinstance(record.get("commit"), str)
        and isinstance(record.get("tree"), str)
    )


def publish_reviewed_branch(run_dir: Path) -> dict[str, Any]:
    """Create or recover one reviewed commit and publish its dedicated branch."""
    run_dir = Path(run_dir).expanduser()
    with run_scoped_lock(run_dir, lock_name=INTEGRATION_LOCK):
        try:
            metadata = validate_run(run_dir)
            verification = verify_reviewed_snapshot(run_dir)
        except (RunStateError, OSError, ValueError) as exc:
            raise GitHubBranchError(str(exc)) from exc
        if not verification.get("matches"):
            raise GitHubBranchError(
                "live worktree no longer matches reviewed snapshot"
            )

        remote, base_branch = _approval_configuration(metadata)
        repo = Path(str(metadata["repo"])).resolve()
        worktree = Path(str(metadata["worktree"])).resolve()
        base_commit = str(metadata["base_commit"])
        reviewed_hash = str(verification["reviewed_diff_hash"])
        task_file = str(metadata["task_file"])
        head_branch = _branch_name(run_dir, task_file)
        remote_url = _git_text(repo, "remote", "get-url", "--push", remote)
        github_repository = _github_repository(remote_url)

        manifest = _validate_manifest(
            run_dir,
            base_commit=base_commit,
            worktree=worktree,
            reviewed_hash=reviewed_hash,
        )
        try:
            assert_candidate_transport(run_dir, metadata, manifest, worktree)
        except ControlAdapterError as exc:
            raise GitHubBranchError(str(exc)) from exc

        current_record_path = run_dir / BRANCH_PUBLICATION_FILENAME
        legacy_record_path = run_dir / LEGACY_PULL_REQUEST_FILENAME
        if current_record_path.exists() and legacy_record_path.exists():
            raise GitHubBranchError("multiple GitHub publication records exist")
        record_path = (
            legacy_record_path if legacy_record_path.exists() else current_record_path
        )
        with _repository_lock(repo):
            if not _repo_clean(repo):
                raise GitHubBranchError(
                    "target repository worktree is not clean"
                )
            if _git_text(repo, "rev-parse", "HEAD") != base_commit:
                raise GitHubBranchError(
                    "target repository HEAD is not the approved base commit"
                )
            base_remote_oid = _remote_ref(
                repo, remote, f"refs/heads/{base_branch}"
            )
            if base_remote_oid != base_commit:
                raise GitHubBranchError(
                    "GitHub base branch no longer matches the approved base commit"
                )

            record: dict[str, Any] | None = None
            if record_path.is_file():
                try:
                    record = read_json(record_path)
                except (OSError, ValueError) as exc:
                    raise GitHubBranchError(
                        "GitHub branch publication record is invalid"
                    ) from exc
                if not _prepared_record_matches(
                    record,
                    run_dir=run_dir,
                    repo=repo,
                    remote=remote,
                    github_repository=github_repository,
                    base_branch=base_branch,
                    head_branch=head_branch,
                    base_commit=base_commit,
                    reviewed_hash=reviewed_hash,
                ):
                    raise GitHubBranchError(
                        "GitHub branch publication record conflicts with "
                        "the reviewed run"
                    )

            if record is None:
                message = _validate_message(_default_message(task_file))
                try:
                    tree, commit = _build_tree_and_commit(
                        repo=repo,
                        worktree=worktree,
                        base_commit=base_commit,
                        manifest=manifest,
                        message=message,
                    )
                except IntegrationError as exc:
                    raise GitHubBranchError(str(exc)) from exc
                record = {
                    "schema_version": BRANCH_PUBLICATION_SCHEMA_VERSION,
                    "run_id": run_dir.name,
                    "result": "prepared",
                    "repo": str(repo),
                    "remote": remote,
                    "github_repository": github_repository,
                    "base_branch": base_branch,
                    "head_branch": head_branch,
                    "base_commit": base_commit,
                    "commit": commit,
                    "tree": tree,
                    "reviewed_diff_hash": reviewed_hash,
                    "prepared_at": utc_now_iso(),
                    "pushed_at": None,
                    "telegram_operations": False,
                    "remote_operations": False,
                }
                atomic_write_json(record_path, record)

            commit = str(record["commit"])
            tree = str(record["tree"])
            if (
                _git_text(repo, "rev-parse", f"{commit}^") != base_commit
                or _git_text(repo, "rev-parse", f"{commit}^{{tree}}") != tree
            ):
                raise GitHubBranchError(
                    "prepared publication commit is missing or invalid"
                )

            final_verification = verify_reviewed_snapshot(run_dir)
            if (
                not final_verification.get("matches")
                or final_verification.get("current_diff_hash") != reviewed_hash
                or not _repo_clean(repo)
                or _git_text(repo, "rev-parse", "HEAD") != base_commit
            ):
                raise GitHubBranchError(
                    "reviewed or canonical state changed while preparing GitHub publication"
                )

            local_ref = f"refs/heads/{head_branch}"
            try:
                local_oid = _git_text(repo, "rev-parse", "--verify", local_ref)
            except IntegrationError:
                local_oid = None
            if local_oid is None:
                _git(repo, "update-ref", local_ref, commit)
            elif local_oid != commit:
                raise GitHubBranchError(
                    "local publication branch already points to different content"
                )

            remote_oid = _remote_ref(repo, remote, local_ref)
            if remote_oid is None:
                _push_branch(repo, remote, commit, head_branch)
                remote_oid = _remote_ref(repo, remote, local_ref)
            if remote_oid != commit:
                raise GitHubBranchError(
                    "remote publication branch does not match the reviewed commit"
                )
            already_published = record.get("result") in {
                "branch_published",
                "branch_pushed",
                "pull_request_opened",
            }
            record.update(
                {
                    "result": "branch_published",
                    "pushed_at": record.get("pushed_at") or utc_now_iso(),
                    "published_at": record.get("published_at") or utc_now_iso(),
                    "remote_operations": True,
                }
            )
            atomic_write_json(record_path, record)
            if already_published:
                return {**record, "result": "already_published"}
            return record
