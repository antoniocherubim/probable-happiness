"""DX-07 typed run-state machine and writer-centralization regressions."""

from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS = REPO_ROOT / "scripts" / "agents"
sys.path.insert(0, str(AGENTS))

from dx.atomic import atomic_write_text  # noqa: E402
from dx.state_machine import (  # noqa: E402
    TRANSITIONS,
    RunEvent,
    RunState,
    StateTransitionError,
    read_state,
    transition_run,
)


ALL_STATES = (None, *RunState)
ALL_EVENT_STATE_PAIRS = tuple(
    (event, state) for event in RunEvent for state in ALL_STATES
)


def set_state_fixture(run_dir: Path, state: RunState | None) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    status_path = run_dir / "status"
    if state is None:
        status_path.unlink(missing_ok=True)
    else:
        atomic_write_text(status_path, state.value)


@pytest.mark.parametrize(("event", "initial"), ALL_EVENT_STATE_PAIRS)
def test_complete_transition_matrix(
    tmp_path: Path,
    event: RunEvent,
    initial: RunState | None,
) -> None:
    run_dir = tmp_path / f"{event.value}-{initial.value if initial else 'empty'}"
    set_state_fixture(run_dir, initial)
    before = (run_dir / "status").read_bytes() if initial is not None else None
    spec = TRANSITIONS[event]
    allowed = initial in spec.sources or (
        initial == spec.target and spec.idempotent
    )

    if allowed:
        result = transition_run(run_dir, event)
        assert result.current == spec.target
        assert read_state(run_dir) == spec.target
        assert result.result == (
            "already_applied" if initial == spec.target else "applied"
        )
    else:
        with pytest.raises(StateTransitionError, match=f"event {event.value}"):
            transition_run(run_dir, event)
        assert read_state(run_dir) == initial
        if before is not None:
            assert (run_dir / "status").read_bytes() == before


def test_idempotent_replay_does_not_rewrite_status(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    first = transition_run(run_dir, RunEvent.RUN_STARTED)
    status_path = run_dir / "status"
    before = status_path.stat()
    content = status_path.read_bytes()

    replay = transition_run(run_dir, RunEvent.RUN_STARTED)
    after = status_path.stat()

    assert first.result == "applied"
    assert replay.result == "already_applied"
    assert after.st_ino == before.st_ino
    assert after.st_mtime_ns == before.st_mtime_ns
    assert status_path.read_bytes() == content


def test_expected_state_conflict_is_compare_and_set_failure(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    transition_run(run_dir, RunEvent.RUN_STARTED)
    before = (run_dir / "status").read_bytes()

    with pytest.raises(StateTransitionError, match="expected"):
        transition_run(
            run_dir,
            RunEvent.REVIEW_STARTED,
            expected_states={RunState.CHANGES_REQUESTED},
        )

    assert read_state(run_dir) == RunState.EXECUTING
    assert (run_dir / "status").read_bytes() == before


def test_concurrent_conflicting_events_have_exactly_one_winner(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    set_state_fixture(run_dir, RunState.REVIEWING)
    barrier = Barrier(2)

    def attempt(event: RunEvent) -> str:
        barrier.wait()
        try:
            return transition_run(run_dir, event).result
        except StateTransitionError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                attempt,
                (
                    RunEvent.REVIEW_APPROVED,
                    RunEvent.REVIEW_CHANGES_REQUESTED,
                ),
            )
        )

    assert sorted(outcomes) == ["applied", "conflict"]
    assert read_state(run_dir) in {
        RunState.APPROVED,
        RunState.CHANGES_REQUESTED,
    }


@pytest.mark.parametrize(
    "legacy",
    (RunState.DELIVERING, RunState.DELIVERY_FAILED, RunState.PUSHED),
)
@pytest.mark.parametrize("event", tuple(RunEvent))
def test_legacy_states_are_terminal(
    tmp_path: Path,
    legacy: RunState,
    event: RunEvent,
) -> None:
    run_dir = tmp_path / f"{legacy.value}-{event.value}"
    set_state_fixture(run_dir, legacy)

    with pytest.raises(StateTransitionError):
        transition_run(run_dir, event)

    assert read_state(run_dir) == legacy


def test_status_reader_rejects_symlink_and_oversized_file(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("EXECUTING\n", encoding="utf-8")
    symlink_run = tmp_path / "symlink-run"
    symlink_run.mkdir()
    (symlink_run / "status").symlink_to(target)
    with pytest.raises(StateTransitionError, match="non-symlink"):
        read_state(symlink_run)

    oversized_run = tmp_path / "oversized-run"
    oversized_run.mkdir()
    (oversized_run / "status").write_text("X" * 129, encoding="utf-8")
    with pytest.raises(StateTransitionError, match="oversized"):
        read_state(oversized_run)


@pytest.mark.parametrize("content", ("", "UNKNOWN\n", b"\xff"))
def test_status_reader_rejects_empty_unknown_and_non_utf8(
    tmp_path: Path,
    content: str | bytes,
) -> None:
    run_dir = tmp_path / "invalid-run"
    run_dir.mkdir()
    status_path = run_dir / "status"
    if isinstance(content, bytes):
        status_path.write_bytes(content)
    else:
        status_path.write_text(content, encoding="utf-8")

    with pytest.raises(StateTransitionError):
        read_state(run_dir)


def test_production_has_only_one_status_writer() -> None:
    state_machine = AGENTS / "dx" / "state_machine.py"
    other_python = [
        path
        for path in (AGENTS / "dx").glob("*.py")
        if path != state_machine
    ]
    for path in other_python:
        source = path.read_text(encoding="utf-8")
        assert "write_status" not in source, path
        assert 'atomic_write_text(run_path / STATUS_FILENAME' not in source, path
        assert ' / "status").write_' not in source, path

    shell = (AGENTS / "run_task.sh").read_text(encoding="utf-8")
    assert "write_run_status" not in shell
    assert '"$RUN_DIR/status"' not in "\n".join(
        line for line in shell.splitlines() if ">" in line or "mv " in line
    )

    cli = (AGENTS / "dx" / "cli.py").read_text(encoding="utf-8")
    assert 'add_parser("set-status"' not in cli
    assert 'add_parser("transition-state"' in cli


def test_internal_cli_exposes_only_runner_events(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    script = AGENTS / "telegram_bridge.py"

    started = subprocess.run(
        [
            sys.executable,
            str(script),
            "transition-state",
            "--run-dir",
            str(run_dir),
            "--event",
            RunEvent.RUN_STARTED.value,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    forbidden = subprocess.run(
        [
            sys.executable,
            str(script),
            "transition-state",
            "--run-dir",
            str(run_dir),
            "--event",
            RunEvent.HUMAN_APPROVED.value,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert started.returncode == 0
    assert json.loads(started.stdout)["current"] == RunState.EXECUTING.value
    assert forbidden.returncode == 2
    assert read_state(run_dir) == RunState.EXECUTING


def test_record_failure_blocks_once_and_preserves_first_reason(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    script = AGENTS / "telegram_bridge.py"
    transition_run(run_dir, RunEvent.RUN_STARTED)

    def record(reason: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(script),
                "record-failure",
                "--run-dir",
                str(run_dir),
                "--reason",
                reason,
                "--phase",
                "executor",
                "--iteration",
                "1",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    assert record("first_failure").returncode == 0
    first = json.loads((run_dir / "failure.json").read_text(encoding="utf-8"))
    assert record("replayed_failure").returncode == 0

    assert read_state(run_dir) == RunState.BLOCKED
    assert json.loads(
        (run_dir / "failure.json").read_text(encoding="utf-8")
    ) == first
