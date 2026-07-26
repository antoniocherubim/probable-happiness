"""Personal Core v2 authoritative state regressions."""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS = REPO_ROOT / "scripts" / "agents"
sys.path.insert(0, str(AGENTS))

from dx.atomic import atomic_write_json  # noqa: E402
from dx.state_machine import (  # noqa: E402
    STATE_FILENAME,
    TRANSITIONS,
    RunEvent,
    RunState,
    StateTransitionError,
    initialize_run_state,
    read_state,
    read_state_document,
    record_run_failure,
    transition_run,
)


def set_state_fixture(run_dir: Path, state: RunState | None) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    if state is not None:
        atomic_write_json(
            run_dir / STATE_FILENAME,
            {
                "schema_version": 1,
                "run_id": run_dir.name,
                "status": state.value,
                "metadata": {},
            },
        )


@pytest.mark.parametrize(
    ("event", "initial"),
    tuple((event, state) for event in RunEvent for state in (None, *RunState)),
)
def test_complete_transition_matrix(
    tmp_path: Path,
    event: RunEvent,
    initial: RunState | None,
) -> None:
    run_dir = tmp_path / f"{event.value}-{initial.value if initial else 'empty'}"
    set_state_fixture(run_dir, initial)
    before = (run_dir / STATE_FILENAME).read_bytes() if initial is not None else None
    spec = TRANSITIONS[event]
    allowed = initial in spec.sources or (initial == spec.target and spec.idempotent)

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
            assert (run_dir / STATE_FILENAME).read_bytes() == before


def test_transition_preserves_metadata_and_idempotent_replay_does_not_rewrite(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    transition_run(run_dir, RunEvent.RUN_STARTED)
    metadata = {"repo": "/repo", "base_commit": "abc"}
    initialize_run_state(run_dir, metadata)
    path = run_dir / STATE_FILENAME
    before = path.stat()
    content = path.read_bytes()

    replay = transition_run(run_dir, RunEvent.RUN_STARTED)

    assert replay.result == "already_applied"
    assert path.stat().st_ino == before.st_ino
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert path.read_bytes() == content
    assert read_state_document(run_dir)["metadata"] == metadata  # type: ignore[index]


def test_metadata_initialization_refuses_different_replay(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    initialize_run_state(run_dir, {"repo": "/one"})
    with pytest.raises(StateTransitionError, match="initialized differently"):
        initialize_run_state(run_dir, {"repo": "/two"})


def test_failure_and_blocked_status_are_published_together_once(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    metadata = {"repo": "/repo"}
    initialize_run_state(run_dir, metadata)
    transition_run(run_dir, RunEvent.RUN_STARTED)
    first = {
        "schema_version": 1,
        "reason": "executor_timeout",
        "phase": "executor",
        "iteration": 2,
        "report": "cursor-2.json",
        "recorded_at": "2026-07-25T00:00:00Z",
    }

    result = record_run_failure(run_dir, first)
    replay = record_run_failure(run_dir, {**first, "reason": "replacement"})
    state = read_state_document(run_dir)

    assert result.result == "applied"
    assert replay.result == "already_applied"
    assert state["status"] == RunState.BLOCKED.value
    assert state["metadata"] == metadata
    assert state["failure"] == first
    assert not (run_dir / "failure.json").exists()


def test_expected_state_conflict_is_compare_and_set_failure(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    transition_run(run_dir, RunEvent.RUN_STARTED)
    before = (run_dir / STATE_FILENAME).read_bytes()
    with pytest.raises(StateTransitionError, match="expected"):
        transition_run(
            run_dir,
            RunEvent.REVIEW_STARTED,
            expected_states={RunState.CHANGES_REQUESTED},
        )
    assert (run_dir / STATE_FILENAME).read_bytes() == before


def test_concurrent_conflicting_events_have_one_winner(tmp_path: Path) -> None:
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
                (RunEvent.REVIEW_APPROVED, RunEvent.REVIEW_CHANGES_REQUESTED),
            )
        )
    assert sorted(outcomes) == ["applied", "conflict"]


def test_state_reader_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("{}\n", encoding="utf-8")
    symlink_run = tmp_path / "symlink-run"
    symlink_run.mkdir()
    (symlink_run / STATE_FILENAME).symlink_to(target)
    with pytest.raises(StateTransitionError, match="non-symlink"):
        read_state(symlink_run)

    cases = (
        lambda run_id: b"x" * (1024 * 1024 + 1),
        lambda run_id: b"not json",
        lambda run_id: json.dumps(
            {"schema_version": 1, "run_id": "wrong", "status": "EXECUTING"}
        ).encode(),
        lambda run_id: json.dumps(
            {"schema_version": 1, "run_id": run_id, "status": "UNKNOWN"}
        ).encode(),
        lambda run_id: json.dumps(
            {
                "schema_version": 1,
                "run_id": run_id,
                "status": "BLOCKED",
                "failure": {"reason": "incomplete"},
            }
        ).encode(),
    )
    for index, build_content in enumerate(cases):
        run_dir = tmp_path / f"invalid-{index}"
        run_dir.mkdir()
        (run_dir / STATE_FILENAME).write_bytes(build_content(run_dir.name))
        with pytest.raises(StateTransitionError):
            read_state(run_dir)


def test_run_blocked_from_every_documented_source(tmp_path: Path) -> None:
    sources = TRANSITIONS[RunEvent.RUN_BLOCKED].sources
    for source in sources:
        run_dir = tmp_path / f"block-{source.value if source else 'empty'}"
        set_state_fixture(run_dir, source)
        result = transition_run(run_dir, RunEvent.RUN_BLOCKED)
        assert result.previous == source
        assert read_state(run_dir) == RunState.BLOCKED


def test_production_has_no_legacy_status_or_run_metadata_paths() -> None:
    for path in AGENTS.rglob("*"):
        if path.suffix not in {".py", ".sh"}:
            continue
        source = path.read_text(encoding="utf-8")
        assert ' / "status"' not in source, path
        assert '$RUN_DIR/status' not in source, path
        assert '"run.json"' not in source, path
        assert '"failure.json"' not in source, path
