"""Append-only audit trail for run state transitions (DX-08).

The trail records a monotonic hash chain of events. It never stores tokens,
environment variables, credential URLs, or full report bodies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .persist import (
    PersistError,
    canonical_json_hash,
    secure_read_json,
    secure_write_json,
)
from .schemas import (
    AUDIT_TRAIL_SCHEMA_VERSION,
    PERSISTENCE_SCHEMA_VERSION,
    RUNNER_VERSION,
)

AUDIT_FILENAME = "audit-trail.json"
GENESIS_PREV_HASH = "0" * 64

_EVENT_FIELDS = frozenset(
    {
        "seq",
        "event_id",
        "run_id",
        "event",
        "previous_state",
        "new_state",
        "previous_hash",
        "artifact_hashes",
        "timestamp",
        "schema_version",
        "runner_version",
        "persistence_schema",
        "origin",
        "entry_hash",
    }
)

_ORIGINS = frozenset({"runner", "bridge", "resume", "migration", "recovery"})


class AuditError(ValueError):
    """Audit trail integrity or schema failure."""


def _entry_binding(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "seq": entry["seq"],
        "event_id": entry["event_id"],
        "run_id": entry["run_id"],
        "event": entry["event"],
        "previous_state": entry["previous_state"],
        "new_state": entry["new_state"],
        "previous_hash": entry["previous_hash"],
        "artifact_hashes": entry["artifact_hashes"],
        "timestamp": entry["timestamp"],
        "schema_version": entry["schema_version"],
        "runner_version": entry["runner_version"],
        "persistence_schema": entry["persistence_schema"],
        "origin": entry["origin"],
    }


def compute_entry_hash(entry: Mapping[str, Any]) -> str:
    return canonical_json_hash(_entry_binding(entry))


def empty_trail(run_id: str) -> dict[str, Any]:
    return {
        "schema_version": AUDIT_TRAIL_SCHEMA_VERSION,
        "persistence_schema": PERSISTENCE_SCHEMA_VERSION,
        "runner_version": RUNNER_VERSION,
        "run_id": run_id,
        "events": [],
        "head_hash": GENESIS_PREV_HASH,
    }


def load_audit_trail(run_dir: Path | str, *, require_private: bool = True) -> dict[str, Any]:
    path = Path(run_dir) / AUDIT_FILENAME
    try:
        path.lstat()
    except FileNotFoundError:
        return empty_trail(Path(run_dir).name)
    try:
        document = secure_read_json(path, require_private=require_private)
    except PersistError as exc:
        raise AuditError(str(exc)) from exc
    validate_audit_trail(document, expected_run_id=Path(run_dir).name)
    return document


def validate_audit_trail(
    document: Mapping[str, Any],
    *,
    expected_run_id: str,
) -> None:
    required = {
        "schema_version",
        "persistence_schema",
        "runner_version",
        "run_id",
        "events",
        "head_hash",
    }
    if set(document) != required:
        raise AuditError("audit trail has missing or unknown fields")
    if document.get("schema_version") != AUDIT_TRAIL_SCHEMA_VERSION:
        if isinstance(document.get("schema_version"), int) and int(
            document["schema_version"]
        ) > AUDIT_TRAIL_SCHEMA_VERSION:
            raise AuditError("audit trail schema is from a future runner")
        raise AuditError("audit trail schema_version mismatch")
    persistence = document.get("persistence_schema")
    if type(persistence) is int and persistence > PERSISTENCE_SCHEMA_VERSION:
        raise AuditError("audit trail persistence schema is from a future runner")
    if persistence != PERSISTENCE_SCHEMA_VERSION:
        raise AuditError("audit trail persistence_schema mismatch")
    if document.get("run_id") != expected_run_id:
        raise AuditError("audit trail run_id mismatch")
    events = document.get("events")
    if not isinstance(events, list):
        raise AuditError("audit trail events must be a list")
    previous_hash = GENESIS_PREV_HASH
    for index, entry in enumerate(events, start=1):
        if not isinstance(entry, dict) or set(entry) != _EVENT_FIELDS:
            raise AuditError(f"audit entry {index} is malformed")
        if entry.get("seq") != index:
            raise AuditError(f"audit entry {index} has non-monotonic seq")
        if entry.get("run_id") != expected_run_id:
            raise AuditError(f"audit entry {index} run_id mismatch")
        if entry.get("previous_hash") != previous_hash:
            raise AuditError(f"audit entry {index} breaks hash chain")
        if entry.get("origin") not in _ORIGINS:
            raise AuditError(f"audit entry {index} has invalid origin")
        if not isinstance(entry.get("timestamp"), str) or not entry["timestamp"]:
            raise AuditError(f"audit entry {index} missing timestamp")
        artifact_hashes = entry.get("artifact_hashes")
        if not isinstance(artifact_hashes, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in artifact_hashes.items()
        ):
            raise AuditError(f"audit entry {index} has invalid artifact_hashes")
        expected = compute_entry_hash(entry)
        if entry.get("entry_hash") != expected:
            raise AuditError(
                f"audit entry {index} hash mismatch (possible timestamp/replay tamper)"
            )
        previous_hash = expected
    if document.get("head_hash") != previous_hash:
        raise AuditError("audit trail head_hash does not match chain")


def append_audit_event(
    run_dir: Path | str,
    *,
    event: str,
    previous_state: str | None,
    new_state: str,
    artifact_hashes: Mapping[str, str] | None = None,
    origin: str = "runner",
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Append one hashed event. Caller must hold the appropriate run lock."""
    from .approval import utc_now_iso

    if origin not in _ORIGINS:
        raise AuditError(f"invalid audit origin: {origin}")
    run_dir = Path(run_dir)
    document = load_audit_trail(run_dir)
    seq = len(document["events"]) + 1
    event_id = f"{run_dir.name}:{seq:08d}"
    stamped_at = timestamp or utc_now_iso()
    entry: dict[str, Any] = {
        "seq": seq,
        "event_id": event_id,
        "run_id": run_dir.name,
        "event": event,
        "previous_state": previous_state,
        "new_state": new_state,
        "previous_hash": document["head_hash"],
        "artifact_hashes": dict(artifact_hashes or {}),
        "timestamp": stamped_at,
        "schema_version": AUDIT_TRAIL_SCHEMA_VERSION,
        "runner_version": RUNNER_VERSION,
        "persistence_schema": PERSISTENCE_SCHEMA_VERSION,
        "origin": origin,
        "entry_hash": "",
    }
    entry["entry_hash"] = compute_entry_hash(entry)
    document["events"] = [*document["events"], entry]
    document["head_hash"] = entry["entry_hash"]
    document["runner_version"] = RUNNER_VERSION
    document["persistence_schema"] = PERSISTENCE_SCHEMA_VERSION
    secure_write_json(run_dir / AUDIT_FILENAME, document)
    return entry
