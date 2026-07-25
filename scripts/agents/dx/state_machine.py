"""Typed, compare-and-set run status transitions for the local-only workflow."""

from __future__ import annotations

import os
import stat
from contextlib import nullcontext
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable

from .atomic import atomic_write_text, run_scoped_lock


STATUS_FILENAME = "status"
STATE_LOCK_FILENAME = ".state.lock"
_MAX_STATUS_BYTES = 128


class RunState(str, Enum):
    EXECUTING = "EXECUTING"
    REVIEWING = "REVIEWING"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    APPROVED = "APPROVED"
    AWAITING_HUMAN_APPROVAL = "AWAITING_HUMAN_APPROVAL"
    HUMAN_APPROVED = "HUMAN_APPROVED"
    BLOCKED = "BLOCKED"

    # Compatibility only. No event may leave these states.
    DELIVERING = "DELIVERING"
    DELIVERY_FAILED = "DELIVERY_FAILED"
    PUSHED = "PUSHED"


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
    RECOVER_HUMAN_APPROVED = "recover_human_approved"


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
    RunEvent.RECOVER_HUMAN_APPROVED: TransitionSpec(
        frozenset({RunState.AWAITING_HUMAN_APPROVAL}),
        RunState.HUMAN_APPROVED,
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


def read_state(run_dir: Path | str) -> RunState | None:
    """Read a short regular status file without following symlinks."""
    path = Path(run_dir) / STATUS_FILENAME
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StateTransitionError("run status cannot be inspected") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise StateTransitionError("run status must be a regular non-symlink file")
    if before.st_size > _MAX_STATUS_BYTES:
        raise StateTransitionError("run status is oversized")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise StateTransitionError("run status changed while opening")
            raw = os.read(fd, _MAX_STATUS_BYTES + 1)
        finally:
            os.close(fd)
    except OSError as exc:
        raise StateTransitionError("run status cannot be read safely") from exc
    if len(raw) > _MAX_STATUS_BYTES:
        raise StateTransitionError("run status is oversized")
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise StateTransitionError("run status is not UTF-8") from exc
    return _coerce_state(value)


def read_status(run_dir: Path | str) -> str:
    state = read_state(run_dir)
    return state.value if state is not None else ""


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
        current = read_state(run_path)
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
        if current not in spec.sources:
            raise StateTransitionError(
                f"event {typed_event.value} cannot transition "
                f"{current.value if current else '<empty>'} to {spec.target.value}"
            )
        atomic_write_text(run_path / STATUS_FILENAME, spec.target.value)
        return TransitionResult(
            event=typed_event,
            previous=current,
            current=spec.target,
            result="applied",
        )


def is_legacy_terminal(state: RunState | str | None) -> bool:
    typed = _coerce_state(state)
    return typed in {
        RunState.DELIVERING,
        RunState.DELIVERY_FAILED,
        RunState.PUSHED,
    }
