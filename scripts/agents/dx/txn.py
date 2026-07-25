"""Logical transactions for correlated status + artifact writes (DX-08).

Crash recovery completes an in-flight journal idempotently or restores the last
valid committed state. Recovery never invents approval, decision, or remote OID
success.

Critical transactions always hold ``.state.lock`` across journal, artifacts,
status, and audit. Specialty locks (``.approval.lock``, ``.resume.lock``) may
be held by callers *outside* that window, but never replace the state lock.
"""

from __future__ import annotations

import re
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .atomic import run_scoped_lock
from .audit import AUDIT_FILENAME, append_audit_event, load_audit_trail
from .persist import (
    PersistError,
    canonical_json_hash,
    cleanup_orphan_temps,
    secure_exclusive_write_json,
    secure_read_json,
    secure_unlink,
    secure_write_json,
)
from .schemas import (
    FUTURE_SCHEMA_REFUSAL,
    RUNNER_VERSION,
    TXN_JOURNAL_SCHEMA_VERSION,
)
from .state_machine import (
    STATE_LOCK_FILENAME,
    STATUS_FILENAME,
    RunEvent,
    StateTransitionError,
    read_state,
    transition_run,
)

JOURNAL_FILENAME = ".txn.json"
# Artifacts must never replace lock, status, journal, or audit authority files.
RESERVED_TRANSACTION_NAMES = frozenset(
    {
        STATE_LOCK_FILENAME,
        ".approval.lock",
        ".resume.lock",
        ".delivery.lock",
        STATUS_FILENAME,
        JOURNAL_FILENAME,
        AUDIT_FILENAME,
    }
)
JOURNAL_COMMITTED = "committed"
JOURNAL_PREPARING = "preparing"
JOURNAL_COMMITTING = "committing"
JOURNAL_ABORTING = "aborting"

_JOURNAL_ORIGINS = frozenset({"runner", "bridge", "resume", "migration", "recovery"})
_JOURNAL_PHASES = frozenset(
    {JOURNAL_PREPARING, JOURNAL_COMMITTING, JOURNAL_COMMITTED, JOURNAL_ABORTING}
)
_JOURNAL_KEYS = frozenset(
    {
        "schema_version",
        "runner_version",
        "run_id",
        "phase",
        "event",
        "status_event",
        "previous_state",
        "artifact_hashes",
        "artifacts",
        "exclusive_artifacts",
        "origin",
        "created_at",
        "new_state",
    }
)

# Critical DX-07 event families that bind status to artifacts.
CRITICAL_BINDINGS: dict[str, frozenset[str]] = {
    RunEvent.RUN_BLOCKED.value: frozenset({"failure.json"}),
    RunEvent.APPROVAL_REQUESTED.value: frozenset({"human_approval_request.json"}),
    RunEvent.HUMAN_APPROVED.value: frozenset({"human_approval_decision.json"}),
    RunEvent.HUMAN_REJECTED.value: frozenset({"human_rejection.json"}),
    RunEvent.RECOVER_HUMAN_APPROVED.value: frozenset({"human_approval_decision.json"}),
    RunEvent.ITERATION_BUDGET_EXTENDED.value: frozenset({"iteration-budget.json"}),
}

# Minimum required keys for critical artifact payloads (extras allowed).
_CRITICAL_ARTIFACT_KEYS: dict[str, frozenset[str]] = {
    "failure.json": frozenset(
        {
            "schema_version",
            "reason",
            "phase",
            "iteration",
            "report",
            "recorded_at",
        }
    ),
    "human_approval_request.json": frozenset(
        {
            "schema_version",
            "technical_status",
            "task",
            "task_id",
            "run_id",
            "base_commit",
            "worktree",
            "review_report",
            "diff_hash",
            "callback_token",
            "token_consumed",
            "created_at",
        }
    ),
    "human_approval_decision.json": frozenset(
        {
            "schema_version",
            "decision",
            "run_id",
            "diff_hash",
            "callback_token",
            "telegram_user_id",
            "telegram_chat_id",
            "decided_at",
        }
    ),
    "human_rejection.json": frozenset(
        {
            "schema_version",
            "decision",
            "run_id",
            "diff_hash",
            "telegram_user_id",
            "telegram_chat_id",
            "decided_at",
        }
    ),
    "iteration-budget.json": frozenset(
        {
            "schema_version",
            "run_id",
            "original_limit",
            "effective_limit",
            "extensions",
            "updated_at",
        }
    ),
}

# Current schema versions for critical artifacts (future schemas fail closed).
_APPROVAL_ARTIFACT_SCHEMA_VERSION = 1
_FAILURE_ARTIFACT_SCHEMA_VERSION = 1


