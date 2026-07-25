"""Run metadata, safe resume planning, and untrusted evidence attachment."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
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
from .persist import PersistError, ensure_private_dir, secure_read_json, secure_write_json
from .profile import ProfileError, load_project_profile
from .schemas import (
    FUTURE_SCHEMA_REFUSAL,
    ITERATION_BUDGET_SCHEMA_VERSION,
    PERSISTENCE_SCHEMA_VERSION,
    RUN_METADATA_SCHEMA_VERSION,
    RUNNER_VERSION,
    SUPPORTED_ITERATION_BUDGET_SCHEMAS,
    SUPPORTED_PRIOR_PERSISTENCE_SCHEMAS,
    SUPPORTED_RUN_METADATA_SCHEMAS,
)
from .state_machine import RunEvent, transition_run


RUN_METADATA = "run.json"
EVIDENCE_MANIFEST = "evidence.json"
MAX_EVIDENCE_BYTES = 1024 * 1024
ITERATION_BUDGET = "iteration-budget.json"
MAX_ADDITIONAL_ITERATIONS = 20
MAX_EFFECTIVE_ITERATIONS = 50
MAX_REVIEW_REASON = "max_review_iterations"
LEGACY_MAX_REVIEW_REASON = "max_iterations"
GENESIS_ENTRY_HASH = "0" * 64


class RunStateError(ValueError):
    """A run cannot be safely resumed or modified."""


class IterationBudgetError(RunStateError):
    """An iteration-budget extension is invalid or unsafe."""


def write_run_metadata(run_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    required = {"repo", "task_file", "base_commit", "worktree", "max_iterations"}
    missing = required - set(payload)
    if missing:
        raise RunStateError(f"run metadata missing: {', '.join(sorted(missing))}")
    run_path = Path(run_dir)
    if run_path.exists():
        info = run_path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RunStateError("run directory must be a regular directory")
    else:
        ensure_private_dir(run_path)
    document = {
        "schema_version": RUN_METADATA_SCHEMA_VERSION,
        "persistence_schema": PERSISTENCE_SCHEMA_VERSION,
        "runner_version": RUNNER_VERSION,
        "run_id": run_path.name,
        **payload,
    }
    atomic_write_json(run_path / RUN_METADATA, document)
    return document


def _version_tuple(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in str(value).split("."):
        if piece.isdigit():
            parts.append(int(piece))
        else:
            break
    return tuple(parts)


def load_run_metadata(run_dir: Path) -> dict[str, Any]:
    path = Path(run_dir) / RUN_METADATA
    try:
        data = secure_read_json(path, require_private=True, containment_root=run_dir)
    except (OSError, UnicodeError, PersistError, ValueError, json.JSONDecodeError) as exc:
        raise RunStateError(f"invalid run metadata: {exc}") from exc
    schema = data.get("schema_version")
    if type(schema) is int and schema > RUN_METADATA_SCHEMA_VERSION:
        raise RunStateError(FUTURE_SCHEMA_REFUSAL)
    if schema not in SUPPORTED_RUN_METADATA_SCHEMAS:
        raise RunStateError("run metadata schema unsupported; run migrate")
    # Schema 1 legacy may omit persistence_schema; schema 2+ must declare it.
    if schema >= 2:
        persistence = data.get("persistence_schema")
        if type(persistence) is not int:
            raise RunStateError("run persistence_schema missing or malformed")
        if persistence > PERSISTENCE_SCHEMA_VERSION:
            raise RunStateError(FUTURE_SCHEMA_REFUSAL)
        if persistence not in SUPPORTED_PRIOR_PERSISTENCE_SCHEMAS:
            raise RunStateError("run persistence schema unsupported; run migrate")
        runner = data.get("runner_version")
        if not isinstance(runner, str) or not runner:
            raise RunStateError("run metadata runner_version missing or malformed")
        if _version_tuple(runner) > _version_tuple(RUNNER_VERSION):
            raise RunStateError(FUTURE_SCHEMA_REFUSAL)
    else:
        persistence = data.get("persistence_schema", 1)
        if type(persistence) is int and persistence > PERSISTENCE_SCHEMA_VERSION:
            raise RunStateError(FUTURE_SCHEMA_REFUSAL)
    if data.get("run_id") != Path(run_dir).name:
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
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise RunStateError(f"invalid review snapshot: {exc}") from exc
    value = data.get("diff_hash")
    return str(value) if value else None


def _regular_json(path: Path, label: str) -> dict[str, Any]:
    try:
        return secure_read_json(
            path, require_private=True, containment_root=path.parent
        )
    except PersistError as exc:
        raise IterationBudgetError(f"{label} is invalid: {exc}") from exc


def _iteration_cursor(run_dir: Path) -> int:
    path = run_dir / "iteration"
    try:
        from .persist import secure_read_text

        value = int(
            secure_read_text(
                path,
                max_bytes=64,
                require_private=True,
                containment_root=run_dir,
            ).strip()
        )
    except (PersistError, OSError, UnicodeError, ValueError) as exc:
        raise IterationBudgetError("iteration cursor is missing or invalid") from exc
    if value < 1:
        raise IterationBudgetError("iteration cursor must be positive")
    return value


def _strict_review_snapshot_hash(run_dir: Path, iteration: int) -> str:
    label = f"review-{iteration}-snapshot.json"
    document = _regular_json(run_dir / label, label)
    if (
        set(document) != {"schema_version", "iteration", "diff_hash"}
        or document.get("schema_version") != 1
        or document.get("iteration") != iteration
        or not re.fullmatch(r"[0-9a-f]{64}", str(document.get("diff_hash", "")))
    ):
        raise IterationBudgetError(f"{label} contract is invalid")
    return str(document["diff_hash"])


def _extension_id(run_id: str, entry: dict[str, Any]) -> str:
    """Legacy DX-04 binding (schema v1)."""
    binding = {
        "run_id": run_id,
        "additional_iterations": entry["additional_iterations"],
        "previous_limit": entry["previous_limit"],
        "effective_limit": entry["effective_limit"],
        "origin": entry["origin"],
        "authorized_at_iteration": entry["authorized_at_iteration"],
        "review_file": entry["review_file"],
        "review_sha256": entry["review_sha256"],
        "reviewed_diff_hash": entry["reviewed_diff_hash"],
        "blocked_reason": entry["blocked_reason"],
    }
    encoded = json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _extension_id_v2(run_id: str, entry: dict[str, Any]) -> str:
    """DX-08 binding includes timestamps, updated_at, and previous entry hash."""
    binding = {
        "run_id": run_id,
        "additional_iterations": entry["additional_iterations"],
        "previous_limit": entry["previous_limit"],
        "effective_limit": entry["effective_limit"],
        "origin": entry["origin"],
        "authorized_at": entry["authorized_at"],
        "updated_at": entry["updated_at"],
        "authorized_at_iteration": entry["authorized_at_iteration"],
        "review_file": entry["review_file"],
        "review_sha256": entry["review_sha256"],
        "reviewed_diff_hash": entry["reviewed_diff_hash"],
        "blocked_reason": entry["blocked_reason"],
        "previous_entry_hash": entry["previous_entry_hash"],
    }
    encoded = json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _entry_chain_hash(entry: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "idempotency_id": entry["idempotency_id"],
                "previous_entry_hash": entry["previous_entry_hash"],
                "authorized_at": entry["authorized_at"],
                "updated_at": entry["updated_at"],
                "effective_limit": entry["effective_limit"],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def load_iteration_budget(run_dir: Path, original_limit: int) -> dict[str, Any]:
    """Load and strictly validate the append-only logical budget chain."""
    path = Path(run_dir) / ITERATION_BUDGET
    try:
        path.lstat()
    except FileNotFoundError:
        return {
            "schema_version": ITERATION_BUDGET_SCHEMA_VERSION,
            "run_id": Path(run_dir).name,
            "original_limit": original_limit,
            "effective_limit": original_limit,
            "extensions": [],
            "updated_at": None,
        }
    document = _regular_json(path, ITERATION_BUDGET)
    required = {
        "schema_version",
        "run_id",
        "original_limit",
        "effective_limit",
        "extensions",
        "updated_at",
    }
    if set(document) != required:
        raise IterationBudgetError("iteration budget has missing or unknown fields")
    schema = document.get("schema_version")
    if type(schema) is int and schema > ITERATION_BUDGET_SCHEMA_VERSION:
        raise IterationBudgetError(FUTURE_SCHEMA_REFUSAL)
    if schema not in SUPPORTED_ITERATION_BUDGET_SCHEMAS:
        raise IterationBudgetError("iteration budget schema unsupported; run migrate")
    if (
        document.get("run_id") != Path(run_dir).name
        or document.get("original_limit") != original_limit
    ):
        raise IterationBudgetError("iteration budget binding mismatch")
    extensions = document.get("extensions")
    if (
        not isinstance(extensions, list)
        or not extensions
        or not isinstance(document.get("updated_at"), str)
    ):
        raise IterationBudgetError(
            "iteration budget needs a non-empty extensions array and timestamp"
        )
    current = original_limit
    identifiers: set[str] = set()
    extension_fields_v1 = {
        "idempotency_id",
        "additional_iterations",
        "previous_limit",
        "effective_limit",
        "origin",
        "authorized_at",
        "authorized_at_iteration",
        "review_file",
        "review_sha256",
        "reviewed_diff_hash",
        "blocked_reason",
    }
    extension_fields_v2 = extension_fields_v1 | {"previous_entry_hash", "updated_at"}
    previous_hash = GENESIS_ENTRY_HASH
    for entry in extensions:
        if not isinstance(entry, dict):
            raise IterationBudgetError("iteration budget extension is malformed")
        if schema == 1:
            if set(entry) != extension_fields_v1:
                raise IterationBudgetError("iteration budget extension is malformed")
        else:
            if set(entry) != extension_fields_v2:
                raise IterationBudgetError("iteration budget extension is malformed")
        additional = entry.get("additional_iterations")
        effective = entry.get("effective_limit")
        if (
            type(additional) is not int
            or not 1 <= additional <= MAX_ADDITIONAL_ITERATIONS
            or entry.get("previous_limit") != current
            or effective != current + additional
            or type(effective) is not int
            or effective > MAX_EFFECTIVE_ITERATIONS
            or entry.get("authorized_at_iteration") != current
            or entry.get("origin") not in {"cli", "telegram"}
            or not isinstance(entry.get("authorized_at"), str)
            or not isinstance(entry.get("review_file"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("review_sha256", "")))
            or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("reviewed_diff_hash", "")))
            or entry.get("blocked_reason")
            not in {MAX_REVIEW_REASON, LEGACY_MAX_REVIEW_REASON}
        ):
            raise IterationBudgetError("iteration budget extension chain is invalid")
        if schema >= 2:
            if not isinstance(entry.get("updated_at"), str) or not entry["updated_at"]:
                raise IterationBudgetError("iteration budget extension updated_at missing")
            if entry.get("previous_entry_hash") != previous_hash:
                raise IterationBudgetError("iteration budget previous hash chain broken")
            identifier = str(entry.get("idempotency_id", ""))
            if identifier != _extension_id_v2(Path(run_dir).name, entry) or identifier in identifiers:
                raise IterationBudgetError("iteration budget idempotency binding is invalid")
            previous_hash = _entry_chain_hash(entry)
        else:
            identifier = str(entry.get("idempotency_id", ""))
            if identifier != _extension_id(Path(run_dir).name, entry) or identifier in identifiers:
                raise IterationBudgetError("iteration budget idempotency binding is invalid")
        expected_review_file = f"review-{entry['authorized_at_iteration']}.json"
        if entry.get("review_file") != expected_review_file:
            raise IterationBudgetError("iteration budget review binding is invalid")
        if _review_sha256(Path(run_dir) / expected_review_file) != entry["review_sha256"]:
            raise IterationBudgetError("authorized reviewer feedback was modified")
        if _strict_review_snapshot_hash(
            Path(run_dir), int(entry["authorized_at_iteration"])
        ) != entry["reviewed_diff_hash"]:
            raise IterationBudgetError("authorized review snapshot binding was modified")
        identifiers.add(identifier)
        current = effective
    if document.get("effective_limit") != current:
        raise IterationBudgetError("iteration budget effective limit mismatch")
    if schema >= 2:
        last = extensions[-1]
        if document.get("updated_at") != last.get("updated_at"):
            raise IterationBudgetError(
                "iteration budget updated_at is not integrity-bound to the tip entry"
            )
    return document


def effective_iteration_limit(run_dir: Path, original_limit: int) -> int:
    return int(load_iteration_budget(run_dir, original_limit)["effective_limit"])


def _failure_reason(run_dir: Path) -> tuple[str, dict[str, Any]]:
    failure = _regular_json(run_dir / "failure.json", "failure.json")
    if set(failure) != {
        "schema_version",
        "reason",
        "phase",
        "iteration",
        "report",
        "recorded_at",
    } or failure.get("schema_version") != 1:
        raise IterationBudgetError("failure.json contract is invalid")
    if (
        type(failure.get("iteration")) is not int
        or not isinstance(failure.get("recorded_at"), str)
        or failure.get("report") is not None
        and not isinstance(failure.get("report"), str)
    ):
        raise IterationBudgetError("failure.json field types are invalid")
    reason = failure.get("reason")
    if not isinstance(reason, str) or not reason:
        raise IterationBudgetError("failure.json has no structured reason")
    return reason, failure


def _probe_delivery_lock(run_dir: Path) -> None:
    path = run_dir / ".delivery.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise IterationBudgetError("delivery lock is not a regular file")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        raise IterationBudgetError("a delivery operation is currently active") from exc
    finally:
        if "fd" in locals():
            os.close(fd)


def _review_sha256(path: Path) -> str:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise IterationBudgetError("last reviewer report must be a regular file")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or (
                opened.st_dev,
                opened.st_ino,
            ) != (info.st_dev, info.st_ino):
                raise IterationBudgetError("last reviewer report changed while opening")
            digest = hashlib.sha256()
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            return digest.hexdigest()
        finally:
            os.close(fd)
    except OSError as exc:
        raise IterationBudgetError("last reviewer report cannot be read") from exc


def _authorize_iteration_extension_locked(
    run_dir: Path,
    additional_iterations: int,
    *,
    origin: str,
) -> dict[str, Any]:
    metadata = validate_run(run_dir)
    original_limit = int(metadata["max_iterations"])
    budget = load_iteration_budget(run_dir, original_limit)
    effective_limit = int(budget["effective_limit"])
    iteration = _iteration_cursor(run_dir)
    status = str(metadata["status"])
    terminal = {
        "APPROVED",
        "AWAITING_HUMAN_APPROVAL",
        "HUMAN_APPROVED",
        "DELIVERING",
        "PUSHED",
        "DELIVERY_FAILED",
    }
    if status in terminal:
        raise IterationBudgetError(
            f"run status {status} is not eligible; only BLOCKED by "
            f"{MAX_REVIEW_REASON} may receive a new budget"
        )

    extensions = budget["extensions"]
    latest = extensions[-1] if extensions else None
    if (
        isinstance(latest, dict)
        and latest.get("additional_iterations") == additional_iterations
        and int(latest["authorized_at_iteration"]) <= iteration
        and iteration < int(latest["effective_limit"])
        and status in {"BLOCKED", "CHANGES_REQUESTED", "EXECUTING", "REVIEWING"}
    ):
        # Replay of the currently active authorization after any interruption.
        if status == "BLOCKED" and iteration == int(latest["authorized_at_iteration"]):
            reason, _failure = _failure_reason(run_dir)
            if reason in {MAX_REVIEW_REASON, LEGACY_MAX_REVIEW_REASON}:
                try:
                    from .txn import LogicalTransaction, _commit_locked

                    txn = LogicalTransaction(
                        run_dir=run_dir,
                        event=RunEvent.ITERATION_BUDGET_EXTENDED.value,
                        status_event=RunEvent.ITERATION_BUDGET_EXTENDED,
                        origin="resume",
                        _lock_name=".resume.lock",
                    )
                    txn.add_json(ITERATION_BUDGET, budget)
                    _commit_locked(txn)
                except (OSError, ValueError, Exception):
                    # plan_resume also recognizes this pending-ledger state.
                    pass
        return {
            "result": "idempotent_replay",
            "idempotency_id": latest["idempotency_id"],
            "original_limit": original_limit,
            "previous_limit": latest["previous_limit"],
            "effective_limit": latest["effective_limit"],
            "iteration": iteration,
        }

    if status != "BLOCKED":
        raise IterationBudgetError(
            f"run status is {status}, not BLOCKED; no iteration budget was changed"
        )
    reason, failure = _failure_reason(run_dir)
    if reason not in {MAX_REVIEW_REASON, LEGACY_MAX_REVIEW_REASON}:
        raise IterationBudgetError(
            f"run is BLOCKED by {reason!r}, not {MAX_REVIEW_REASON!r}; "
            "fix that blocker or use the ordinary resume/review-only flow"
        )
    if failure.get("phase") != "loop":
        raise IterationBudgetError("max-iteration blocker has an invalid phase")
    if iteration != effective_limit:
        raise IterationBudgetError(
            f"iteration cursor {iteration} does not match effective limit {effective_limit}"
        )
    failure_iteration = failure.get("iteration")
    if failure_iteration not in {iteration, iteration + 1}:
        raise IterationBudgetError("failure artifact does not match the exhausted iteration")
    review_path = run_dir / f"review-{iteration}.json"
    review = _regular_json(review_path, f"review-{iteration}.json")
    if review.get("status") != "CHANGES_REQUESTED":
        raise IterationBudgetError(
            f"last reviewer status is {review.get('status')!r}, not CHANGES_REQUESTED"
        )
    expected_review_fields = {"status", "summary", "findings", "tests_required"}
    if set(review) != expected_review_fields:
        raise IterationBudgetError("last reviewer report contract is invalid")
    if (
        not isinstance(review.get("summary"), str)
        or not isinstance(review.get("findings"), list)
        or not isinstance(review.get("tests_required"), list)
        or not all(isinstance(item, str) for item in review["tests_required"])
    ):
        raise IterationBudgetError("last reviewer report field types are invalid")
    cursor_report = run_dir / f"cursor-{iteration}.json"
    reviewer_result = run_dir / f"reviewer-{iteration}-result.json"
    try:
        cursor_info = cursor_report.lstat()
    except OSError as exc:
        raise IterationBudgetError("last executor report is missing or empty") from exc
    if (
        stat.S_ISLNK(cursor_info.st_mode)
        or not stat.S_ISREG(cursor_info.st_mode)
        or cursor_info.st_size == 0
    ):
        raise IterationBudgetError("last executor report is missing or empty")
    result = _regular_json(reviewer_result, f"reviewer-{iteration}-result.json")
    if result.get("state") != "completed" or result.get("exit_code") != 0:
        raise IterationBudgetError("last reviewer execution did not complete successfully")
    snapshot_hash = _strict_review_snapshot_hash(run_dir, iteration)
    current_hash = compute_diff_hash(Path(metadata["worktree"]), str(metadata["base_commit"]))
    if not snapshot_hash or current_hash != snapshot_hash:
        raise IterationBudgetError("worktree drifted from the last reviewed snapshot")
    approval_or_delivery_present = False
    for name in (
        "human_approval_request.json",
        "human_approval_decision.json",
        "human_rejection.json",
        "delivery.json",
    ):
        try:
            (run_dir / name).lstat()
        except FileNotFoundError:
            continue
        approval_or_delivery_present = True
        break
    if approval_or_delivery_present:
        raise IterationBudgetError("approval or delivery artifacts make this run ineligible")
    _probe_delivery_lock(run_dir)
    new_limit = effective_limit + additional_iterations
    if new_limit > MAX_EFFECTIVE_ITERATIONS:
        raise IterationBudgetError(
            f"effective limit {new_limit} exceeds defensive cap {MAX_EFFECTIVE_ITERATIONS}"
        )
    review_digest = _review_sha256(review_path)
    authorized_at = utc_now_iso()
    updated_at = authorized_at
    # Upgrade any legacy v1 entries in-place (limits unchanged) so the on-disk
    # ledger is always schema v2 after a new authorization.
    migrated: list[dict[str, Any]] = []
    previous_hash = GENESIS_ENTRY_HASH
    for old in extensions:
        if (
            "previous_entry_hash" in old
            and "updated_at" in old
            and budget.get("schema_version") == 2
        ):
            migrated.append(old)
            previous_hash = _entry_chain_hash(old)
            continue
        upgraded = dict(old)
        upgraded["previous_entry_hash"] = previous_hash
        upgraded["updated_at"] = upgraded.get("updated_at") or upgraded.get(
            "authorized_at"
        )
        upgraded["idempotency_id"] = _extension_id_v2(run_dir.name, upgraded)
        previous_hash = _entry_chain_hash(upgraded)
        migrated.append(upgraded)
    entry = {
        "additional_iterations": additional_iterations,
        "previous_limit": effective_limit,
        "effective_limit": new_limit,
        "origin": origin,
        "authorized_at": authorized_at,
        "updated_at": updated_at,
        "authorized_at_iteration": iteration,
        "review_file": review_path.name,
        "review_sha256": review_digest,
        "reviewed_diff_hash": snapshot_hash,
        "blocked_reason": reason,
        "previous_entry_hash": previous_hash,
    }
    entry["idempotency_id"] = _extension_id_v2(run_dir.name, entry)
    document = {
        "schema_version": ITERATION_BUDGET_SCHEMA_VERSION,
        "run_id": run_dir.name,
        "original_limit": original_limit,
        "effective_limit": new_limit,
        "extensions": [*migrated, entry],
        "updated_at": updated_at,
    }
    from .txn import LogicalTransaction, TransactionError, _commit_locked

    txn = LogicalTransaction(
        run_dir=run_dir,
        event=RunEvent.ITERATION_BUDGET_EXTENDED.value,
        status_event=RunEvent.ITERATION_BUDGET_EXTENDED,
        origin="resume" if origin == "cli" else "bridge",
        _lock_name=".resume.lock",
    )
    txn.add_json(ITERATION_BUDGET, document)
    try:
        # Caller already holds ``.resume.lock``; commit acquires ``.state.lock``.
        _commit_locked(txn, state_lock_held=False)
        status_transition = "completed"
    except (OSError, ValueError, TransactionError):
        # If interrupted mid-journal, recovery / plan_resume still see ledger.
        status_transition = "pending_recovery"
        try:
            (run_dir / ITERATION_BUDGET).lstat()
        except FileNotFoundError:
            raise
        except OSError:
            raise
    return {
        "result": "authorized",
        "idempotency_id": entry["idempotency_id"],
        "original_limit": original_limit,
        "previous_limit": effective_limit,
        "effective_limit": new_limit,
        "iteration": iteration,
        "status_transition": status_transition,
    }


def authorize_iteration_extension(
    run_dir: Path,
    additional_iterations: int,
    *,
    origin: str = "cli",
    resume_lock_held: bool = False,
) -> dict[str, Any]:
    if (
        type(additional_iterations) is not int
        or not 1 <= additional_iterations <= MAX_ADDITIONAL_ITERATIONS
    ):
        raise IterationBudgetError(
            f"additional iterations must be an integer between 1 and "
            f"{MAX_ADDITIONAL_ITERATIONS}"
        )
    if origin not in {"cli", "telegram"}:
        raise IterationBudgetError("iteration-budget origin is invalid")
    run_dir = Path(run_dir).expanduser().resolve()
    if resume_lock_held:
        return _authorize_iteration_extension_locked(
            run_dir, additional_iterations, origin=origin
        )
    with run_scoped_lock(run_dir, lock_name=".resume.lock", blocking=False):
        return _authorize_iteration_extension_locked(
            run_dir, additional_iterations, origin=origin
        )


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
    original_limit = int(metadata["max_iterations"])
    budget = load_iteration_budget(run_dir, original_limit)
    effective_limit = int(budget["effective_limit"])
    if iteration < 1 or iteration > effective_limit:
        raise RunStateError("invalid iteration in run")

    if status in {
        "HUMAN_APPROVED",
        "DELIVERING",
        "DELIVERY_FAILED",
        "PUSHED",
    }:
        # DELIVERING/DELIVERY_FAILED/PUSHED are retained only so runs created by
        # older releases remain inspectable. Current releases never perform or
        # resume network delivery.
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
        latest_extension = budget["extensions"][-1] if budget["extensions"] else None
        pending_extension = (
            status == "BLOCKED"
            and isinstance(latest_extension, dict)
            and iteration == latest_extension.get("authorized_at_iteration")
            and effective_limit == latest_extension.get("effective_limit")
            and iteration < effective_limit
        )
        if pending_extension and not review_only:
            reason, _failure = _failure_reason(run_dir)
            if reason not in {MAX_REVIEW_REASON, LEGACY_MAX_REVIEW_REASON}:
                raise RunStateError(
                    "pending iteration authorization no longer matches the blocker"
                )
            phase = "executor"
            iteration += 1
        elif review_only:
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
    return {
        **metadata,
        "original_max_iterations": original_limit,
        "effective_max_iterations": effective_limit,
        "resume_phase": phase,
        "iteration": iteration,
        "review_only": review_only,
    }


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
            from .persist import secure_write_bytes

            secure_write_bytes(
                destination,
                b"".join(chunks),
                mode=0o600,
                fsync_dir=True,
                containment_root=run_dir,
            )
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
