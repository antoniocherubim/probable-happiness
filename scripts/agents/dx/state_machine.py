"""Typed, compare-and-set run status transitions for the local-only workflow."""

from __future__ import annotations

import json
import os
import stat
from contextlib import nullcontext
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable

from .atomic import atomic_write_json, run_scoped_lock


STATE_FILENAME = "state.json"
STATE_LOCK_FILENAME = ".state.lock"
_MAX_STATE_BYTES = 1024 * 1024
_FAILURE_FIELDS = {
    "schema_version",
    "reason",
    "phase",
    "iteration",
    "report",
    "recorded_at",
}


class RunState(str, Enum):
    EXECUTING = "EXECUTING"
    REVIEWING = "REVIEWING"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    APPROVED = "APPROVED"
    AWAITING_HUMAN_APPROVAL = "AWAITING_HUMAN_APPROVAL"
    HUMAN_APPROVED = "HUMAN_APPROVED"
    BLOCKED = "BLOCKED"


class RunEvent(str, Enum):
    RUN_STARTED = "run_started"
    EXECUTOR_STARTED = "executor_started"
    REVIEW_STARTED = "review_started"
    REVIEW_CHANGES_REQUESTED = "review_changes_requested"
    REVIEW_APPROVED = "review_approved"
    APPROVAL_REQUESTED = "approval_requested"
    HUMAN_APPROVED = "human_approved"
    HUMAN_REJECTED = "human_rejected"
    ITERATION_BUDGET_EXTENDED = "iteration_budget_extended"
    RUN_BLOCKED = "run_blocked"


class StateTransitionError(ValueError):
    """A typed state transition is invalid or conflicts with concurrent state."""


@dataclass(frozen=True)
class TransitionSpec:
    sources: frozenset[RunState | None]
    target: RunState
    idempotent: bool = True


@dataclass(frozen=True)
class TransitionResult:
    event: RunEvent
    previous: RunState | None
    current: RunState
    result: str


# Active nonterminal states plus missing status (None). Missing status is the
# only pre-run_started condition; run_blocked intentionally allows ∅ → BLOCKED
# so record-failure / early interrupt can close a run that never reached
# EXECUTING (startup-failure semantics).
_ACTIVE_NONTERMINAL = frozenset(
    {
        None,
        RunState.EXECUTING,
        RunState.REVIEWING,
        RunState.CHANGES_REQUESTED,
        RunState.APPROVED,
        RunState.AWAITING_HUMAN_APPROVAL,
    }
)

TRANSITIONS: dict[RunEvent, TransitionSpec] = {
    RunEvent.RUN_STARTED: TransitionSpec(
        frozenset({None}), RunState.EXECUTING
    ),
    RunEvent.EXECUTOR_STARTED: TransitionSpec(
        frozenset({RunState.CHANGES_REQUESTED, RunState.BLOCKED}),
        RunState.EXECUTING,
    ),
    RunEvent.REVIEW_STARTED: TransitionSpec(
        frozenset(
            {
                RunState.EXECUTING,
                RunState.CHANGES_REQUESTED,
                RunState.BLOCKED,
                RunState.APPROVED,
            }
        ),
        RunState.REVIEWING,
    ),
    RunEvent.REVIEW_CHANGES_REQUESTED: TransitionSpec(
        frozenset({RunState.REVIEWING}), RunState.CHANGES_REQUESTED
    ),
    RunEvent.REVIEW_APPROVED: TransitionSpec(
        frozenset({RunState.REVIEWING}), RunState.APPROVED
    ),
    RunEvent.APPROVAL_REQUESTED: TransitionSpec(
        frozenset({RunState.APPROVED}), RunState.AWAITING_HUMAN_APPROVAL
    ),
    RunEvent.HUMAN_APPROVED: TransitionSpec(
        frozenset({RunState.AWAITING_HUMAN_APPROVAL}),
        RunState.HUMAN_APPROVED,
    ),
    RunEvent.HUMAN_REJECTED: TransitionSpec(
        frozenset({RunState.AWAITING_HUMAN_APPROVAL}),
        RunState.BLOCKED,
        idempotent=False,
    ),
    RunEvent.ITERATION_BUDGET_EXTENDED: TransitionSpec(
        frozenset({RunState.BLOCKED}), RunState.CHANGES_REQUESTED
    ),
    RunEvent.RUN_BLOCKED: TransitionSpec(
        _ACTIVE_NONTERMINAL, RunState.BLOCKED
    ),
}


def _coerce_event(event: RunEvent | str) -> RunEvent:
    try:
        return event if isinstance(event, RunEvent) else RunEvent(event)
    except ValueError as exc:
        raise StateTransitionError(f"unknown run event: {event!r}") from exc


def _coerce_state(value: RunState | str | None) -> RunState | None:
    if value is None:
        return None
    if value == "":
        raise StateTransitionError("run status is empty")
    try:
        return value if isinstance(value, RunState) else RunState(value)
    except ValueError as exc:
        raise StateTransitionError(f"unknown run status: {value!r}") from exc