class TransactionError(ValueError):
    """Logical transaction failed closed without inventing success."""


@dataclass
class ArtifactWrite:
    name: str
    payload: dict[str, Any]
    exclusive: bool = False


@dataclass
class LogicalTransaction:
    run_dir: Path
    event: str
    origin: str = "runner"
    artifacts: list[ArtifactWrite] = field(default_factory=list)
    status_event: RunEvent | str | None = None
    # Specialty lock name for LogicalTransaction.commit() outer acquisition.
    # The state lock is always acquired for the commit body itself.
    _lock_name: str = STATE_LOCK_FILENAME
    # Recovery may pin audit fields from the durable journal.
    _audit_previous_state: str | None | object = field(default=None, repr=False)
    _audit_timestamp: str | None = None
    _audit_previous_unset: bool = True

    def add_json(
        self, name: str, payload: dict[str, Any], *, exclusive: bool = False
    ) -> None:
        if Path(name).name != name or "/" in name or name in {".", ".."}:
            raise TransactionError(f"artifact name must be basename: {name}")
        if name in RESERVED_TRANSACTION_NAMES or name.startswith("."):
            raise TransactionError(f"refusing reserved artifact name: {name}")
        self.artifacts.append(
            ArtifactWrite(name=name, payload=payload, exclusive=exclusive)
        )

    def commit(self) -> dict[str, Any]:
        run_dir = Path(self.run_dir)
        # Consistent order: specialty lock (if any) then canonical state lock.
        if self._lock_name != STATE_LOCK_FILENAME:
            with run_scoped_lock(run_dir, lock_name=self._lock_name):
                with run_scoped_lock(run_dir, lock_name=STATE_LOCK_FILENAME):
                    return _commit_under_state_lock(self)
        with run_scoped_lock(run_dir, lock_name=STATE_LOCK_FILENAME):
            return _commit_under_state_lock(self)


def _journal_path(run_dir: Path) -> Path:
    return Path(run_dir) / JOURNAL_FILENAME


def _write_journal(run_dir: Path, payload: dict[str, Any]) -> None:
    secure_write_json(_journal_path(run_dir), payload, containment_root=run_dir)


def validate_journal(document: Mapping[str, Any], *, expected_run_id: str) -> None:
    """Reject malformed or future journals before any recovery mutation."""
    keys = set(document)
    if not keys.issubset(_JOURNAL_KEYS):
        raise TransactionError("transaction journal has unknown fields")
    required = _JOURNAL_KEYS - {"new_state"}
    if not required.issubset(keys):
        raise TransactionError("transaction journal missing required fields")
    schema = document.get("schema_version")
    if type(schema) is int and schema > TXN_JOURNAL_SCHEMA_VERSION:
        raise TransactionError(FUTURE_SCHEMA_REFUSAL)
    if schema != TXN_JOURNAL_SCHEMA_VERSION:
        raise TransactionError("transaction journal schema_version mismatch")
    if document.get("run_id") != expected_run_id:
        raise TransactionError("transaction journal run_id mismatch")
    if document.get("phase") not in _JOURNAL_PHASES:
        raise TransactionError(f"unknown journal phase: {document.get('phase')!r}")
    if document.get("origin") not in _JOURNAL_ORIGINS:
        raise TransactionError("transaction journal has invalid origin")
    if not isinstance(document.get("event"), str) or not document["event"]:
        raise TransactionError("transaction journal event missing")
    if not isinstance(document.get("created_at"), str) or not document["created_at"]:
        raise TransactionError("transaction journal created_at missing")
    hashes = document.get("artifact_hashes")
    if not isinstance(hashes, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in hashes.items()
    ):
        raise TransactionError("transaction journal artifact_hashes malformed")
    artifacts = document.get("artifacts")
    if not isinstance(artifacts, dict) or not all(
        isinstance(k, str) and isinstance(v, dict) for k, v in artifacts.items()
    ):
        raise TransactionError("transaction journal artifacts malformed")
    exclusive = document.get("exclusive_artifacts")
    if not isinstance(exclusive, list) or not all(isinstance(x, str) for x in exclusive):
        raise TransactionError("transaction journal exclusive_artifacts malformed")
    for name, payload in artifacts.items():
        digest = hashes.get(name)
        if digest is None:
            raise TransactionError(f"transaction journal missing hash for {name}")
        if canonical_json_hash(payload) != digest:
            raise TransactionError(f"transaction journal hash mismatch for {name}")
    previous = document.get("previous_state")
    if previous is not None and not isinstance(previous, str):
        raise TransactionError("transaction journal previous_state malformed")
    status_event = document.get("status_event")
    if status_event is not None and not isinstance(status_event, str):
        raise TransactionError("transaction journal status_event malformed")
    new_state = document.get("new_state")
    if new_state is not None and not isinstance(new_state, str):
        raise TransactionError("transaction journal new_state malformed")


