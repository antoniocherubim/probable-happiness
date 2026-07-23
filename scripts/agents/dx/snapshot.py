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


class SnapshotError(ValueError):
    """The working snapshot is unsafe, incomplete, or no longer reviewed."""


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
    try:
        for line in (worktree / task_file).read_text(encoding="utf-8").splitlines():
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
    except (OSError, UnicodeError):
        pass
    return task_id


def _read_sanitized(path: Path, limit: int = 1600) -> str:
    if not path.is_file():
        return "não informado"
    value = sanitize_text(path.read_text(encoding="utf-8", errors="replace")).strip()
    if len(value) > limit:
        return value[: limit - 24].rstrip() + " …[truncated field]"
    return value or "não informado"


def _executor_summary(path: Path) -> tuple[str, list[str]]:
    raw = _read_sanitized(path)
    risks: list[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return raw, risks
    if not isinstance(data, dict):
        return raw, risks
    for key in ("summary", "result", "message"):
        if isinstance(data.get(key), str) and data[key].strip():
            raw = sanitize_text(data[key].strip())[:1600]
            break
    value = data.get("risks") or data.get("residual_risks")
    if isinstance(value, list):
        risks = [sanitize_text(str(item))[:500] for item in value[:20]]
    return raw, risks


_TEST_COUNT = re.compile(r"(?i)\b(\d+)\s+(passed|failed|skipped|errors?)\b")


def _test_summary(run_dir: Path, executor_report: Path) -> tuple[dict[str, int], list[str]]:
    counts = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0}
    commands: list[str] = []
    sources = [executor_report, *sorted(run_dir.glob("validation-*.log"))]
    for source in sources:
        if not source.is_file():
            continue
        text = source.read_text(encoding="utf-8", errors="replace")
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
        reviewer = json.loads(reviewer_report.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"invalid reviewer report: {exc}") from exc
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
    findings = reviewer.get("findings") if isinstance(reviewer, dict) else []
    safe_findings: list[dict[str, str]] = []
    if isinstance(findings, list):
        for finding in findings[:30]:
            if isinstance(finding, dict):
                safe_findings.append(
                    {
                        "severity": sanitize_text(str(finding.get("severity", "")))[:40],
                        "title": sanitize_text(str(finding.get("title", "")))[:300],
                        "details": sanitize_text(str(finding.get("details", "")))[:800],
                    }
                )
    validation_results = [
        read_json(path)
        for path in sorted(run_dir.glob("validation-*-result.json"))
        if path.is_file()
    ]
    summary = {
        "schema_version": 1,
        "task_id": task_id,
        "task_title": _task_title(worktree, task_file, task_id),
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
