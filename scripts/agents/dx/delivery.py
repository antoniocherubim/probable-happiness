"""Opt-in, post-approval delivery of the exact reviewed snapshot to a branch."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any, Sequence

from .approval import (
    STATUS_DELIVERING,
    STATUS_DELIVERY_FAILED,
    STATUS_HUMAN_APPROVED,
    STATUS_PUSHED,
    compute_diff_hash,
    enqueue_notification,
    read_status,
    utc_now_iso,
    validate_decision_matches_request,
    write_status,
)
from .atomic import atomic_write_json, read_json, run_scoped_lock
from .profile import ProjectProfile, sanitize_text
from .runstate import RunStateError, validate_run
from .snapshot import MANIFEST_FILENAME, SnapshotError, build_snapshot_manifest, split_telegram_message


DELIVERY_FILENAME = "delivery.json"


class DeliveryError(RuntimeError):
    """Delivery is unsafe or failed without changing the approved snapshot."""


def _git(
    repo: Path,
    args: Sequence[str],
    *,
    environment: dict[str, str] | None = None,
    input_bytes: bytes | None = None,
) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )
    except OSError as exc:
        raise DeliveryError(f"git command unavailable: {args[0]}") from exc
    if completed.returncode != 0:
        raise DeliveryError(f"git command failed: {args[0]}")
    return completed.stdout.decode("utf-8", errors="replace").strip()


def _task_title(worktree: Path, task_file: str, task_id: str) -> str:
    try:
        for line in (worktree / task_file).read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("# "):
                title = sanitize_text(line.strip()[2:]).replace("\n", " ").strip()
                title = re.sub(
                    rf"^{re.escape(task_id)}\s*(?:—|-|:)\s*",
                    "",
                    title,
                    count=1,
                    flags=re.IGNORECASE,
                )
                if title:
                    return title[:180]
    except (OSError, UnicodeError):
        pass
    return task_id


def _github_web_base(remote_url: str) -> str | None:
    value = remote_url.strip()
    match = re.fullmatch(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?", value)
    if not match:
        match = re.fullmatch(r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?", value)
    if not match:
        match = re.fullmatch(r"ssh://git@github\.com/([^/]+)/([^/]+?)(?:\.git)?", value)
    if not match:
        return None
    owner, repository = match.groups()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+", repository
    ):
        return None
    return f"https://github.com/{owner}/{repository}"


def freeze_delivery_config(
    *,
    repo: Path,
    worktree: Path,
    base_commit: str,
    task_file: str,
    task_id: str,
    task_slug: str,
    profile: ProjectProfile,
) -> dict[str, Any]:
    if profile.delivery_mode == "none":
        return {"mode": "none"}
    if profile.delivery_mode != "push_branch" or not profile.delivery_push_after_human_approval:
        raise DeliveryError("automatic delivery is not explicitly enabled")
    title = _task_title(worktree, task_file, task_id)
    fields = {
        "task_id": task_id.lower(),
        "task_slug": task_slug.lower(),
        "task_title": title,
    }
    branch = profile.delivery_branch_template.format(**fields).lower()
    if branch in {"main", "master", profile.delivery_base_branch.lower()}:
        raise DeliveryError("delivery branch may not be main, master, or the configured base branch")
    if not branch or branch.startswith("-") or branch.endswith("."):
        raise DeliveryError("delivery branch is empty or reserved")
    _git(repo, ["check-ref-format", "--branch", branch])
    _git(repo, ["check-ref-format", f"refs/heads/{profile.delivery_base_branch}"])
    commit_message = profile.delivery_commit_message_template.format(**fields)
    commit_message = " ".join(commit_message.splitlines()).strip()
    if not commit_message or len(commit_message) > 500:
        raise DeliveryError("rendered commit message is invalid")
    remote_urls = _git(
        repo, ["remote", "get-url", "--push", "--all", profile.delivery_remote]
    ).splitlines()
    if len(remote_urls) != 1:
        raise DeliveryError("delivery remote must have exactly one push URL")
    remote_url = remote_urls[0]
    remote_url_hash = hashlib.sha256(remote_url.encode("utf-8")).hexdigest()
    base_candidates = (
        f"refs/heads/{profile.delivery_base_branch}",
        f"refs/remotes/{profile.delivery_remote}/{profile.delivery_base_branch}",
    )
    if not any(
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", candidate],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        ).stdout.decode().strip()
        == base_commit
        for candidate in base_candidates
    ):
        raise DeliveryError("configured base branch does not match the frozen base commit")
    try:
        _git(worktree, ["var", "GIT_AUTHOR_IDENT"])
        _git(worktree, ["var", "GIT_COMMITTER_IDENT"])
    except DeliveryError as exc:
        raise DeliveryError("Git author/committer identity is not configured") from exc
    return {
        "mode": "push_branch",
        "remote": profile.delivery_remote,
        "base_branch": profile.delivery_base_branch,
        "branch": branch,
        "commit_message": commit_message,
        "remote_url_hash": remote_url_hash,
        "github_web_base": _github_web_base(remote_url),
        "push_after_human_approval": True,
    }


def _entry_bytes(worktree: Path, entry: dict[str, Any]) -> bytes:
    relative = str(entry["path"])
    path = worktree / relative
    if entry.get("kind") == "symlink":
        if not path.is_symlink():
            raise DeliveryError(f"reviewed symlink changed: {relative}")
        data = os.fsencode(os.readlink(path))
    else:
        try:
            info = path.lstat()
        except OSError as exc:
            raise DeliveryError(f"reviewed file disappeared: {relative}") from exc
        if not stat.S_ISREG(info.st_mode):
            raise DeliveryError(f"reviewed entry is no longer regular: {relative}")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                info.st_dev,
                info.st_ino,
            ):
                raise DeliveryError(f"reviewed file changed while opening: {relative}")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            data = b"".join(chunks)
            after = os.fstat(fd)
            if (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
                raise DeliveryError(f"reviewed file changed while reading: {relative}")
        finally:
            os.close(fd)
    if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
        raise DeliveryError(f"reviewed content hash changed: {relative}")
    return data


def _manifest_binding(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": document.get("schema_version"),
        "base_commit": document.get("base_commit"),
        "snapshot_hash": document.get("snapshot_hash"),
        "entries": document.get("entries"),
    }


def _build_tree(
    *,
    repo: Path,
    worktree: Path,
    run_dir: Path,
    base_commit: str,
    manifest: dict[str, Any],
) -> str:
    index_path = run_dir / "delivery.index"
    index_path.unlink(missing_ok=True)
    environment = {**os.environ, "GIT_INDEX_FILE": str(index_path)}
    _git(repo, ["read-tree", base_commit], environment=environment)
    for entry in manifest.get("entries", []):
        if not isinstance(entry, dict):
            raise DeliveryError("invalid reviewed manifest entry")
        relative = str(entry.get("path", ""))
        if entry.get("operation") == "delete":
            _git(repo, ["update-index", "--force-remove", "--", relative], environment=environment)
            continue
        data = _entry_bytes(worktree, entry)
        blob = _git(repo, ["hash-object", "-w", "--stdin"], input_bytes=data)
        _git(
            repo,
            ["update-index", "--add", "--cacheinfo", str(entry["mode"]), blob, relative],
            environment=environment,
        )
    return _git(repo, ["write-tree"], environment=environment)


def _remote_oid(repo: Path, remote: str, branch: str) -> str | None:
    output = _git(repo, ["ls-remote", "--heads", remote, f"refs/heads/{branch}"])
    if not output:
        return None
    lines = output.splitlines()
    if len(lines) != 1:
        raise DeliveryError("remote returned an ambiguous branch ref")
    oid, _separator, ref = lines[0].partition("\t")
    if ref != f"refs/heads/{branch}" or not re.fullmatch(r"[0-9a-fA-F]{40,64}", oid):
        raise DeliveryError("remote returned an invalid branch ref")
    return oid.lower()


def _write_delivery_failure(
    run_dir: Path,
    frozen: dict[str, Any],
    reason: str,
    existing: dict[str, Any] | None = None,
) -> None:
    write_status(run_dir, STATUS_DELIVERY_FAILED)
    payload = {
        **(existing or {}),
        "schema_version": 1,
        "status": STATUS_DELIVERY_FAILED,
        "reason": reason,
        "remote": frozen.get("remote"),
        "branch": frozen.get("branch"),
        "base_commit": (existing or {}).get("base_commit"),
        "reviewed_diff_hash": (existing or {}).get("reviewed_diff_hash"),
        "commit_oid": (existing or {}).get("commit_oid"),
        "tree_oid": (existing or {}).get("tree_oid"),
        "failed_at": utc_now_iso(),
    }
    atomic_write_json(run_dir / DELIVERY_FILENAME, payload)
    try:
        enqueue_notification(
            run_dir=run_dir,
            kind="delivery_failed",
            summary=f"Entrega da branch {frozen.get('branch')} falhou: {reason}",
        )
    except (OSError, ValueError, json.JSONDecodeError):
        pass


def deliver_run(run_dir: Path) -> dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    with run_scoped_lock(run_dir, lock_name=".delivery.lock"):
        try:
            metadata = validate_run(run_dir)
        except RunStateError as exc:
            reason = str(exc)
            write_status(run_dir, STATUS_DELIVERY_FAILED)
            atomic_write_json(
                run_dir / DELIVERY_FILENAME,
                {
                    "schema_version": 1,
                    "status": STATUS_DELIVERY_FAILED,
                    "reason": reason,
                    "failed_at": utc_now_iso(),
                },
            )
            try:
                enqueue_notification(
                    run_dir=run_dir,
                    kind="delivery_failed",
                    summary=f"Entrega bloqueada pela validação segura do run: {reason}",
                )
            except (OSError, ValueError, json.JSONDecodeError):
                pass
            raise DeliveryError(reason) from exc
        frozen = metadata.get("delivery")
        if not isinstance(frozen, dict) or frozen.get("mode") == "none":
            return {"status": "disabled"}
        if frozen.get("mode") != "push_branch":
            raise DeliveryError("invalid frozen delivery mode")
        existing = read_json(run_dir / DELIVERY_FILENAME) if (run_dir / DELIVERY_FILENAME).is_file() else None
        if read_status(run_dir) == STATUS_PUSHED and existing:
            return existing
        progress = existing
        try:
            decision = validate_decision_matches_request(run_dir)
            request = read_json(run_dir / "human_approval_request.json")
            repo = Path(metadata["repo"])
            worktree = Path(metadata["worktree"])
            base_commit = str(metadata["base_commit"])
            if _git(worktree, ["rev-parse", "HEAD"]) != base_commit:
                raise DeliveryError("worktree HEAD no longer matches the frozen base")
            current_hash = compute_diff_hash(worktree, base_commit)
            if current_hash != request.get("diff_hash") or current_hash != decision.get("diff_hash"):
                raise DeliveryError("approved_snapshot_changed")
            recorded_manifest = read_json(run_dir / MANIFEST_FILENAME)
            current_manifest = build_snapshot_manifest(worktree, base_commit)
            if _manifest_binding(recorded_manifest) != _manifest_binding(current_manifest):
                raise DeliveryError("reviewed_manifest_changed")
            remote_urls = _git(
                repo,
                ["remote", "get-url", "--push", "--all", str(frozen["remote"])],
            ).splitlines()
            if len(remote_urls) != 1:
                raise DeliveryError("delivery_remote_changed")
            remote_url = remote_urls[0]
            if hashlib.sha256(remote_url.encode("utf-8")).hexdigest() != frozen.get("remote_url_hash"):
                raise DeliveryError("delivery_remote_changed")
            write_status(run_dir, STATUS_DELIVERING)
            tree_oid = _build_tree(
                repo=repo,
                worktree=worktree,
                run_dir=run_dir,
                base_commit=base_commit,
                manifest=recorded_manifest,
            )
            commit_oid = str((existing or {}).get("commit_oid") or "")
            if commit_oid:
                if _git(repo, ["rev-parse", f"{commit_oid}^{{tree}}"] ) != tree_oid:
                    raise DeliveryError("recorded delivery commit tree mismatch")
                if _git(repo, ["rev-parse", f"{commit_oid}^"]) != base_commit:
                    raise DeliveryError("recorded delivery commit parent mismatch")
            else:
                commit_oid = _git(
                    worktree,
                    ["commit-tree", tree_oid, "-p", base_commit, "-m", str(frozen["commit_message"])],
                )
            if compute_diff_hash(worktree, base_commit) != request.get("diff_hash"):
                raise DeliveryError("snapshot_changed_before_branch")
            branch = str(frozen["branch"])
            progress = {
                **(existing or {}),
                "schema_version": 1,
                "status": STATUS_DELIVERING,
                "branch": branch,
                "remote": frozen["remote"],
                "base_branch": frozen["base_branch"],
                "base_commit": base_commit,
                "reviewed_diff_hash": request["diff_hash"],
                "commit_oid": commit_oid,
                "tree_oid": tree_oid,
                "approved_at": decision.get("decided_at"),
                "delivery_started_at": (existing or {}).get("delivery_started_at")
                or utc_now_iso(),
            }
            atomic_write_json(run_dir / DELIVERY_FILENAME, progress)
            local_ref = f"refs/heads/{branch}"
            local = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", local_ref],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                text=True,
            ).stdout.strip()
            if local and local != commit_oid:
                raise DeliveryError("local_branch_exists")
            if not local:
                _git(repo, ["update-ref", local_ref, commit_oid, "0" * 40])
            remote_oid = _remote_oid(repo, str(frozen["remote"]), branch)
            if remote_oid and remote_oid != commit_oid:
                raise DeliveryError("remote_branch_exists")
            remote_was_existing = remote_oid == commit_oid
            if not remote_oid:
                if compute_diff_hash(worktree, base_commit) != request.get("diff_hash"):
                    raise DeliveryError("snapshot_changed_before_push")
                _git(
                    repo,
                    ["push", str(frozen["remote"]), f"{commit_oid}:refs/heads/{branch}"],
                )
                remote_oid = _remote_oid(repo, str(frozen["remote"]), branch)
            if remote_oid != commit_oid:
                raise DeliveryError("remote_oid_mismatch")
            web = frozen.get("github_web_base")
            branch_url = compare_url = None
            if isinstance(web, str) and web.startswith("https://github.com/"):
                branch_url = f"{web}/tree/{urllib.parse.quote(branch, safe='')}"
                compare_url = (
                    f"{web}/compare/{urllib.parse.quote(str(frozen['base_branch']), safe='')}"
                    f"...{urllib.parse.quote(branch, safe='')}"
                )
            payload = {
                "schema_version": 1,
                "task_id": metadata.get("task_file", "").rsplit("/", 1)[-1].rsplit(".", 1)[0],
                "status": STATUS_PUSHED,
                "branch": branch,
                "remote": frozen["remote"],
                "base_branch": frozen["base_branch"],
                "base_commit": base_commit,
                "reviewed_diff_hash": request["diff_hash"],
                "commit_oid": commit_oid,
                "tree_oid": tree_oid,
                "remote_oid": remote_oid,
                "approved_at": decision.get("decided_at"),
                "delivered_at": utc_now_iso(),
                "push_result": "idempotent" if remote_was_existing else "pushed",
                "branch_url": branch_url,
                "compare_url": compare_url,
            }
            atomic_write_json(run_dir / DELIVERY_FILENAME, payload)
            lines = [
                f"{payload['task_id']} publicada com sucesso",
                "",
                f"Branch: {branch}",
                f"Commit: {commit_oid[:12]}",
                f"Remote: {frozen['remote']}",
                "Status: pronta para revisão e merge",
            ]
            message = sanitize_text("\n".join(lines))
            if branch_url:
                message += (
                    f"\n\nAbrir no GitHub:\n{branch_url}"
                    f"\n\nComparar com {frozen['base_branch']}:\n{compare_url or ''}"
                )
            try:
                enqueue_notification(
                    run_dir=run_dir,
                    kind="pushed",
                    summary=message,
                    messages=split_telegram_message(message),
                )
            except (OSError, ValueError, json.JSONDecodeError):
                # The remote OID was confirmed; notification is best-effort and
                # must never downgrade a successful delivery.
                pass
            write_status(run_dir, STATUS_PUSHED)
            return payload
        except (DeliveryError, RunStateError, SnapshotError, OSError, ValueError, json.JSONDecodeError) as exc:
            reason = str(exc)
            _write_delivery_failure(run_dir, frozen, reason, progress)
            raise DeliveryError(reason) from exc