def _read_journal(run_dir: Path) -> dict[str, Any] | None:
    path = _journal_path(run_dir)
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    document = secure_read_json(path, containment_root=run_dir)
    validate_journal(document, expected_run_id=run_dir.name)
    return document


def _artifact_hashes(artifacts: list[ArtifactWrite]) -> dict[str, str]:
    return {item.name: canonical_json_hash(item.payload) for item in artifacts}


def _status_event_name(status_event: RunEvent | str | None) -> str | None:
    if status_event is None:
        return None
    if isinstance(status_event, RunEvent):
        return status_event.value
    return str(status_event)


def _require_nonempty_str(payload: Mapping[str, Any], key: str, *, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise TransactionError(f"malformed required artifact: {label} ({key})")
    return value


def _require_positive_int(payload: Mapping[str, Any], key: str, *, label: str) -> int:
    value = payload.get(key)
    if type(value) is not int or value <= 0:
        raise TransactionError(f"malformed required artifact: {label} ({key})")
    return value


def _refuse_future_schema(schema: object, *, current: int, label: str) -> int:
    if type(schema) is not int or schema < 1:
        raise TransactionError(f"malformed required artifact: {label}")
    if schema > current:
        raise TransactionError(FUTURE_SCHEMA_REFUSAL)
    if schema != current:
        raise TransactionError(f"malformed required artifact: {label}")
    return schema


def _validate_approval_request_payload(
    payload: Mapping[str, Any], *, expected_run_id: str
) -> None:
    _refuse_future_schema(
        payload.get("schema_version"),
        current=_APPROVAL_ARTIFACT_SCHEMA_VERSION,
        label="human_approval_request.json",
    )
    if payload.get("run_id") != expected_run_id:
        raise TransactionError("approval request run_id mismatch")
    # Gate may only open from technical APPROVED; other statuses are forged.
    technical_status = _require_nonempty_str(
        payload, "technical_status", label="human_approval_request.json"
    )
    if technical_status != "APPROVED":
        raise TransactionError(
            "approval request technical_status must be APPROVED"
        )
    for key in (
        "task",
        "task_id",
        "base_commit",
        "worktree",
        "review_report",
        "created_at",
    ):
        _require_nonempty_str(payload, key, label="human_approval_request.json")
    diff_hash = _require_nonempty_str(
        payload, "diff_hash", label="human_approval_request.json"
    )
    if not re.fullmatch(r"[0-9a-f]{64}", diff_hash):
        raise TransactionError(
            "malformed required artifact: human_approval_request.json (diff_hash)"
        )
    token = _require_nonempty_str(
        payload, "callback_token", label="human_approval_request.json"
    )
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise TransactionError(
            "malformed required artifact: human_approval_request.json (callback_token)"
        )
    if type(payload.get("token_consumed")) is not bool:
        raise TransactionError("malformed required artifact: human_approval_request.json")


def _resolve_bound_approval_request(
    txn: LogicalTransaction, present: Mapping[str, ArtifactWrite]
) -> dict[str, Any]:
    """Load the request from the transaction or a durable on-disk artifact."""
    if "human_approval_request.json" in present:
        payload = present["human_approval_request.json"].payload
        if not isinstance(payload, dict) or not payload:
            raise TransactionError("malformed required artifact: human_approval_request.json")
        return payload
    request_path = Path(txn.run_dir) / "human_approval_request.json"
    try:
        request_path.lstat()
    except FileNotFoundError as exc:
        raise TransactionError(
            "critical approval decision/rejection requires a corresponding request"
        ) from exc
    try:
        return secure_read_json(request_path, containment_root=Path(txn.run_dir))
    except PersistError as exc:
        raise TransactionError(
            f"critical approval decision/rejection cannot read request: {exc}"
        ) from exc


def _validate_decision_payload(
    decision: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    expected_run_id: str,
) -> None:
    _refuse_future_schema(
        decision.get("schema_version"),
        current=_APPROVAL_ARTIFACT_SCHEMA_VERSION,
        label="human_approval_decision.json",
    )
    _validate_approval_request_payload(request, expected_run_id=expected_run_id)
    if decision.get("decision") != "approve":
        raise TransactionError("approval decision must be approve")
    if decision.get("run_id") != expected_run_id:
        raise TransactionError("approval decision run_id mismatch")
    if decision.get("run_id") != request.get("run_id"):
        raise TransactionError("approval decision run_id mismatch")
    if not decision.get("diff_hash") or decision.get("diff_hash") != request.get(
        "diff_hash"
    ):
        raise TransactionError("approval decision diff_hash mismatch")
    expected_token = request.get("callback_token")
    if not expected_token or decision.get("callback_token") != expected_token:
        raise TransactionError("approval decision callback_token mismatch")
    if request.get("token_consumed") is not True:
        raise TransactionError("approval request token not consumed")
    _require_positive_int(
        decision, "telegram_user_id", label="human_approval_decision.json"
    )
    _require_positive_int(
        decision, "telegram_chat_id", label="human_approval_decision.json"
    )
    _require_nonempty_str(
        decision, "decided_at", label="human_approval_decision.json"
    )


def _validate_rejection_payload(
    rejection: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    expected_run_id: str,
) -> None:
    _refuse_future_schema(
        rejection.get("schema_version"),
        current=_APPROVAL_ARTIFACT_SCHEMA_VERSION,
        label="human_rejection.json",
    )
    _validate_approval_request_payload(request, expected_run_id=expected_run_id)
    if rejection.get("decision") != "reject":
        raise TransactionError("rejection decision must be reject")
    if rejection.get("run_id") != expected_run_id:
        raise TransactionError("rejection run_id mismatch")
    if rejection.get("run_id") != request.get("run_id"):
        raise TransactionError("rejection run_id mismatch")
    if not rejection.get("diff_hash") or rejection.get("diff_hash") != request.get(
        "diff_hash"
    ):
        raise TransactionError("rejection diff_hash mismatch")
    if request.get("token_consumed") is not True:
        raise TransactionError("approval request token not consumed")
    _require_positive_int(
        rejection, "telegram_user_id", label="human_rejection.json"
    )
    _require_positive_int(
        rejection, "telegram_chat_id", label="human_rejection.json"
    )
    _require_nonempty_str(rejection, "decided_at", label="human_rejection.json")


def _validate_failure_payload(payload: Mapping[str, Any]) -> None:
    _refuse_future_schema(
        payload.get("schema_version"),
        current=_FAILURE_ARTIFACT_SCHEMA_VERSION,
        label="failure.json",
    )
    expected_keys = _CRITICAL_ARTIFACT_KEYS["failure.json"]
    if set(payload) != expected_keys:
        raise TransactionError("malformed required artifact: failure.json")
    _require_nonempty_str(payload, "reason", label="failure.json")
    _require_nonempty_str(payload, "phase", label="failure.json")
    _require_nonempty_str(payload, "recorded_at", label="failure.json")
    iteration = payload.get("iteration")
    if type(iteration) is not int or iteration < 0:
        raise TransactionError("malformed required artifact: failure.json")
    report = payload.get("report")
    if report is not None and (not isinstance(report, str) or not report):
        raise TransactionError("malformed required artifact: failure.json")


def _validate_iteration_budget_payload(
    payload: Mapping[str, Any],
    *,
    expected_run_id: str,
    run_dir: Path,
) -> None:
    from .runstate import IterationBudgetError, validate_iteration_budget_document
    from .schemas import (
        ITERATION_BUDGET_SCHEMA_VERSION,
        SUPPORTED_ITERATION_BUDGET_SCHEMAS,
    )

    schema = payload.get("schema_version")
    if type(schema) is not int or schema < 1:
        raise TransactionError("malformed required artifact: iteration-budget.json")
    if schema > ITERATION_BUDGET_SCHEMA_VERSION:
        raise TransactionError(FUTURE_SCHEMA_REFUSAL)
    if schema not in SUPPORTED_ITERATION_BUDGET_SCHEMAS:
        raise TransactionError("malformed required artifact: iteration-budget.json")
    if payload.get("run_id") != expected_run_id:
        raise TransactionError("iteration-budget run_id mismatch")
    original_limit = payload.get("original_limit")
    if type(original_limit) is not int or original_limit < 1:
        raise TransactionError("malformed required artifact: iteration-budget.json")
    try:
        validate_iteration_budget_document(
            payload,
            run_dir=run_dir,
            original_limit=original_limit,
        )
    except IterationBudgetError as exc:
        raise TransactionError(
            f"malformed required artifact: iteration-budget.json ({exc})"
        ) from exc


def _validate_critical_bindings(txn: LogicalTransaction) -> None:
    """Refuse mismatched event/status_event or malformed required payloads."""
    event_name = txn.event
    status_name = _status_event_name(txn.status_event)
    event_critical = event_name in CRITICAL_BINDINGS
    status_critical = status_name is not None and status_name in CRITICAL_BINDINGS
    if not event_critical and not status_critical:
        return
    if status_name is None:
        raise TransactionError(
            f"critical event {event_name} requires matching status_event"
        )
    if event_name != status_name:
        raise TransactionError(
            f"transaction event {event_name!r} does not match status_event "
            f"{status_name!r}; refusing critical promotion"
        )
    required = CRITICAL_BINDINGS[event_name]
    present = {item.name: item for item in txn.artifacts}
    missing = required - present.keys()
    if missing:
        raise TransactionError(
            f"transaction for {event_name} missing artifacts: {sorted(missing)}"
        )
    expected_run_id = Path(txn.run_dir).name
    for name in sorted(required):
        payload = present[name].payload
        if not isinstance(payload, dict) or not payload:
            raise TransactionError(f"malformed required artifact: {name}")
        schema = payload.get("schema_version")
        if type(schema) is not int or schema < 1:
            raise TransactionError(f"malformed required artifact: {name}")
        expected_keys = _CRITICAL_ARTIFACT_KEYS.get(name)
        if expected_keys is not None and not expected_keys.issubset(payload.keys()):
            raise TransactionError(f"malformed required artifact: {name}")

    if "failure.json" in required:
        _validate_failure_payload(present["failure.json"].payload)
    if "human_approval_request.json" in required:
        _validate_approval_request_payload(
            present["human_approval_request.json"].payload,
            expected_run_id=expected_run_id,
        )
    if "human_approval_decision.json" in required:
        request = _resolve_bound_approval_request(txn, present)
        _validate_decision_payload(
            present["human_approval_decision.json"].payload,
            request,
            expected_run_id=expected_run_id,
        )
    if "human_rejection.json" in required:
        request = _resolve_bound_approval_request(txn, present)
        _validate_rejection_payload(
            present["human_rejection.json"].payload,
            request,
            expected_run_id=expected_run_id,
        )
    if "iteration-budget.json" in required:
        _validate_iteration_budget_payload(
            present["iteration-budget.json"].payload,
            expected_run_id=expected_run_id,
            run_dir=Path(txn.run_dir),
        )


def _audit_covers_event(
    run_dir: Path,
    *,
    event: str,
    new_state: str,
    artifact_hashes: Mapping[str, str],
    previous_state: str | None | object = None,
    timestamp: str | None = None,
    check_previous: bool = False,
) -> bool:
    try:
        trail = load_audit_trail(run_dir)
    except Exception:
        return False
    events = trail.get("events") or []
    if not events:
        return False
    last = events[-1]
    if last.get("event") != event or last.get("new_state") != new_state:
        return False
    if check_previous and last.get("previous_state") != previous_state:
        return False
    if timestamp is not None and last.get("timestamp") != timestamp:
        return False
    recorded = last.get("artifact_hashes") or {}
    for name, digest in artifact_hashes.items():
        if recorded.get(name) != digest:
            return False
    return True


def _finish_already_applied(
    run_dir: Path,
    *,
    event_name: str,
    previous_value: str | None,
    new_state: str,
    hashes: dict[str, str],
    origin: str,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Ensure audit exists and clear any leftover journal after idempotent apply."""
    if not _audit_covers_event(
        run_dir,
        event=event_name,
        new_state=new_state,
        artifact_hashes=hashes,
        previous_state=previous_value,
        timestamp=timestamp,
        check_previous=True,
    ):
        from .approval import utc_now_iso

        append_audit_event(
            run_dir,
            event=event_name,
            previous_state=previous_value,
            new_state=new_state,
            artifact_hashes=hashes,
            origin=origin,
            timestamp=timestamp or utc_now_iso(),
        )
    if _journal_path(run_dir).exists():
        secure_unlink(_journal_path(run_dir), containment_root=run_dir)
    cleanup_orphan_temps(run_dir)
    return {
        "event": event_name,
        "previous_state": previous_value,
        "new_state": new_state,
        "artifact_hashes": hashes,
        "result": "already_applied",
    }


def _critical_replay_binding_matches(
    txn: LogicalTransaction, hashes: Mapping[str, str]
) -> tuple[bool, dict[str, str]]:
    """Return whether on-disk critical bindings authorize idempotent replay.

    Destination status alone is never enough: each required artifact must be
    readable and contract-valid. Byte-identical proposals match; a durable
    valid ``failure.json`` wins over a different proposed reason. Missing,
    corrupt, future-schema, or mismatched bindings do not match.
    """
    run_dir = Path(txn.run_dir)
    matching = True
    replay_hashes = dict(hashes)
    for item in txn.artifacts:
        target = run_dir / item.name
        try:
            existing = secure_read_json(target, containment_root=run_dir)
        except (PersistError, OSError, ValueError):
            return False, replay_hashes
        existing_hash = canonical_json_hash(existing)
        if existing_hash == hashes[item.name]:
            if item.name == "failure.json":
                try:
                    _validate_failure_payload(existing)
                except TransactionError:
                    return False, replay_hashes
            continue
        if item.name == "failure.json":
            try:
                _validate_failure_payload(existing)
            except TransactionError:
                return False, replay_hashes
            # First durable failure wins; do not rewrite or bless a
            # different proposed reason over a valid existing one.
            replay_hashes = {"failure.json": existing_hash}
            continue
        return False, replay_hashes
    return matching, replay_hashes


def _publish_artifact(run_dir: Path, item: ArtifactWrite, digest: str) -> None:
    target = run_dir / item.name
    try:
        existing = secure_read_json(target, containment_root=run_dir)
        if canonical_json_hash(existing) == digest:
            return
        if item.exclusive:
            raise TransactionError(
                f"exclusive artifact {item.name} already exists with different content"
            )
    except PersistError as exc:
        message = str(exc).lower()
        if item.exclusive and "missing" not in message:
            # Symlink/special/insecure at exclusive path must fail closed.
            if any(
                token in message
                for token in ("symlink", "fifo", "socket", "device", "hard link", "insecure")
            ):
                raise TransactionError(str(exc)) from exc
        # Missing or rewriteable mismatch falls through to publish.
    if item.exclusive:
        if not secure_exclusive_write_json(
            target, item.payload, containment_root=run_dir
        ):
            existing = secure_read_json(target, containment_root=run_dir)
            if canonical_json_hash(existing) != digest:
                raise TransactionError(
                    f"exclusive publish lost race for {item.name}"
                )
        return
    secure_write_json(target, item.payload, containment_root=run_dir)


def _commit_under_state_lock(txn: LogicalTransaction) -> dict[str, Any]:
    from .approval import utc_now_iso
    from .state_machine import _transition_under_lock

    run_dir = Path(txn.run_dir)
    # Bindings and payload contracts must hold before any mutation.
    _validate_critical_bindings(txn)
    previous = read_state(run_dir)
    previous_value = previous.value if previous else None
    if not txn._audit_previous_unset:
        previous_value = txn._audit_previous_state  # type: ignore[assignment]
    event_name = txn.event
    hashes = _artifact_hashes(txn.artifacts)
    created_at = txn._audit_timestamp or utc_now_iso()

    # Refuse unsafe destination paths before any journal/status mutation.
    from .persist import _refuse_unsafe_existing_target

    for item in txn.artifacts:
        try:
            _refuse_unsafe_existing_target(run_dir / item.name)
        except PersistError as exc:
            raise TransactionError(str(exc)) from exc

    # Destination state alone never authorizes critical replay. When status is
    # already at the critical target, prove a contract-valid on-disk binding
    # (byte-identical proposal, or first durable valid failure.json). Missing,
    # corrupt, future-schema, or mismatched bindings refuse without mutation —
    # never fall through to journal/publish to "repair" the binding.
    if (
        txn.status_event is not None
        and previous is not None
        and event_name in CRITICAL_BINDINGS
    ):
        from .state_machine import TRANSITIONS, _coerce_event

        target = TRANSITIONS[_coerce_event(txn.status_event)].target.value
        if previous.value == target:
            matching, replay_hashes = _critical_replay_binding_matches(txn, hashes)
            if matching:
                return _finish_already_applied(
                    run_dir,
                    event_name=event_name,
                    previous_value=previous_value,
                    new_state=previous.value,
                    hashes=replay_hashes,
                    origin=txn.origin,
                    timestamp=created_at,
                )
            raise TransactionError(
                "critical destination state lacks a matching bound artifact; "
                "refusing replay that would invent or repair the binding"
            )
    elif txn.status_event is not None and previous is not None:
        if result_already_applied(txn.status_event, previous.value):
            return _finish_already_applied(
                run_dir,
                event_name=event_name,
                previous_value=previous_value,
                new_state=previous.value,
                hashes=dict(hashes),
                origin=txn.origin,
                timestamp=created_at,
            )

    journal = {
        "schema_version": TXN_JOURNAL_SCHEMA_VERSION,
        "runner_version": RUNNER_VERSION,
        "run_id": run_dir.name,
        "phase": JOURNAL_PREPARING,
        "event": event_name,
        "status_event": (
            txn.status_event.value
            if isinstance(txn.status_event, RunEvent)
            else txn.status_event
        ),
        "previous_state": previous_value
        if txn._audit_previous_unset
        else txn._audit_previous_state,
        "artifact_hashes": hashes,
        "artifacts": {item.name: item.payload for item in txn.artifacts},
        "exclusive_artifacts": [item.name for item in txn.artifacts if item.exclusive],
        "origin": txn.origin if txn.origin != "recovery" else "recovery",
        "created_at": created_at,
    }
    # Fresh commits use live previous_state; recovery keeps journal previous.
    if txn._audit_previous_unset:
        journal["previous_state"] = previous.value if previous else None
    _write_journal(run_dir, journal)

    journal["phase"] = JOURNAL_COMMITTING
    _write_journal(run_dir, journal)

    for item in txn.artifacts:
        _publish_artifact(run_dir, item, hashes[item.name])

    new_state = previous.value if previous else None
    if txn.status_event is not None:
        try:
            # Private under-lock apply after CRITICAL_BINDINGS validation above.
            # Public transition_run has no caller-spoofable bypass.
            result = _transition_under_lock(
                run_dir,
                (
                    txn.status_event
                    if isinstance(txn.status_event, RunEvent)
                    else RunEvent(txn.status_event)
                ),
                expected=None,
                record_audit=False,
            )
            new_state = result.current.value
        except StateTransitionError as exc:
            current = read_state(run_dir)
            if current is not None and result_already_applied(
                txn.status_event, current.value
            ):
                new_state = current.value
            else:
                journal["phase"] = JOURNAL_ABORTING
                _write_journal(run_dir, journal)
                raise TransactionError(str(exc)) from exc

    audit_previous = (
        journal["previous_state"]
        if not txn._audit_previous_unset
        else (previous.value if previous else None)
    )
    resolved_state = new_state or (
        previous.value if previous else ""
    ) or ""
    if not _audit_covers_event(
        run_dir,
        event=event_name,
        new_state=resolved_state,
        artifact_hashes=hashes,
        previous_state=audit_previous,
        timestamp=created_at,
        check_previous=True,
    ):
        append_audit_event(
            run_dir,
            event=event_name,
            previous_state=audit_previous,
            new_state=resolved_state,
            artifact_hashes=hashes,
            origin=txn.origin,
            timestamp=created_at,
        )

    journal["phase"] = JOURNAL_COMMITTED
    journal["new_state"] = new_state
    _write_journal(run_dir, journal)
    secure_unlink(_journal_path(run_dir), containment_root=run_dir)
    cleanup_orphan_temps(run_dir)
    return {
        "event": event_name,
        "previous_state": audit_previous,
        "new_state": new_state,
        "artifact_hashes": hashes,
        "result": "committed",
    }


def _commit_locked(
    txn: LogicalTransaction,
    *,
    state_lock_held: bool = False,
) -> dict[str, Any]:
    """Commit under ``.state.lock`` (acquire unless caller already holds it)."""
    run_dir = Path(txn.run_dir)
    context = (
        nullcontext()
        if state_lock_held
        else run_scoped_lock(run_dir, lock_name=STATE_LOCK_FILENAME)
    )
    with context:
        return _commit_under_state_lock(txn)


def result_already_applied(status_event: RunEvent | str, current: str) -> bool:
    from .state_machine import TRANSITIONS, _coerce_event

    typed = _coerce_event(status_event)
    return TRANSITIONS[typed].target.value == current and TRANSITIONS[typed].idempotent


def recover_run_transactions(run_dir: Path | str) -> dict[str, Any]:
    """
    Complete or abort an interrupted journal.

    Never invents HUMAN_APPROVED / decision / remote OID. If committing had
    already written artifacts and the status event is idempotently applicable,
    finishes the transaction; otherwise restores by clearing a preparing journal
    without promoting state.

    Preserves the journal's ``previous_state`` and ``created_at`` when writing
    the recovery audit event.
    """
    run_dir = Path(run_dir)
    with run_scoped_lock(run_dir, lock_name=STATE_LOCK_FILENAME):
        try:
            journal = _read_journal(run_dir)
        except TransactionError:
            # Malformed/future journal: fail closed without mutation.
            raise
        if journal is None:
            removed = cleanup_orphan_temps(run_dir)
            return {"result": "clean", "removed_temps": removed}
        phase = journal.get("phase")
        if phase == JOURNAL_COMMITTED:
            secure_unlink(_journal_path(run_dir), containment_root=run_dir)
            return {"result": "cleared_committed_marker"}
        if phase == JOURNAL_PREPARING:
            secure_unlink(_journal_path(run_dir), containment_root=run_dir)
            cleanup_orphan_temps(run_dir)
            return {"result": "aborted_preparing", "event": journal.get("event")}
        if phase in {JOURNAL_COMMITTING, JOURNAL_ABORTING}:
            artifacts = journal.get("artifacts") or {}
            exclusive = set(journal.get("exclusive_artifacts") or [])
            txn = LogicalTransaction(
                run_dir=run_dir,
                event=str(journal.get("event") or "recovery"),
                origin="recovery",
                status_event=journal.get("status_event"),
                _audit_previous_state=journal.get("previous_state"),
                _audit_timestamp=str(journal.get("created_at")),
                _audit_previous_unset=False,
            )
            for name, payload in artifacts.items():
                txn.add_json(str(name), payload, exclusive=str(name) in exclusive)
            outcome = _commit_under_state_lock(txn)
            outcome["result"] = "recovered"
            return outcome
        raise TransactionError(f"unknown journal phase: {phase!r}")


def commit_status_and_artifacts(
    run_dir: Path | str,
    *,
    event: str,
    status_event: RunEvent | str,
    artifacts: Mapping[str, dict[str, Any]],
    origin: str = "runner",
    lock_name: str = STATE_LOCK_FILENAME,
    exclusive: frozenset[str] | set[str] | None = None,
) -> dict[str, Any]:
    """Convenience wrapper used by record-failure / approval paths."""
    exclusive_names = exclusive or frozenset()
    txn = LogicalTransaction(
        run_dir=Path(run_dir),
        event=event,
        origin=origin,
        status_event=status_event,
        _lock_name=lock_name,
    )
    for name, payload in artifacts.items():
        txn.add_json(name, payload, exclusive=name in exclusive_names)
    run_path = Path(run_dir)
    if lock_name != STATE_LOCK_FILENAME:
        with run_scoped_lock(run_path, lock_name=lock_name):
            return _commit_locked(txn, state_lock_held=False)
    with run_scoped_lock(run_path, lock_name=STATE_LOCK_FILENAME):
        return _commit_locked(txn, state_lock_held=True)


def commit_status_with_audit_locked(
    run_dir: Path,
    *,
    event: RunEvent | str,
    origin: str = "runner",
) -> dict[str, Any]:
    """Journal status+audit for ordinary transitions (no correlated artifacts).

    Caller must already hold ``.state.lock``. Critical artifact-bearing events
    are refused before any journal/status/audit mutation; use
    ``LogicalTransaction`` / ``commit_status_and_artifacts`` instead.
    """
    from .approval import utc_now_iso
    from .state_machine import TRANSITIONS, _coerce_event

    typed = _coerce_event(event)
    if typed.value in CRITICAL_BINDINGS:
        raise TransactionError(
            f"event {typed.value} requires LogicalTransaction with bound "
            "artifacts; refusing artifactless status/audit mutation"
        )
    previous = read_state(run_dir)
    previous_value = previous.value if previous else None
    spec = TRANSITIONS[typed]
    if previous is not None and previous == spec.target and spec.idempotent:
        return _finish_already_applied(
            run_dir,
            event_name=typed.value,
            previous_value=previous_value,
            new_state=previous_value,
            hashes={},
            origin=origin,
        )
    created_at = utc_now_iso()
    journal = {
        "schema_version": TXN_JOURNAL_SCHEMA_VERSION,
        "runner_version": RUNNER_VERSION,
        "run_id": run_dir.name,
        "phase": JOURNAL_PREPARING,
        "event": typed.value,
        "status_event": typed.value,
        "previous_state": previous_value,
        "artifact_hashes": {},
        "artifacts": {},
        "exclusive_artifacts": [],
        "origin": origin,
        "created_at": created_at,
    }
    _write_journal(run_dir, journal)
    journal["phase"] = JOURNAL_COMMITTING
    _write_journal(run_dir, journal)
    result = transition_run(
        run_dir,
        typed,
        state_lock_held=True,
        record_audit=False,
    )
    new_state = result.current.value
    if not _audit_covers_event(
        run_dir,
        event=typed.value,
        new_state=new_state,
        artifact_hashes={},
        previous_state=previous_value,
        timestamp=created_at,
        check_previous=True,
    ):
        append_audit_event(
            run_dir,
            event=typed.value,
            previous_state=previous_value,
            new_state=new_state,
            artifact_hashes={},
            origin=origin,
            timestamp=created_at,
        )
    journal["phase"] = JOURNAL_COMMITTED
    journal["new_state"] = new_state
    _write_journal(run_dir, journal)
    secure_unlink(_journal_path(run_dir), containment_root=run_dir)
    cleanup_orphan_temps(run_dir)
    return {
        "event": typed.value,
        "previous_state": previous_value,
        "new_state": new_state,
        "artifact_hashes": {},
        "result": "committed",
    }
