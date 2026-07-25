"""Versioned, idempotent migrations for run persistence (DX-08).

Migrations always create a backup/manifest before writing. They never promote
run status, never invent approvals/decisions/remote OIDs, and refuse unknown
future schemas without mutation.

Concurrency: migration holds both ``.migration.lock`` and ``.state.lock`` so it
cannot race active status/artifact transitions that use the state lock.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tarfile
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .atomic import run_scoped_lock
from .persist import (
    PersistError,
    assert_contained,
    canonical_json_hash,
    ensure_private_dir,
    fsync_directory,
    secure_read_bytes,
    secure_read_json,
    secure_write_bytes,
    secure_write_json,
)
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
from .state_machine import STATE_LOCK_FILENAME, read_status

MIGRATION_LOCK = ".migration.lock"
BACKUP_DIRNAME = ".migration-backup"
MANIFEST_NAME = "migration-manifest.json"


class MigrationError(ValueError):
    """Migration refused or failed closed."""


@dataclass(frozen=True)
class Migration:
    migration_id: str
    description: str
    apply: Callable[[Path, dict[str, Any]], dict[str, Any]]


def _load_run_json(run_dir: Path, *, require_private: bool = False) -> dict[str, Any] | None:
    """Legacy runs may still have broader modes; migration inspects then rewrites 0600."""
    path = run_dir / "run.json"
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    return secure_read_json(path, require_private=require_private)


def _detect_persistence_schema(run_dir: Path) -> int:
    meta = _load_run_json(run_dir)
    if meta is None:
        # Pre-DX-03 style runs may lack run.json; treat as schema 1 legacy.
        return 1
    version = meta.get("persistence_schema", meta.get("schema_version", 1))
    if type(version) is not int:
        raise MigrationError("run persistence schema is not an integer")
    if version > PERSISTENCE_SCHEMA_VERSION:
        raise MigrationError(FUTURE_SCHEMA_REFUSAL)
    if version not in SUPPORTED_PRIOR_PERSISTENCE_SCHEMAS and version != PERSISTENCE_SCHEMA_VERSION:
        raise MigrationError(f"unsupported persistence schema: {version}")
    return version


def migrate_run_metadata_v1_to_v2(run_dir: Path, report: dict[str, Any]) -> dict[str, Any]:
    path = run_dir / "run.json"
    try:
        path.lstat()
    except FileNotFoundError:
        report["skipped"].append("run.json missing")
        return report
    data = secure_read_json(path, require_private=False)
    schema = data.get("schema_version", 1)
    if schema not in SUPPORTED_RUN_METADATA_SCHEMAS:
        if type(schema) is int and schema > RUN_METADATA_SCHEMA_VERSION:
            raise MigrationError(FUTURE_SCHEMA_REFUSAL)
        raise MigrationError(f"unsupported run.json schema: {schema}")
    if (
        schema == RUN_METADATA_SCHEMA_VERSION
        and data.get("persistence_schema") == PERSISTENCE_SCHEMA_VERSION
        and data.get("runner_version")
    ):
        report["unchanged"].append("run.json")
        return report
    updated = dict(data)
    updated["schema_version"] = RUN_METADATA_SCHEMA_VERSION
    updated["persistence_schema"] = PERSISTENCE_SCHEMA_VERSION
    updated["runner_version"] = updated.get("runner_version") or RUNNER_VERSION
    if "delivery" not in updated:
        updated["delivery"] = {"mode": "none"}
    secure_write_json(path, updated, containment_root=run_dir)
    report["applied"].append("run.json->v2")
    return report


def migrate_frozen_profile_envelope(run_dir: Path, report: dict[str, Any]) -> dict[str, Any]:
    """Stamp profile schema on the frozen profile embedded in run.json."""
    path = run_dir / "run.json"
    try:
        path.lstat()
    except FileNotFoundError:
        report["skipped"].append("profile: run.json missing")
        return report
    data = secure_read_json(path, require_private=False)
    profile = data.get("profile")
    if not isinstance(profile, dict):
        report["skipped"].append("profile missing")
        return report
    if profile.get("profile_schema") == 1 and data.get("persistence_schema") == PERSISTENCE_SCHEMA_VERSION:
        report["unchanged"].append("profile")
        return report
    updated_profile = dict(profile)
    updated_profile["profile_schema"] = 1
    updated = dict(data)
    updated["profile"] = updated_profile
    secure_write_json(path, updated, containment_root=run_dir)
    report["applied"].append("profile->schema1")
    return report


def migrate_approval_artifacts_v1(run_dir: Path, report: dict[str, Any]) -> dict[str, Any]:
    """Ensure approval request/decision carry schema_version without inventing them."""
    changed = False
    for name in (
        "human_approval_request.json",
        "human_approval_decision.json",
        "human_rejection.json",
        "telegram_notify.json",
    ):
        path = run_dir / name
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        data = secure_read_json(path, require_private=False)
        if data.get("schema_version") == 1:
            report["unchanged"].append(name)
            continue
        if "schema_version" in data and type(data["schema_version"]) is int and data["schema_version"] > 1:
            raise MigrationError(FUTURE_SCHEMA_REFUSAL)
        updated = dict(data)
        updated["schema_version"] = 1
        secure_write_json(path, updated, containment_root=run_dir)
        report["applied"].append(f"{name}->schema1")
        changed = True
    if not changed and "human_approval_request.json" not in " ".join(report.get("unchanged", [])):
        report["skipped"].append("approval artifacts missing")
    return report


def migrate_iteration_budget_v1_to_v2(run_dir: Path, report: dict[str, Any]) -> dict[str, Any]:
    """Add previous_entry_hash chain and updated_at without changing effective_limit."""
    path = run_dir / "iteration-budget.json"
    try:
        path.lstat()
    except FileNotFoundError:
        report["skipped"].append("iteration-budget.json missing")
        return report
    data = secure_read_json(path, require_private=False)
    schema = data.get("schema_version", 1)
    if type(schema) is int and schema > ITERATION_BUDGET_SCHEMA_VERSION:
        raise MigrationError(FUTURE_SCHEMA_REFUSAL)
    if schema not in SUPPORTED_ITERATION_BUDGET_SCHEMAS:
        raise MigrationError(f"unsupported iteration-budget schema: {schema}")
    if schema == ITERATION_BUDGET_SCHEMA_VERSION:
        extensions = data.get("extensions")
        if isinstance(extensions, list) and extensions:
            from .runstate import _extension_id_v2

            tip = extensions[-1]
            intact = (
                isinstance(tip, dict)
                and tip.get("updated_at") == data.get("updated_at")
                and all(
                    isinstance(entry, dict)
                    and "updated_at" in entry
                    and "previous_entry_hash" in entry
                    and entry.get("idempotency_id")
                    == _extension_id_v2(run_dir.name, entry)
                    for entry in extensions
                )
            )
            if intact:
                report["unchanged"].append("iteration-budget.json")
                return report
        # Fall through to rewrite if binding incomplete or pre-updated_at v2.
    extensions = data.get("extensions")
    if not isinstance(extensions, list):
        raise MigrationError("iteration-budget extensions malformed")
    previous_hash = "0" * 64
    migrated_extensions: list[dict[str, Any]] = []
    document_updated = data.get("updated_at")
    if not isinstance(document_updated, str) or not document_updated:
        document_updated = None
    for index, entry in enumerate(extensions):
        if not isinstance(entry, dict):
            raise MigrationError("iteration-budget entry malformed")
        new_entry = dict(entry)
        new_entry["previous_entry_hash"] = previous_hash
        entry_updated = new_entry.get("updated_at") or new_entry.get("authorized_at")
        if not isinstance(entry_updated, str) or not entry_updated:
            raise MigrationError("iteration-budget entry missing authorized_at/updated_at")
        # Historical entries keep their own stamp; tip aligns with document updated_at.
        if index == len(extensions) - 1 and document_updated:
            entry_updated = document_updated
        new_entry["updated_at"] = entry_updated
        from .runstate import _entry_chain_hash, _extension_id_v2

        new_entry["idempotency_id"] = _extension_id_v2(run_dir.name, new_entry)
        previous_hash = _entry_chain_hash(new_entry)
        migrated_extensions.append(new_entry)
    tip_updated = (
        migrated_extensions[-1]["updated_at"] if migrated_extensions else document_updated
    )
    updated = dict(data)
    updated["schema_version"] = ITERATION_BUDGET_SCHEMA_VERSION
    updated["extensions"] = migrated_extensions
    updated["updated_at"] = tip_updated
    secure_write_json(path, updated, containment_root=run_dir)
    report["applied"].append("iteration-budget.json->v2")
    return report


def migrate_ensure_audit_trail(run_dir: Path, report: dict[str, Any]) -> dict[str, Any]:
    from .audit import empty_trail, load_audit_trail

    path = run_dir / "audit-trail.json"
    try:
        path.lstat()
        load_audit_trail(run_dir, require_private=False)
        report["unchanged"].append("audit-trail.json")
        return report
    except FileNotFoundError:
        pass
    except Exception as exc:
        raise MigrationError(f"existing audit trail is corrupt: {exc}") from exc
    trail = empty_trail(run_dir.name)
    secure_write_json(path, trail, containment_root=run_dir)
    report["applied"].append("audit-trail.json created")
    return report


def inspect_legacy_delivery(run_dir: Path, report: dict[str, Any]) -> dict[str, Any]:
    """Record legacy delivery artifacts for operators; never resume push."""
    legacy = []
    for name in (
        "delivery.json",
        "delivery-job.json",
        "delivery-result.json",
    ):
        if (run_dir / name).exists():
            legacy.append(name)
    status = read_status(run_dir)
    if status in {"DELIVERING", "DELIVERY_FAILED", "PUSHED"}:
        legacy.append(f"status:{status}")
    if legacy:
        report["legacy_delivery"] = legacy
        report["notes"].append(
            "legacy delivery artifacts are inspect-only; push will not resume"
        )
    return report


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        "dx08-run-metadata-v2",
        "Stamp runner_version and persistence_schema on run.json",
        migrate_run_metadata_v1_to_v2,
    ),
    Migration(
        "dx08-frozen-profile",
        "Stamp profile_schema on frozen profile in run.json",
        migrate_frozen_profile_envelope,
    ),
    Migration(
        "dx08-approval-schema",
        "Stamp schema_version on approval/outbox artifacts when present",
        migrate_approval_artifacts_v1,
    ),
    Migration(
        "dx08-iteration-budget-v2",
        "Add previous_entry_hash chain without altering limits",
        migrate_iteration_budget_v1_to_v2,
    ),
    Migration(
        "dx08-audit-trail",
        "Ensure empty audit trail exists",
        migrate_ensure_audit_trail,
    ),
    Migration(
        "dx08-legacy-delivery-inspect",
        "Surface legacy delivery artifacts without promoting state",
        inspect_legacy_delivery,
    ),
)


def _sha256_file(path: Path) -> str:
    raw = secure_read_bytes(path, max_bytes=64 * 1024 * 1024, require_private=False)
    return hashlib.sha256(raw).hexdigest()


def _artifact_hashes(run_dir: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for child in sorted(run_dir.iterdir()):
        if child.name == BACKUP_DIRNAME or child.name.startswith("."):
            continue
        if child.is_symlink() or not child.is_file():
            continue
        try:
            hashes[child.name] = _sha256_file(child)
        except PersistError:
            continue
    return hashes


def _audit_head(run_dir: Path) -> str:
    from .audit import GENESIS_PREV_HASH, load_audit_trail

    try:
        trail = load_audit_trail(run_dir, require_private=False)
        return str(trail.get("head_hash") or GENESIS_PREV_HASH)
    except Exception:
        return GENESIS_PREV_HASH


def _backup_run(run_dir: Path) -> Path:
    """Create a permission-preserving tar via secure temp write + durable publish."""
    backup_root = ensure_private_dir(run_dir / BACKUP_DIRNAME)
    from .approval import utc_now_iso

    safe_stamp = utc_now_iso().replace(":", "").replace("-", "")
    archive_name = f"pre-migration-{safe_stamp}.tar"
    archive = backup_root / archive_name
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{archive_name}.",
        suffix=".tmp",
        dir=str(backup_root),
    )
    tmp_path = Path(tmp_name)
    try:
        os.close(fd)
        with tarfile.open(tmp_path, "w") as tar:
            for child in sorted(run_dir.iterdir()):
                if child.name == BACKUP_DIRNAME:
                    continue
                # Refuse to archive through symlinks at the top level.
                try:
                    info = child.lstat()
                except OSError:
                    continue
                if stat.S_ISLNK(info.st_mode):
                    raise MigrationError(
                        f"refusing to backup symlink artifact: {child.name}"
                    )
                tar.add(child, arcname=child.name, recursive=True)
        os.chmod(tmp_path, 0o600)
        # Durable publish: fsync file then atomic replace + dir fsync.
        with open(tmp_path, "rb") as handle:
            os.fsync(handle.fileno())
        payload = secure_read_bytes(tmp_path, max_bytes=512 * 1024 * 1024, require_private=True)
        secure_write_bytes(archive, payload, mode=0o600, containment_root=run_dir)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
    fsync_directory(backup_root)
    return archive


def _write_manifest(run_dir: Path, payload: dict[str, Any]) -> None:
    secure_write_json(
        run_dir / BACKUP_DIRNAME / MANIFEST_NAME,
        payload,
        containment_root=run_dir,
    )


def plan_migration(run_dir: Path | str) -> dict[str, Any]:
    """Read-only migration plan; never creates locks or mutates the run."""
    run_dir = Path(run_dir)
    schema = _detect_persistence_schema(run_dir)
    pending = [m.migration_id for m in MIGRATIONS]
    return {
        "run_dir": str(run_dir),
        "run_id": run_dir.name,
        "persistence_schema": schema,
        "target_persistence_schema": PERSISTENCE_SCHEMA_VERSION,
        "runner_version": RUNNER_VERSION,
        "pending_migrations": pending,
        "status": read_status(run_dir),
        "dry_run": True,
    }


def _directory_snapshot(run_dir: Path) -> dict[str, tuple[int, bytes | None]]:
    """Capture names + optional small-file bytes for dry-run/future refusal checks."""
    snapshot: dict[str, tuple[int, bytes | None]] = {}
    for child in run_dir.iterdir():
        try:
            info = child.lstat()
        except OSError:
            continue
        raw: bytes | None = None
        if stat.S_ISREG(info.st_mode) and info.st_size <= 1024 * 1024:
            try:
                raw = child.read_bytes()
            except OSError:
                raw = None
        snapshot[child.name] = (info.st_mode, raw)
    return snapshot


def migrate_run(
    run_dir: Path | str,
    *,
    dry_run: bool = False,
    rollback: bool = False,
) -> dict[str, Any]:
    run_dir = Path(run_dir).expanduser()
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise MigrationError("run directory must be a regular directory")
    run_dir = Path(os.path.normpath(str(run_dir if run_dir.is_absolute() else Path.cwd() / run_dir)))

    # Future-schema refusal and dry-run must fail/return without mutation (no lock files).
    if dry_run:
        before = _directory_snapshot(run_dir)
        plan = plan_migration(run_dir)
        after = _directory_snapshot(run_dir)
        if before != after:
            raise MigrationError("dry-run mutated the run directory")
        plan["result"] = "dry_run"
        return plan

    # Detect future schema before acquiring locks so refusal leaves zero new files.
    try:
        _detect_persistence_schema(run_dir)
    except MigrationError as exc:
        if FUTURE_SCHEMA_REFUSAL in str(exc) or "newer" in str(exc).lower():
            raise
        # Other detection errors still go through locked path below for consistency.
        pass

    # Hold migration + state locks so transitions cannot interleave writes.
    with ExitStack() as stack:
        try:
            stack.enter_context(
                run_scoped_lock(run_dir, lock_name=MIGRATION_LOCK, blocking=False)
            )
        except BlockingIOError as exc:
            raise MigrationError("another migration holds the run lock") from exc
        try:
            stack.enter_context(
                run_scoped_lock(run_dir, lock_name=STATE_LOCK_FILENAME, blocking=False)
            )
        except BlockingIOError as exc:
            raise MigrationError(
                "active state transition holds .state.lock; retry migration later"
            ) from exc

        schema = _detect_persistence_schema(run_dir)
        if rollback:
            return _rollback(run_dir)
        report: dict[str, Any] = {
            "run_dir": str(run_dir),
            "run_id": run_dir.name,
            "from_schema": schema,
            "applied": [],
            "unchanged": [],
            "skipped": [],
            "notes": [],
            "status_before": read_status(run_dir),
        }
        audit_head_before = _audit_head(run_dir)
        artifact_hashes_before = _artifact_hashes(run_dir)
        names_before = set(artifact_hashes_before)
        archive = _backup_run(run_dir)
        report["backup"] = str(archive)
        _write_manifest(
            run_dir,
            {
                "schema_version": 1,
                "runner_version": RUNNER_VERSION,
                "backup": archive.name,
                "backup_sha256": _sha256_file(archive),
                "status_before": report["status_before"],
                "audit_head_before": audit_head_before,
                "artifact_hashes_before": artifact_hashes_before,
                "artifact_names_before": sorted(names_before),
                "migrations": [m.migration_id for m in MIGRATIONS],
            },
        )
        for migration in MIGRATIONS:
            report = migration.apply(run_dir, report)
        report["status_after"] = read_status(run_dir)
        if report["status_after"] != report["status_before"]:
            raise MigrationError("migration must not promote or change status")
        report["to_schema"] = PERSISTENCE_SCHEMA_VERSION
        report["audit_head_before"] = audit_head_before
        report["result"] = "migrated"
        return report


def _rollback(run_dir: Path) -> dict[str, Any]:
    from .audit import GENESIS_PREV_HASH

    backup_root = run_dir / BACKUP_DIRNAME
    manifest_path = backup_root / MANIFEST_NAME
    try:
        manifest = secure_read_json(manifest_path, require_private=False)
    except (PersistError, FileNotFoundError, OSError) as exc:
        raise MigrationError(f"no migration backup/manifest to restore: {exc}") from exc
    expected_head = manifest.get("audit_head_before", GENESIS_PREV_HASH)
    current_head = _audit_head(run_dir)
    if current_head != expected_head:
        raise MigrationError(
            "rollback refused: audit head changed after migration "
            f"(before={expected_head[:12]}…, now={current_head[:12]}…)"
        )
    archive_name = manifest.get("backup")
    if not isinstance(archive_name, str):
        raise MigrationError("migration manifest missing backup name")
    archive = backup_root / archive_name
    assert_contained(archive, backup_root)
    try:
        archive_info = archive.lstat()
    except FileNotFoundError as exc:
        raise MigrationError("migration backup archive missing") from exc
    if stat.S_ISLNK(archive_info.st_mode) or not stat.S_ISREG(archive_info.st_mode):
        raise MigrationError("migration backup archive is not a regular file")
    expected_archive_hash = manifest.get("backup_sha256")
    if isinstance(expected_archive_hash, str) and expected_archive_hash:
        actual_archive_hash = _sha256_file(archive)
        if actual_archive_hash != expected_archive_hash:
            raise MigrationError("migration backup archive checksum mismatch")
    expected_hashes = manifest.get("artifact_hashes_before")
    if not isinstance(expected_hashes, dict):
        raise MigrationError("migration manifest missing artifact_hashes_before")
    names_before = set(manifest.get("artifact_names_before") or expected_hashes.keys())

    staging = ensure_private_dir(run_dir / ".migration-restore-staging")
    try:
        with tarfile.open(archive, "r") as tar:
            try:
                tar.extractall(staging, filter=tarfile.data_filter)  # type: ignore[arg-type]
            except (AttributeError, TypeError):
                tar.extractall(staging)
        restored = []
        for child in staging.iterdir():
            dest = run_dir / child.name
            if dest.exists() or dest.is_symlink():
                if dest.is_dir() and not dest.is_symlink():
                    shutil.rmtree(dest)
                else:
                    try:
                        if dest.is_file() or dest.is_symlink():
                            dest.unlink()
                    except OSError as exc:
                        raise MigrationError(
                            f"cannot clear {dest.name} for restore: {exc}"
                        ) from exc
            if child.is_dir():
                shutil.copytree(child, dest)
                os.chmod(dest, 0o700)
            else:
                payload = secure_read_bytes(
                    child, max_bytes=64 * 1024 * 1024, require_private=False
                )
                secure_write_bytes(dest, payload, mode=0o600, containment_root=run_dir)
            restored.append(child.name)

        # Remove migration-created files that were not in the pre-migration snapshot.
        for child in list(run_dir.iterdir()):
            if child.name == BACKUP_DIRNAME or child.name.startswith(".migration"):
                continue
            if child.name in names_before:
                continue
            try:
                info = child.lstat()
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                shutil.rmtree(child)
            else:
                child.unlink()

        # Validate restored regular files against manifest hashes.
        for name, expected in expected_hashes.items():
            path = run_dir / name
            try:
                actual = _sha256_file(path)
            except PersistError as exc:
                raise MigrationError(f"restored artifact missing or unreadable: {name}") from exc
            if actual != expected:
                raise MigrationError(f"restored artifact hash mismatch: {name}")
        fsync_directory(run_dir)
        return {
            "result": "rolled_back",
            "restored": restored,
            "backup": str(archive),
            "status": read_status(run_dir),
            "audit_head": _audit_head(run_dir),
        }
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def verify_run_state(run_dir: Path | str) -> dict[str, Any]:
    """Read-only verification of schemas, hashes, and common problems."""
    from .audit import load_audit_trail, validate_audit_trail

    run_dir = Path(run_dir).expanduser().resolve()
    problems: list[str] = []
    info: dict[str, Any] = {
        "run_dir": str(run_dir),
        "run_id": run_dir.name,
        "runner_version": RUNNER_VERSION,
        "status": read_status(run_dir),
    }
    try:
        info["persistence_schema"] = _detect_persistence_schema(run_dir)
    except MigrationError as exc:
        problems.append(str(exc))
        info["persistence_schema"] = None

    meta = _load_run_json(run_dir)
    if meta is not None:
        info["run_metadata_schema"] = meta.get("schema_version")
        info["run_runner_version"] = meta.get("runner_version")
        info["run_keys"] = sorted(meta.keys())

    budget_path = run_dir / "iteration-budget.json"
    if budget_path.exists():
        try:
            budget = secure_read_json(budget_path, require_private=False)
            info["iteration_budget_schema"] = budget.get("schema_version")
            info["effective_limit"] = budget.get("effective_limit")
        except PersistError as exc:
            problems.append(f"iteration-budget: {exc}")

    try:
        trail = load_audit_trail(run_dir, require_private=False)
        validate_audit_trail(trail, expected_run_id=run_dir.name)
        info["audit_events"] = len(trail["events"])
        info["audit_head"] = trail["head_hash"]
    except Exception as exc:
        problems.append(f"audit: {exc}")

    journal = run_dir / ".txn.json"
    if journal.exists():
        problems.append("incomplete transaction journal present; run recover")
        info["txn_journal"] = True
    else:
        info["txn_journal"] = False

    for name in ("status", "run.json", "failure.json", "human_approval_request.json"):
        path = run_dir / name
        if not path.exists() and not path.is_symlink():
            continue
        try:
            if path.is_symlink():
                problems.append(f"{name} is a symlink")
            elif path.is_fifo():
                problems.append(f"{name} is a FIFO")
        except OSError as exc:
            problems.append(f"{name}: {exc}")

    info["problems"] = problems
    info["ok"] = not problems
    return info


def inspect_run(run_dir: Path | str) -> dict[str, Any]:
    """Operator-facing inspection without secrets."""
    verification = verify_run_state(run_dir)
    run_dir = Path(run_dir).expanduser().resolve()
    artifacts = []
    for child in sorted(run_dir.iterdir()):
        if child.name.startswith(".") and child.name not in {".txn.json"}:
            if child.name.startswith(".migration"):
                artifacts.append({"name": child.name, "kind": "migration"})
            continue
        try:
            info = child.lstat()
            artifacts.append(
                {
                    "name": child.name,
                    "size": info.st_size,
                    "mode": oct(info.st_mode & 0o777),
                    "is_symlink": child.is_symlink(),
                }
            )
        except OSError:
            continue
    verification["artifacts"] = artifacts
    verification["migrations"] = [m.migration_id for m in MIGRATIONS]
    return verification
