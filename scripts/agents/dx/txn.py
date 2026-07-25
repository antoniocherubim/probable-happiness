"""Logical transactions for correlated status + artifact writes (DX-08).

Crash recovery completes an in-flight journal idempotently or restores the last
valid committed state. Recovery never invents approval, decision, or remote OID
success.

Critical transactions always hold ``.state.lock`` across journal, artifacts,
status, and audit. Specialty locks (``.approval.lock``, ``.resume.lock``) may
be held by callers *outside* that window, but never replace the state lock.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .atomic import run_scoped_lock
from .audit import append_audit_event, load_audit_trail
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
    RunEvent,
    StateTransitionError,
    read_state,
    transition_run,
)

JOURNAL_FILENAME = ".txn.json"
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
        if "/" in name or name.startswith(".") and name != JOURNAL_FILENAME:
            if name.startswith(".") and name.endswith(".tmp"):
                raise TransactionError(f"refusing temp artifact name: {name}")
        if Path(name).name != name:
            raise TransactionError(f"artifact name must be basename: {name}")
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

    run_dir = Path(txn.run_dir)
    previous = read_state(run_dir)
    previous_value = previous.value if previous else None
    if not txn._audit_previous_unset:
        previous_value = txn._audit_previous_state  # type: ignore[assignment]
    event_name = txn.event
    required = CRITICAL_BINDINGS.get(event_name, frozenset())
    present = {item.name for item in txn.artifacts}
    if required and not required.issubset(present):
        raise TransactionError(
            f"transaction for {event_name} missing artifacts: "
            f"{sorted(required - present)}"
        )
    hashes = _artifact_hashes(txn.artifacts)
    created_at = txn._audit_timestamp or utc_now_iso()

    # Idempotent path: status already at target and artifacts match — still
    # finish audit + clear journal so recovery never reports success with a
    # leftover `.txn.json` or missing audit event.
    if txn.status_event is not None and previous is not None:
        if result_already_applied(txn.status_event, previous.value):
            matching = True
            existing_failure_hash = None
            for item in txn.artifacts:
                target = run_dir / item.name
                try:
                    existing = secure_read_json(target, containment_root=run_dir)
                except (PersistError, OSError, ValueError):
                    matching = False
                    break
                if canonical_json_hash(existing) != hashes[item.name]:
                    if item.name == "failure.json":
                        existing_failure_hash = canonical_json_hash(existing)
                        return _finish_already_applied(
                            run_dir,
                            event_name=event_name,
                            previous_value=previous_value,
                            new_state=previous.value,
                            hashes={"failure.json": existing_failure_hash},
                            origin=txn.origin,
                            timestamp=created_at,
                        )
                    matching = False
                    break
            if matching:
                return _finish_already_applied(
                    run_dir,
                    event_name=event_name,
                    previous_value=previous_value,
                    new_state=previous.value,
                    hashes=hashes,
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
            result = transition_run(
                run_dir,
                txn.status_event,
                state_lock_held=True,
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

    Caller must already hold ``.state.lock``. Does not enforce CRITICAL_BINDINGS
    — artifact-bearing events must use ``LogicalTransaction`` /
    ``commit_status_and_artifacts`` instead.
    """
    from .approval import utc_now_iso
    from .state_machine import TRANSITIONS, _coerce_event

    typed = _coerce_event(event)
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
