"""Reviewed snapshot manifest, documentation policy, and safe Telegram summary."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any, Iterable

from .approval import compute_diff_hash, utc_now_iso
from .atomic import atomic_write_json, read_json
from .profile import ProjectProfile, sanitize_text


MANIFEST_FILENAME = "reviewed_manifest.json"
SUMMARY_FILENAME = "technical_summary.json"
TELEGRAM_CHUNK_LIMIT = 3500

_TECHNICAL_SUMMARY_KEYS = frozenset(
    {
        "schema_version",
        "task_id",
        "task_title",
        "repository",
        "base_commit",
        "iteration",
        "max_iterations",
        "reviewed_diff_hash",
        "files",
        "file_count",
        "additions",
        "deletions",
        "executor_summary",
        "test_counts",
        "test_commands",
        "validation_status",
        "reviewer_status",
        "reviewer_summary",
        "findings",
        "residual_risks",
        "documentation",
        "prepared_at",
        "telegram_messages",
    }
)
_TECHNICAL_SUMMARY_REQUIRED = frozenset(
    {
        "schema_version",
        "task_id",
        "task_title",
        "repository",
        "base_commit",
        "iteration",
        "max_iterations",
        "reviewed_diff_hash",
        "files",
        "file_count",
        "additions",
        "deletions",
        "executor_summary",
        "test_counts",
        "test_commands",
        "validation_status",
        "reviewer_status",
        "reviewer_summary",
        "findings",
        "residual_risks",
        "documentation",
        "prepared_at",
    }
)


class SnapshotError(ValueError):
    """The working snapshot is unsafe, incomplete, or no longer reviewed."""


def validate_technical_summary(document: dict[str, Any]) -> dict[str, Any]:
    """Refuse malformed, unknown-field, or future technical summaries."""
    from .schemas import FUTURE_SCHEMA_REFUSAL

    if not isinstance(document, dict):
        raise SnapshotError("technical summary must be a JSON object")
    unknown = set(document) - _TECHNICAL_SUMMARY_KEYS
    if unknown:
        raise SnapshotError("technical summary has unknown fields")
    if not _TECHNICAL_SUMMARY_REQUIRED.issubset(document):
        raise SnapshotError("technical summary missing required fields")
    schema = document.get("schema_version")
    if type(schema) is int and schema > 1:
        raise SnapshotError(FUTURE_SCHEMA_REFUSAL)
    if schema != 1:
        raise SnapshotError("technical summary schema_version mismatch")

    def _require_str(key: str) -> str:
        value = document.get(key)
        if not isinstance(value, str) or not value:
            raise SnapshotError(f"technical summary field types are invalid ({key})")
        return value

    def _require_int(key: str, *, min_value: int | None = None) -> int:
        value = document.get(key)
        if type(value) is not int:
            raise SnapshotError(f"technical summary field types are invalid ({key})")
        if min_value is not None and value < min_value:
            raise SnapshotError(f"technical summary field types are invalid ({key})")
        return value

    for key in (
        "task_id",
        "task_title",
        "repository",
        "base_commit",
        "reviewed_diff_hash",
        "executor_summary",
        "validation_status",
        "reviewer_status",
        "reviewer_summary",
        "prepared_at",
    ):
        _require_str(key)
    _require_int("iteration", min_value=1)
    _require_int("max_iterations", min_value=1)
    _require_int("file_count", min_value=0)
    _require_int("additions", min_value=0)
    _require_int("deletions", min_value=0)
    files = document.get("files")
    if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
        raise SnapshotError("technical summary field types are invalid (files)")
    if len(files) != document["file_count"]:
        raise SnapshotError("technical summary file_count mismatch")
    test_counts = document.get("test_counts")
    if not isinstance(test_counts, dict) or not all(
        type(value) is int for value in test_counts.values()
    ):
        raise SnapshotError("technical summary field types are invalid (test_counts)")
    test_commands = document.get("test_commands")
    if not isinstance(test_commands, list) or not all(
        isinstance(item, str) for item in test_commands
    ):
        raise SnapshotError("technical summary field types are invalid (test_commands)")
    for key in ("findings", "residual_risks", "documentation"):
        value = document.get(key)
        if not isinstance(value, list):
            raise SnapshotError(f"technical summary field types are invalid ({key})")
    messages = document.get("telegram_messages")
    if messages is not None:
        if not isinstance(messages, list) or not messages:
            raise SnapshotError("technical summary telegram_messages malformed")
        if not all(isinstance(item, str) and item for item in messages):
            raise SnapshotError("technical summary telegram_messages malformed")
    return document


def _git_bytes(worktree: Path, *args: str) -> bytes:
    try:
        return subprocess.check_output(
            ["git", "-C", str(worktree), *args],
            stderr=subprocess.STDOUT,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SnapshotError(f"git snapshot command failed: {' '.join(args)}") from exc


def _safe_relative(raw: bytes) -> str:
    value = raw.decode("utf-8", errors="surrogateescape")
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts or value.startswith(".git/"):
        raise SnapshotError(f"unsafe snapshot path: {value!r}")
    return value


def _read_entry(worktree: Path, relative: str) -> dict[str, Any]:
    path = worktree / relative
    try:
        info = path.lstat()
    except OSError as exc:
        raise SnapshotError(f"snapshot entry disappeared: {relative}") from exc
    if stat.S_ISLNK(info.st_mode):
        target = os.readlink(path)
        data = os.fsencode(target)
        return {
            "path": relative,
            "operation": "upsert",
            "kind": "symlink",
            "mode": "120000",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
        }
    if not stat.S_ISREG(info.st_mode):
        raise SnapshotError(f"special file is forbidden in reviewed snapshot: {relative}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SnapshotError(f"cannot safely open snapshot entry: {relative}") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise SnapshotError(f"snapshot entry changed while opening: {relative}")
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
        after = os.fstat(fd)
        if (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
            raise SnapshotError(f"snapshot entry changed while reading: {relative}")
    finally:
        os.close(fd)
    return {
        "path": relative,
        "operation": "upsert",
        "kind": "regular",
        "mode": "100755" if info.st_mode & 0o111 else "100644",
        "sha256": digest.hexdigest(),
        "size_bytes": size,
    }


def reject_nonignored_special_files(worktree: Path) -> None:
    """Reject repository special files even when Git omits them from ls-files."""
    root = Path(worktree).resolve()
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        kept: list[str] = []
        for name in directories:
            candidate = current_path / name
            relative = candidate.relative_to(root).as_posix()
            ignored = subprocess.run(
                ["git", "-C", str(root), "check-ignore", "--quiet", "--", f"{relative}/"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode == 0
            if not ignored:
                kept.append(name)
        directories[:] = kept
        for name in files:
            candidate = current_path / name
            try:
                info = candidate.lstat()
            except OSError as exc:
                raise SnapshotError(f"cannot inspect repository entry: {candidate}") from exc
            if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                continue
            relative = candidate.relative_to(root).as_posix()
            ignored = subprocess.run(
                ["git", "-C", str(root), "check-ignore", "--quiet", "--", relative],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode == 0
            if not ignored:
                raise SnapshotError(f"special file is forbidden in reviewed snapshot: {relative}")


def build_snapshot_manifest(worktree: Path, base_commit: str) -> dict[str, Any]:
    worktree = Path(worktree).resolve()
    reject_nonignored_special_files(worktree)
    changed_raw = _git_bytes(
        worktree,
        "diff",
        "--name-status",
        "-z",
        "--no-renames",
        base_commit,
        "--",
    ).split(b"\0")
    changed = [item for item in changed_raw if item]
    if len(changed) % 2:
        raise SnapshotError("invalid git name-status output")
    operations: dict[str, str] = {}
    for index in range(0, len(changed), 2):
        status_code = changed[index].decode("ascii", errors="replace")
        relative = _safe_relative(changed[index + 1])
        if status_code not in {"A", "M", "D", "T", "U"}:
            raise SnapshotError(f"unsupported Git status {status_code!r} for {relative}")
        if status_code == "U":
            raise SnapshotError(f"unmerged path in snapshot: {relative}")
        operations[relative] = "delete" if status_code == "D" else "upsert"
    untracked = _git_bytes(
        worktree,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
    ).split(b"\0")
    for raw in untracked:
        if raw:
            operations[_safe_relative(raw)] = "upsert"

    entries: list[dict[str, Any]] = []
    for relative, operation in sorted(operations.items()):
        if operation == "delete":
            entries.append({"path": relative, "operation": "delete"})
        else:
            entries.append(_read_entry(worktree, relative))
    return {
        "schema_version": 1,
        "base_commit": base_commit,
        "snapshot_hash": compute_diff_hash(worktree, base_commit),
        "entries": entries,
        "created_at": utc_now_iso(),
    }


def _render_documentation_paths(
    profile: ProjectProfile,
    *,
    task_id: str,
    task_slug: str,
) -> tuple[str, ...]:
    rendered: list[str] = []
    for template in profile.documentation_paths:
        value = template.format(task_id=task_id, task_slug=task_slug)
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or not value:
            raise SnapshotError(f"unsafe rendered documentation path: {value!r}")
        rendered.append(value)
    return tuple(rendered)


def validate_documentation(
    profile: ProjectProfile,
    manifest: dict[str, Any],
    *,
    task_id: str,
    task_slug: str,
) -> list[str]:
    required = _render_documentation_paths(profile, task_id=task_id, task_slug=task_slug)
    changed = {
        str(entry.get("path"))
        for entry in manifest.get("entries", [])
        if isinstance(entry, dict) and entry.get("operation") == "upsert"
    }
    documented = [path for path in required if path in changed]
    if profile.documentation_required:
        missing = [path for path in required if path not in changed]
        if missing:
            raise SnapshotError(
                "required documentation was not created or updated: " + ", ".join(missing)
            )
    return documented


def _task_title(worktree: Path, task_file: str, task_id: str) -> str:
    from .persist import PersistError, secure_read_text

    path = worktree / task_file
    try:
        text = secure_read_text(
            path,
            max_bytes=256 * 1024,
            require_private=False,
            containment_root=worktree,
        )
    except PersistError as exc:
        message = str(exc).lower()
        if "missing" in message:
            return task_id
        raise SnapshotError(f"task file cannot be read safely: {exc}") from exc
    except FileNotFoundError:
        return task_id
    except (OSError, UnicodeError) as exc:
        raise SnapshotError(f"task file cannot be read safely: {exc}") from exc
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            title = re.sub(
                rf"^{re.escape(task_id)}\s*(?:—|-|:)\s*",
                "",
                title,
                count=1,
                flags=re.IGNORECASE,
            )
            if title:
                return sanitize_text(title)[:240]
    return task_id


_VALIDATION_RESULT_KEYS = frozenset(
    {
        "schema_version",
        "phase",
        "iteration",
        "state",
        "reason",
        "exit_code",
        "child_exit_code",
        "elapsed_seconds",
        "last_activity_at",
        "changed_files",
        "finished_at",
    }
)

# Production Cursor Agent ``--output-format json`` result envelope (same contract
# as resume/plan_resume). Synthetic ``{"summary": …}`` fixtures are refused.
_CURSOR_AGENT_RESULT_KEYS = frozenset(
    {
        "type",
        "subtype",
        "is_error",
        "duration_ms",
        "duration_api_ms",
        "result",
        "session_id",
        "request_id",
        "usage",
    }
)
_REVIEWER_REPORT_KEYS = frozenset(
    {"status", "summary", "findings", "tests_required"}
)
_REVIEWER_STATUSES = frozenset({"APPROVED", "CHANGES_REQUESTED", "BLOCKED"})
_FINDING_KEYS = frozenset({"severity", "title", "details", "files"})
_FINDING_SEVERITIES = frozenset({"critical", "high", "medium", "low"})


def _validate_validation_result(path: Path, data: Any) -> dict[str, Any]:
    """Fail closed on corrupt/future/mistyped validation-*-result.json payloads."""
    from .schemas import FUTURE_SCHEMA_REFUSAL

    label = path.name
    if not isinstance(data, dict):
        raise SnapshotError(f"{label} must be a JSON object")
    unknown = set(data) - _VALIDATION_RESULT_KEYS
    if unknown:
        raise SnapshotError(f"{label} has unknown fields")
    schema = data.get("schema_version")
    if type(schema) is int and schema > 1:
        raise SnapshotError(FUTURE_SCHEMA_REFUSAL)
    if schema != 1:
        raise SnapshotError(f"{label} schema_version mismatch")
    if not _VALIDATION_RESULT_KEYS.issubset(data):
        raise SnapshotError(f"{label} missing required fields")
    if (
        not isinstance(data.get("phase"), str)
        or not data["phase"]
        or type(data.get("iteration")) is not int
        or data["iteration"] < 1
        or not isinstance(data.get("state"), str)
        or not data["state"]
        or (
            data.get("reason") is not None
            and not isinstance(data.get("reason"), str)
        )
        or type(data.get("exit_code")) is not int
        or type(data.get("child_exit_code")) is not int
        or type(data.get("elapsed_seconds")) not in {int, float}
        or not isinstance(data.get("last_activity_at"), str)
        or not data["last_activity_at"]
        or type(data.get("changed_files")) is not int
        or not isinstance(data.get("finished_at"), str)
        or not data["finished_at"]
    ):
        raise SnapshotError(f"{label} field types are invalid")
    return data


def validate_reviewer_report(data: Any) -> dict[str, Any]:
    """Fail closed on missing/unknown/mistyped reviewer report contracts."""
    if not isinstance(data, dict) or set(data) != _REVIEWER_REPORT_KEYS:
        raise SnapshotError("reviewer report has missing or unknown fields")
    if data["status"] not in _REVIEWER_STATUSES:
        raise SnapshotError("invalid reviewer status")
    if (
        not isinstance(data["summary"], str)
        or not isinstance(data["tests_required"], list)
        or not all(isinstance(item, str) for item in data["tests_required"])
    ):
        raise SnapshotError("invalid reviewer summary/tests_required")
    if not isinstance(data["findings"], list):
        raise SnapshotError("invalid reviewer findings")
    for finding in data["findings"]:
        if (
            not isinstance(finding, dict)
            or set(finding) != _FINDING_KEYS
            or finding.get("severity") not in _FINDING_SEVERITIES
            or not isinstance(finding.get("title"), str)
            or not isinstance(finding.get("details"), str)
            or not isinstance(finding.get("files"), list)
            or not all(isinstance(item, str) for item in finding["files"])
        ):
            raise SnapshotError("malformed nested reviewer finding")
    return data


def _validate_executor_envelope(data: Any) -> dict[str, Any]:
    """Fail closed unless the payload is the production Cursor Agent envelope."""
    if not isinstance(data, dict):
        raise SnapshotError("executor report must be a JSON object")
    unknown = set(data) - _CURSOR_AGENT_RESULT_KEYS
    if unknown:
        raise SnapshotError("executor report has unknown fields")
    if not _CURSOR_AGENT_RESULT_KEYS.issubset(data):
        raise SnapshotError("executor report missing required fields")
    if (
        data.get("type") != "result"
        or not isinstance(data.get("subtype"), str)
        or not data["subtype"]
        or type(data.get("is_error")) is not bool
        or type(data.get("duration_ms")) is not int
        or data["duration_ms"] < 0
        or type(data.get("duration_api_ms")) is not int
        or data["duration_api_ms"] < 0
        or not isinstance(data.get("result"), str)
        or not isinstance(data.get("session_id"), str)
        or not data["session_id"]
        or not isinstance(data.get("request_id"), str)
        or not data["request_id"]
        or not isinstance(data.get("usage"), dict)
    ):
        raise SnapshotError("executor report field types are invalid")
    return data


def _executor_summary(path: Path) -> tuple[str, list[str]]:
    from .persist import PersistError, secure_read_json

    try:
        data = secure_read_json(path, require_private=True)
    except PersistError as exc:
        message = str(exc).lower()
        if "missing" in message:
            return "não informado", []
        raise SnapshotError(f"executor report cannot be read safely: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise SnapshotError(f"executor report cannot be read safely: {exc}") from exc
    validated = _validate_executor_envelope(data)
    raw = sanitize_text(validated["result"].strip())[:1600]
    return raw or "não informado", []


_TEST_COUNT = re.compile(r"(?i)\b(\d+)\s+(passed|failed|skipped|errors?)\b")


def _test_summary(run_dir: Path, executor_report: Path) -> tuple[dict[str, int], list[str]]:
    from .persist import PersistError, secure_read_text

    counts = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0}
    commands: list[str] = []
    sources = [executor_report, *sorted(run_dir.glob("validation-*.log"))]
    for source in sources:
        try:
            source.lstat()
        except FileNotFoundError:
            # Absent inputs are skipped; present unsafe/corrupt inputs fail closed.
            continue
        try:
            text = secure_read_text(
                source,
                max_bytes=2 * 1024 * 1024,
                require_private=True,
                containment_root=run_dir if source.parent == run_dir else None,
            )
        except (OSError, PersistError, UnicodeError) as exc:
            raise SnapshotError(
                f"validation input cannot be read safely ({source.name}): {exc}"
            ) from exc
        for number, label in _TEST_COUNT.findall(text):
            normalized = "errors" if label.lower().startswith("error") else label.lower()
            counts[normalized] += int(number)
    metadata = read_json(run_dir / "run.json")
    profile = metadata.get("profile") or {}
    validation = profile.get("validation") if isinstance(profile, dict) else {}
    configured = validation.get("commands") if isinstance(validation, dict) else []
    if isinstance(configured, list):
        commands = [" ".join(map(str, item))[:500] for item in configured if isinstance(item, list)]
    return counts, commands


def prepare_review_artifacts(
    *,
    run_dir: Path,
    repo: Path,
    worktree: Path,
    task_file: str,
    task_id: str,
    task_slug: str,
    base_commit: str,
    iteration: int,
    max_iterations: int,
    executor_report: Path,
    reviewer_report: Path,
    reviewed_hash: str,
    profile: ProjectProfile,
) -> tuple[dict[str, Any], list[str]]:
    manifest = build_snapshot_manifest(worktree, base_commit)
    if manifest["snapshot_hash"] != reviewed_hash:
        raise SnapshotError("manifest hash does not match the reviewed snapshot")
    documentation = validate_documentation(
        profile,
        manifest,
        task_id=task_id,
        task_slug=task_slug,
    )
    try:
        from .persist import PersistError, secure_read_json

        reviewer_raw = secure_read_json(reviewer_report, require_private=True)
    except (OSError, PersistError, ValueError) as exc:
        raise SnapshotError(f"invalid reviewer report: {exc}") from exc
    # Validate the full reviewer + executor envelopes before any derived publish.
    reviewer = validate_reviewer_report(reviewer_raw)
    executor_summary, risks = _executor_summary(executor_report)
    counts, commands = _test_summary(run_dir, executor_report)
    numstat = _git_bytes(worktree, "diff", "--numstat", "--no-renames", base_commit, "--")
    additions = deletions = 0
    for line in numstat.decode("utf-8", errors="replace").splitlines():
        parts = line.split("\t", 2)
        if len(parts) >= 2:
            if parts[0].isdigit():
                additions += int(parts[0])
            if parts[1].isdigit():
                deletions += int(parts[1])
    for raw in _git_bytes(
        worktree, "ls-files", "--others", "--exclude-standard", "-z"
    ).split(b"\0"):
        if not raw:
            continue
        relative = _safe_relative(raw)
        candidate = worktree / relative
        try:
            info = candidate.lstat()
            if not stat.S_ISREG(info.st_mode):
                continue
            fd = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                opened = os.fstat(fd)
                if not stat.S_ISREG(opened.st_mode) or (
                    opened.st_dev,
                    opened.st_ino,
                ) != (info.st_dev, info.st_ino):
                    raise SnapshotError(f"untracked file changed while counting: {relative}")
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(fd, 1024 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                data = b"".join(chunks)
            finally:
                os.close(fd)
        except OSError as exc:
            raise SnapshotError(f"cannot count untracked diff lines: {relative}") from exc
        if b"\0" not in data:
            additions += data.count(b"\n") + int(bool(data) and not data.endswith(b"\n"))
    safe_findings: list[dict[str, str]] = []
    for finding in reviewer["findings"][:30]:
        safe_findings.append(
            {
                "severity": sanitize_text(finding["severity"])[:40],
                "title": sanitize_text(finding["title"])[:300],
                "details": sanitize_text(finding["details"])[:800],
            }
        )
    validation_results = []
    for path in sorted(run_dir.glob("validation-*-result.json")):
        from .persist import PersistError, secure_lstat_regular, secure_read_json

        try:
            secure_lstat_regular(path, require_private=True)
        except PersistError as exc:
            raise SnapshotError(
                f"validation result cannot be read safely ({path.name}): {exc}"
            ) from exc
        try:
            raw = secure_read_json(
                path, require_private=True, containment_root=run_dir
            )
        except (OSError, PersistError, ValueError) as exc:
            raise SnapshotError(
                f"validation result cannot be read safely ({path.name}): {exc}"
            ) from exc
        validation_results.append(_validate_validation_result(path, raw))
    # Resolve task title before publishing so symlink/unsafe task files fail
    # closed without writing reviewed_manifest.json / technical_summary.json.
    task_title = _task_title(worktree, task_file, task_id)
    summary = {
        "schema_version": 1,
        "task_id": task_id,
        "task_title": task_title,
        "repository": repo.name,
        "base_commit": base_commit,
        "iteration": iteration,
        "max_iterations": max_iterations,
        "reviewed_diff_hash": reviewed_hash,
        "files": [entry["path"] for entry in manifest["entries"]],
        "file_count": len(manifest["entries"]),
        "additions": additions,
        "deletions": deletions,
        "executor_summary": executor_summary,
        "test_counts": counts,
        "test_commands": commands,
        "validation_status": "passed"
        if all(item.get("state") == "completed" for item in validation_results)
        else ("not_configured" if not validation_results else "failed"),
        "reviewer_status": reviewer.get("status"),
        "reviewer_summary": sanitize_text(str(reviewer.get("summary", "")))[:2000],
        "findings": safe_findings,
        "residual_risks": risks,
        "documentation": documentation,
        "prepared_at": utc_now_iso(),
    }
    message = format_technical_summary(summary)
    chunks = split_telegram_message(message)
    summary["telegram_messages"] = chunks
    atomic_write_json(run_dir / MANIFEST_FILENAME, manifest)
    atomic_write_json(run_dir / SUMMARY_FILENAME, summary)
    return summary, chunks


def format_technical_summary(summary: dict[str, Any]) -> str:
    counts = summary.get("test_counts") or {}
    lines = [
        f"{summary.get('task_id')} — {summary.get('task_title')}",
        "",
        f"Repositório: {summary.get('repository')}",
        f"Base: {str(summary.get('base_commit'))[:12]}",
        f"Resultado técnico: {summary.get('reviewer_status')}",
        f"Iteração: {summary.get('iteration')}/{summary.get('max_iterations')}",
        f"Arquivos: {summary.get('file_count')}",
        f"Diff: +{summary.get('additions')} / -{summary.get('deletions')}",
        (
            "Testes: "
            f"{counts.get('passed', 0)} passed, {counts.get('skipped', 0)} skipped, "
            f"{counts.get('failed', 0)} failed, {counts.get('errors', 0)} errors"
        ),
        f"Validação configurada: {summary.get('validation_status')}",
        f"Hash revisado: {str(summary.get('reviewed_diff_hash'))[:12]}…",
        "",
        "Arquivos alterados:",
        *[f"- {sanitize_text(str(path))}" for path in summary.get("files", [])],
        "",
        "Resumo do executor:",
        sanitize_text(str(summary.get("executor_summary", "não informado"))),
        "",
        "Comandos de teste/validação:",
        *([f"- {sanitize_text(str(item))}" for item in summary.get("test_commands", [])] or ["- nenhum configurado"]),
        "",
        "Resumo do reviewer:",
        sanitize_text(str(summary.get("reviewer_summary", ""))),
        "",
        "Findings:",
    ]
    findings = summary.get("findings") or []
    if findings:
        for finding in findings:
            lines.append(
                f"- [{finding.get('severity')}] {finding.get('title')}: {finding.get('details')}"
            )
    else:
        lines.append("- nenhum")
    lines.extend(["", "Riscos residuais:"])
    risks = summary.get("residual_risks") or []
    lines.extend([f"- {sanitize_text(str(item))}" for item in risks] or ["- nenhum informado"])
    lines.extend(["", "Documentação:"])
    docs = summary.get("documentation") or []
    lines.extend([f"- {sanitize_text(str(item))}" for item in docs] or ["- nenhuma exigida/alterada"])
    return "\n".join(lines)


def split_telegram_message(text: str, limit: int = TELEGRAM_CHUNK_LIMIT) -> list[str]:
    """Split plain text safely; Telegram parse_mode is deliberately unused."""
    safe = sanitize_text(text)
    body_limit = max(128, limit - 24)
    chunks: list[str] = []
    current = ""
    for line in safe.splitlines(keepends=True):
        if len(line) > body_limit:
            line = line[: body_limit - 22] + " …[truncated field]\n"
        if current and len(current) + len(line) > body_limit:
            chunks.append(current.rstrip())
            current = ""
        current += line
    if current or not chunks:
        chunks.append(current.rstrip())
    total = len(chunks)
    return [f"({index}/{total})\n{chunk}" for index, chunk in enumerate(chunks, 1)]