def _validate_failure(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _FAILURE_FIELDS:
        raise StateTransitionError("run failure contract is invalid")
    if (
        value.get("schema_version") != 1
        or not isinstance(value.get("reason"), str)
        or not value["reason"]
        or not isinstance(value.get("phase"), str)
        or type(value.get("iteration")) is not int
        or value.get("report") is not None
        and not isinstance(value.get("report"), str)
        or not isinstance(value.get("recorded_at"), str)
    ):
        raise StateTransitionError("run failure field types are invalid")
    return value


def _validate_human_decision(
    run_path: Path,
    value: object,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise StateTransitionError("human decision must be an object")
    if (
        value.get("schema_version") != 1
        or value.get("decision") not in {"approve", "reject"}
        or value.get("run_id") != run_path.name
        or not isinstance(value.get("diff_hash"), str)
        or not value["diff_hash"]
        or type(value.get("telegram_user_id")) is not int
        or value["telegram_user_id"] <= 0
        or type(value.get("telegram_chat_id")) is not int
        or value["telegram_chat_id"] <= 0
        or not isinstance(value.get("decided_at"), str)
    ):
        raise StateTransitionError("human decision contract is invalid")
    return value


def read_state_document(run_dir: Path | str) -> dict[str, object] | None:
    """Read and validate the authoritative state without following symlinks."""
    run_path = Path(run_dir)
    path = run_path / STATE_FILENAME
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StateTransitionError("run state cannot be inspected") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise StateTransitionError("run state must be a regular non-symlink file")
    if before.st_size > _MAX_STATE_BYTES:
        raise StateTransitionError("run state is oversized")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise StateTransitionError("run state changed while opening")
            raw = os.read(fd, _MAX_STATE_BYTES + 1)
        finally:
            os.close(fd)
    except OSError as exc:
        raise StateTransitionError("run state cannot be read safely") from exc
    if len(raw) > _MAX_STATE_BYTES:
        raise StateTransitionError("run state is oversized")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateTransitionError("run state is not valid JSON") from exc
    if not isinstance(document, dict):
        raise StateTransitionError("run state must be a JSON object")
    if document.get("schema_version") != 1:
        raise StateTransitionError("run state schema mismatch")
    if document.get("run_id") != run_path.name:
        raise StateTransitionError("run state binding mismatch")
    current = _coerce_state(document.get("status"))
    metadata = document.get("metadata", {})
    if not isinstance(metadata, dict):
        raise StateTransitionError("run state metadata must be an object")
    if "failure" in document:
        _validate_failure(document["failure"])
    if "iteration_budget" in document and not isinstance(
        document["iteration_budget"], dict
    ):
        raise StateTransitionError("run iteration budget must be an object")
    if "human_decision" in document:
        decision = _validate_human_decision(run_path, document["human_decision"])
        expected = (
            RunState.HUMAN_APPROVED
            if decision["decision"] == "approve"
            else RunState.BLOCKED
        )
        if current != expected:
            raise StateTransitionError(
                "human decision and run status are inconsistent"
            )
    return document


def initialize_run_state(
    run_dir: Path | str,
    metadata: dict[str, object],
) -> dict[str, object]:
    """Bind immutable run metadata while preserving an already-published status."""
    run_path = Path(run_dir)
    with run_scoped_lock(run_path, lock_name=STATE_LOCK_FILENAME):
        existing = read_state_document(run_path)
        if existing is not None and existing.get("metadata"):
            if existing["metadata"] != metadata:
                raise StateTransitionError("run metadata already initialized differently")
            return existing
        document: dict[str, object] = {
            "schema_version": 1,
            "run_id": run_path.name,
            "status": existing.get("status") if existing else None,
            "metadata": metadata,
        }
        atomic_write_json(run_path / STATE_FILENAME, document)
        return document


def read_state(run_dir: Path | str) -> RunState | None:
    document = read_state_document(run_dir)
    return _coerce_state(document.get("status")) if document is not None else None


def read_status(run_dir: Path | str) -> str:
    state = read_state(run_dir)
    return state.value if state is not None else ""


def record_run_failure(
    run_dir: Path | str,
    failure: dict[str, object],
) -> TransitionResult:
    """Publish the first structured failure and BLOCKED status in one write."""
    run_path = Path(run_dir)
    _validate_failure(failure)
    with run_scoped_lock(run_path, lock_name=STATE_LOCK_FILENAME):
        document = read_state_document(run_path)
        current = _coerce_state(document.get("status")) if document else None
        if current == RunState.BLOCKED and document and "failure" in document:
            return TransitionResult(
                event=RunEvent.RUN_BLOCKED,
                previous=current,
                current=current,
                result="already_applied",
            )
        spec = TRANSITIONS[RunEvent.RUN_BLOCKED]
        if current != RunState.BLOCKED and current not in spec.sources:
            raise StateTransitionError(
                f"event {RunEvent.RUN_BLOCKED.value} cannot transition "
                f"{current.value if current else '<empty>'} to {spec.target.value}"
            )
        updated: dict[str, object] = dict(
            document
            or {
                "schema_version": 1,
                "run_id": run_path.name,
                "metadata": {},
            }
        )
        updated["status"] = RunState.BLOCKED.value
        updated["failure"] = failure
        atomic_write_json(run_path / STATE_FILENAME, updated)
        return TransitionResult(
            event=RunEvent.RUN_BLOCKED,
            previous=current,
            current=RunState.BLOCKED,
            result="applied",
        )


def record_iteration_budget_extension(
    run_dir: Path | str,
    budget: dict[str, object],
    *,
    expected_budget: dict[str, object] | None,
    expected_failure: dict[str, object],
) -> TransitionResult:
    """Publish an authorized budget and its status transition in one write."""
    run_path = Path(run_dir)
    with run_scoped_lock(run_path, lock_name=STATE_LOCK_FILENAME):
        document = read_state_document(run_path)
        current = _coerce_state(document.get("status")) if document else None
        if current != RunState.BLOCKED:
            raise StateTransitionError(
                "iteration budget authorization lost BLOCKED state"
            )
        assert document is not None
        if document.get("failure") != expected_failure:
            raise StateTransitionError(
                "iteration budget authorization lost failure binding"
            )
        if document.get("iteration_budget") != expected_budget:
            raise StateTransitionError(
                "iteration budget changed during authorization"
            )
        updated = dict(document)
        updated["iteration_budget"] = budget
        updated["status"] = RunState.CHANGES_REQUESTED.value
        atomic_write_json(run_path / STATE_FILENAME, updated)
        return TransitionResult(
            event=RunEvent.ITERATION_BUDGET_EXTENDED,
            previous=current,
            current=RunState.CHANGES_REQUESTED,
            result="applied",
        )


def record_human_decision(
    run_dir: Path | str,
    decision: dict[str, object],
) -> TransitionResult:
    """Publish an authenticated human decision and terminal status together."""
    run_path = Path(run_dir)
    _validate_human_decision(run_path, decision)
    target = (
        RunState.HUMAN_APPROVED
        if decision["decision"] == "approve"
        else RunState.BLOCKED
    )
    event = (
        RunEvent.HUMAN_APPROVED
        if decision["decision"] == "approve"
        else RunEvent.HUMAN_REJECTED
    )
    with run_scoped_lock(run_path, lock_name=STATE_LOCK_FILENAME):
        document = read_state_document(run_path)
        current = _coerce_state(document.get("status")) if document else None
        if document is None:
            raise StateTransitionError("run state is missing")
        existing = document.get("human_decision")
        if existing is not None:
            if existing == decision and current == target:
                return TransitionResult(
                    event=event,
                    previous=current,
                    current=current,
                    result="already_applied",
                )
            raise StateTransitionError("human decision is already recorded")
        if current != RunState.AWAITING_HUMAN_APPROVAL:
            raise StateTransitionError(
                f"human decision cannot transition "
                f"{current.value if current else '<empty>'} to {target.value}"
            )
        updated = dict(document)
        updated["human_decision"] = decision
        updated["status"] = target.value
        atomic_write_json(run_path / STATE_FILENAME, updated)
        return TransitionResult(
            event=event,
            previous=current,
            current=target,
            result="applied",
        )


def transition_run(
    run_dir: Path | str,
    event: RunEvent | str,
    *,
    expected_states: Iterable[RunState | str | None] | None = None,
    state_lock_held: bool = False,
) -> TransitionResult:
    """Apply a typed transition under ``.state.lock`` with CAS semantics."""
    run_path = Path(run_dir)
    typed_event = _coerce_event(event)
    spec = TRANSITIONS[typed_event]
    expected = (
        frozenset(_coerce_state(value) for value in expected_states)
        if expected_states is not None
        else None
    )
    lock = (
        nullcontext()
        if state_lock_held
        else run_scoped_lock(run_path, lock_name=STATE_LOCK_FILENAME)
    )
    with lock:
        document = read_state_document(run_path)
        current = _coerce_state(document.get("status")) if document else None
        if expected is not None and current not in expected:
            raise StateTransitionError(
                f"event {typed_event.value} expected "
                f"{sorted(state.value if state else '<empty>' for state in expected)}, "
                f"got {current.value if current else '<empty>'}"
            )
        if current == spec.target:
            if not spec.idempotent:
                raise StateTransitionError(
                    f"event {typed_event.value} is not replayable from "
                    f"{spec.target.value}"
                )
            return TransitionResult(
                event=typed_event,
                previous=current,
                current=current,
                result="already_applied",
            )
        if document and document.get("human_decision") is not None:
            raise StateTransitionError(
                "run with a human decision is terminal"
            )
        if current not in spec.sources:
            raise StateTransitionError(
                f"event {typed_event.value} cannot transition "
                f"{current.value if current else '<empty>'} to {spec.target.value}"
            )
        updated: dict[str, object] = dict(
            document
            or {
                "schema_version": 1,
                "run_id": run_path.name,
                "metadata": {},
            }
        )
        updated["status"] = spec.target.value
        atomic_write_json(run_path / STATE_FILENAME, updated)
        return TransitionResult(
            event=typed_event,
            previous=current,
            current=spec.target,
            result="applied",
        )
