"""DX-08A — critical event authority, lock refusal, durable mode, secure I/O."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS = REPO_ROOT / "scripts" / "agents"
sys.path.insert(0, str(AGENTS))

from dx.atomic import run_scoped_lock  # noqa: E402
from dx.persist import (  # noqa: E402
    PersistError,
    secure_acquire_lock_fd,
    secure_read_json,
    secure_write_json,
)
from dx.state_machine import (  # noqa: E402
    RunEvent,
    StateTransitionError,
    read_status,
    transition_run,
)
from dx.txn import (  # noqa: E402
    CRITICAL_BINDINGS,
    LogicalTransaction,
    TransactionError,
    commit_status_with_audit_locked,
)


def _private_run(tmp_path: Path, name: str = "run-1") -> Path:
    run_dir = tmp_path / name
    run_dir.mkdir(mode=0o700)
    return run_dir


def _cursor_agent_result(result: str = "ok", **overrides: object) -> dict:
    """Production Cursor Agent ``--output-format json`` result envelope."""
    payload: dict = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 1,
        "duration_api_ms": 1,
        "result": result,
        "session_id": "00000000-0000-0000-0000-000000000001",
        "request_id": "00000000-0000-0000-0000-000000000002",
        "usage": {
            "inputTokens": 0,
            "outputTokens": 0,
            "cacheReadTokens": 0,
            "cacheWriteTokens": 0,
        },
    }
    payload.update(overrides)
    return payload


def _snapshot(run_dir: Path) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes()
        for path in sorted(run_dir.iterdir())
        if path.is_file()
    }


def _failure_payload() -> dict:
    return {
        "schema_version": 1,
        "reason": "boom",
        "phase": "loop",
        "iteration": 1,
        "report": None,
        "recorded_at": "2026-07-25T00:00:00Z",
    }


def _approval_request_payload(run_dir: Path, *, token_consumed: bool = False) -> dict:
    return {
        "schema_version": 1,
        "technical_status": "APPROVED",
        "task": "docs/tasks/DX-08A.md",
        "task_id": "DX-08A",
        "run_id": run_dir.name,
        "base_commit": "abc",
        "worktree": "/tmp/wt",
        "review_report": "review.json",
        "diff_hash": "a" * 64,
        "callback_token": "b" * 32,
        "token_consumed": token_consumed,
        "created_at": "2026-07-25T00:00:00Z",
    }


def _iteration_budget_payload(run_dir: Path) -> dict:
    """Build a contract-valid iteration-budget artifact with review bindings."""
    import hashlib
    import json

    from dx.runstate import MAX_REVIEW_REASON, _extension_id

    iteration = 3
    additional = 2
    original = 3
    effective = 5
    review = {
        "status": "CHANGES_REQUESTED",
        "summary": "need more iterations",
        "findings": [],
        "tests_required": ["pytest"],
    }
    review_text = json.dumps(review, indent=2, sort_keys=True) + "\n"
    review_path = run_dir / f"review-{iteration}.json"
    review_path.write_text(review_text, encoding="utf-8")
    os.chmod(review_path, 0o600)
    review_sha = hashlib.sha256(review_text.encode("utf-8")).hexdigest()
    diff_hash = "c" * 64
    secure_write_json(
        run_dir / f"review-{iteration}-snapshot.json",
        {"schema_version": 1, "iteration": iteration, "diff_hash": diff_hash},
    )
    entry = {
        "idempotency_id": "x" * 64,
        "additional_iterations": additional,
        "previous_limit": original,
        "effective_limit": effective,
        "origin": "cli",
        "authorized_at": "2026-07-25T00:00:00Z",
        "authorized_at_iteration": iteration,
        "review_file": f"review-{iteration}.json",
        "review_sha256": review_sha,
        "reviewed_diff_hash": diff_hash,
        "blocked_reason": MAX_REVIEW_REASON,
    }
    entry["idempotency_id"] = _extension_id(run_dir.name, entry)
    return {
        "schema_version": 1,
        "run_id": run_dir.name,
        "original_limit": original,
        "effective_limit": effective,
        "extensions": [entry],
        "updated_at": "2026-07-25T00:00:00Z",
    }


def _artifact_for(event: str, run_dir: Path) -> dict:
    required = next(iter(CRITICAL_BINDINGS[event]))
    if required == "failure.json":
        return {required: _failure_payload()}
    if required == "human_approval_request.json":
        return {required: _approval_request_payload(run_dir)}
    if required == "human_approval_decision.json":
        return {
            "human_approval_request.json": _approval_request_payload(
                run_dir, token_consumed=True
            ),
            required: {
                "schema_version": 1,
                "decision": "approve",
                "run_id": run_dir.name,
                "diff_hash": "a" * 64,
                "callback_token": "b" * 32,
                "telegram_user_id": 1,
                "telegram_chat_id": 1,
                "decided_at": "2026-07-25T00:00:00Z",
            },
        }
    if required == "human_rejection.json":
        return {
            "human_approval_request.json": _approval_request_payload(
                run_dir, token_consumed=True
            ),
            required: {
                "schema_version": 1,
                "decision": "reject",
                "run_id": run_dir.name,
                "diff_hash": "a" * 64,
                "telegram_user_id": 1,
                "telegram_chat_id": 1,
                "decided_at": "2026-07-25T00:00:00Z",
                "reason": "no",
            },
        }
    if required == "iteration-budget.json":
        return {required: _iteration_budget_payload(run_dir)}
    raise AssertionError(required)


def _arm_for_critical(run_dir: Path, event: str) -> None:
    """Place status in a legal source state for the critical event."""
    if event == RunEvent.RUN_BLOCKED.value:
        transition_run(run_dir, RunEvent.RUN_STARTED)
        return
    if event == RunEvent.APPROVAL_REQUESTED.value:
        transition_run(run_dir, RunEvent.RUN_STARTED)
        transition_run(run_dir, RunEvent.REVIEW_STARTED)
        transition_run(run_dir, RunEvent.REVIEW_APPROVED)
        return
    if event in {
        RunEvent.HUMAN_APPROVED.value,
        RunEvent.HUMAN_REJECTED.value,
        RunEvent.RECOVER_HUMAN_APPROVED.value,
    }:
        transition_run(run_dir, RunEvent.RUN_STARTED)
        transition_run(run_dir, RunEvent.REVIEW_STARTED)
        transition_run(run_dir, RunEvent.REVIEW_APPROVED)
        (run_dir / "status").write_text("AWAITING_HUMAN_APPROVAL\n", encoding="utf-8")
        os.chmod(run_dir / "status", 0o600)
        return
    if event == RunEvent.ITERATION_BUDGET_EXTENDED.value:
        transition_run(run_dir, RunEvent.RUN_STARTED)
        txn = LogicalTransaction(
            run_dir=run_dir,
            event=RunEvent.RUN_BLOCKED.value,
            status_event=RunEvent.RUN_BLOCKED,
        )
        txn.add_json("failure.json", _failure_payload())
        txn.commit()
        return
    raise AssertionError(event)


@pytest.mark.parametrize("event", sorted(CRITICAL_BINDINGS))
def test_artifactless_critical_events_refuse_before_mutation(
    tmp_path: Path, event: str
) -> None:
    run_dir = _private_run(tmp_path, f"refuse-{event}")
    _arm_for_critical(run_dir, event)
    before = _snapshot(run_dir)
    names_before = set(before)

    with pytest.raises(StateTransitionError, match="bound artifacts"):
        transition_run(run_dir, RunEvent(event))

    assert set(_snapshot(run_dir)) == names_before
    assert _snapshot(run_dir) == before
    assert not (run_dir / ".txn.json").exists()


@pytest.mark.parametrize("event", sorted(CRITICAL_BINDINGS))
def test_commit_status_with_audit_refuses_critical(
    tmp_path: Path, event: str
) -> None:
    run_dir = _private_run(tmp_path, f"audit-{event}")
    _arm_for_critical(run_dir, event)
    before = _snapshot(run_dir)
    with pytest.raises(TransactionError, match="bound artifacts"):
        commit_status_with_audit_locked(run_dir, event=RunEvent(event))
    assert _snapshot(run_dir) == before


@pytest.mark.parametrize("event", sorted(CRITICAL_BINDINGS))
def test_critical_transaction_with_artifact_succeeds(
    tmp_path: Path, event: str
) -> None:
    run_dir = _private_run(tmp_path, f"ok-{event}")
    _arm_for_critical(run_dir, event)
    artifacts = _artifact_for(event, run_dir)
    txn = LogicalTransaction(
        run_dir=run_dir,
        event=event,
        status_event=RunEvent(event),
        origin="runner",
    )
    for name, payload in artifacts.items():
        txn.add_json(name, payload)
    outcome = txn.commit()
    assert outcome["result"] in {"committed", "already_applied"}
    required = next(iter(CRITICAL_BINDINGS[event]))
    assert secure_read_json(run_dir / required) == artifacts[required]


def test_idempotent_replay_without_artifact_binding_refused(tmp_path: Path) -> None:
    run_dir = _private_run(tmp_path)
    transition_run(run_dir, RunEvent.RUN_STARTED)
    txn = LogicalTransaction(
        run_dir=run_dir,
        event=RunEvent.RUN_BLOCKED.value,
        status_event=RunEvent.RUN_BLOCKED,
    )
    txn.add_json("failure.json", _failure_payload())
    txn.commit()
    assert read_status(run_dir) == "BLOCKED"
    before = _snapshot(run_dir)
    with pytest.raises(StateTransitionError, match="bound artifacts"):
        transition_run(run_dir, RunEvent.RUN_BLOCKED)
    assert _snapshot(run_dir) == before


def test_transaction_missing_wrong_or_symlink_artifact_refused(tmp_path: Path) -> None:
    run_dir = _private_run(tmp_path)
    transition_run(run_dir, RunEvent.RUN_STARTED)
    before = _snapshot(run_dir)

    missing = LogicalTransaction(
        run_dir=run_dir,
        event=RunEvent.RUN_BLOCKED.value,
        status_event=RunEvent.RUN_BLOCKED,
    )
    with pytest.raises(TransactionError, match="missing artifacts"):
        missing.commit()
    assert _snapshot(run_dir) == before

    wrong = LogicalTransaction(
        run_dir=run_dir,
        event=RunEvent.RUN_BLOCKED.value,
        status_event=RunEvent.RUN_BLOCKED,
    )
    wrong.add_json("human_approval_request.json", {"schema_version": 1})
    with pytest.raises(TransactionError, match="missing artifacts"):
        wrong.commit()
    assert _snapshot(run_dir) == before

    decoy = run_dir / "decoy.json"
    secure_write_json(decoy, _failure_payload())
    link = run_dir / "failure.json"
    link.symlink_to(decoy)
    before_link = _snapshot(run_dir)
    txn = LogicalTransaction(
        run_dir=run_dir,
        event=RunEvent.RUN_BLOCKED.value,
        status_event=RunEvent.RUN_BLOCKED,
    )
    txn.add_json("failure.json", _failure_payload())
    with pytest.raises((TransactionError, PersistError)):
        txn.commit()
    assert link.is_symlink()
    assert _snapshot(run_dir) == before_link


def test_insecure_lock_refused_without_mutation(tmp_path: Path) -> None:
    run_dir = _private_run(tmp_path)
    lock_path = run_dir / ".state.lock"
    lock_path.write_text("x", encoding="utf-8")
    os.chmod(lock_path, 0o644)
    before = lock_path.read_bytes()
    before_mode = stat.S_IMODE(lock_path.stat().st_mode)
    before_ino = lock_path.stat().st_ino

    with pytest.raises((ValueError, PersistError), match="insecure lock mode"):
        with run_scoped_lock(run_dir, lock_name=".state.lock"):
            pass

    assert lock_path.read_bytes() == before
    assert stat.S_IMODE(lock_path.stat().st_mode) == before_mode
    assert lock_path.stat().st_ino == before_ino


def test_lock_symlink_hardlink_and_new_lock_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _private_run(tmp_path)

    target = run_dir / "real.lock"
    target.write_text("x", encoding="utf-8")
    os.chmod(target, 0o600)
    link = run_dir / ".approval.lock"
    link.symlink_to(target)
    with pytest.raises((ValueError, PersistError), match="symlink"):
        with run_scoped_lock(run_dir, lock_name=".approval.lock"):
            pass
    assert link.is_symlink()

    hard_dir = _private_run(tmp_path, "hard")
    primary = hard_dir / ".state.lock"
    primary.write_text("x", encoding="utf-8")
    os.chmod(primary, 0o600)
    alias = hard_dir / "alias.lock"
    os.link(primary, alias)
    with pytest.raises((ValueError, PersistError), match="hard link"):
        with run_scoped_lock(hard_dir, lock_name=".state.lock"):
            pass
    assert primary.read_bytes() == b"x"
    assert stat.S_IMODE(primary.stat().st_mode) == 0o600

    synced: list[str] = []
    real = secure_acquire_lock_fd.__module__
    import dx.persist as persist_mod

    original = persist_mod.fsync_directory

    def tracking(path: Path | str) -> None:
        synced.append(str(path))
        original(path)

    monkeypatch.setattr(persist_mod, "fsync_directory", tracking)
    fresh = _private_run(tmp_path, "fresh-lock")
    with run_scoped_lock(fresh, lock_name=".state.lock"):
        pass
    lock = fresh / ".state.lock"
    assert lock.is_file()
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    assert any(str(fresh) in item for item in synced)
    del real


def test_fault_injection_chmod_second_fsync_replace_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dx.persist as persist_mod

    run_dir = _private_run(tmp_path)

    path_chmod = run_dir / "chmod.json"
    real_fchmod = persist_mod.os.fchmod

    def boom_fchmod(fd: int, mode: int) -> None:
        raise OSError("injected chmod failure")

    monkeypatch.setattr(persist_mod.os, "fchmod", boom_fchmod)
    with pytest.raises(OSError, match="injected chmod"):
        secure_write_json(path_chmod, {"a": 1})
    assert not path_chmod.exists()
    monkeypatch.setattr(persist_mod.os, "fchmod", real_fchmod)

    path_second = run_dir / "second-fsync.json"
    calls = {"n": 0}
    real_fsync = persist_mod.os.fsync

    def counting_fsync(fd: int) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("injected second fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(persist_mod.os, "fsync", counting_fsync)
    with pytest.raises(OSError, match="second fsync"):
        secure_write_json(path_second, {"a": 1})
    assert not path_second.exists()
    monkeypatch.setattr(persist_mod.os, "fsync", real_fsync)

    path_replace = run_dir / "replace.json"
    real_replace = persist_mod.os.replace

    def boom_replace(src: str, dst: str) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(persist_mod.os, "replace", boom_replace)
    with pytest.raises(OSError, match="injected replace"):
        secure_write_json(path_replace, {"a": 1})
    assert not path_replace.exists()
    monkeypatch.setattr(persist_mod.os, "replace", real_replace)

    path_link = run_dir / "link.json"
    real_link = persist_mod.os.link

    def boom_link(src: str, dst: str) -> None:
        raise OSError("injected link failure")

    monkeypatch.setattr(persist_mod.os, "link", boom_link)
    from dx.persist import secure_exclusive_write_json

    with pytest.raises(OSError, match="injected link"):
        secure_exclusive_write_json(path_link, {"a": 1})
    assert not path_link.exists()
    monkeypatch.setattr(persist_mod.os, "link", real_link)

    path_dir = run_dir / "dirsync.json"
    real_dir = persist_mod.fsync_directory

    def boom_dir(path: Path | str) -> None:
        raise PersistError(f"directory fsync unsupported or failed for {path}")

    monkeypatch.setattr(persist_mod, "fsync_directory", boom_dir)
    with pytest.raises(PersistError, match="directory fsync"):
        secure_write_json(path_dir, {"a": 1})
    monkeypatch.setattr(persist_mod, "fsync_directory", real_dir)


def test_corrupted_iteration_never_defaults_to_one(tmp_path: Path) -> None:
    from dx.runstate import IterationBudgetError, _iteration_cursor

    run_dir = _private_run(tmp_path)
    iteration = run_dir / "iteration"
    iteration.write_text("not-a-number\n", encoding="utf-8")
    os.chmod(iteration, 0o600)
    with pytest.raises(IterationBudgetError, match="iteration"):
        _iteration_cursor(run_dir)

    iteration.write_text("2\n", encoding="utf-8")
    os.chmod(iteration, 0o644)
    with pytest.raises(IterationBudgetError, match="iteration|insecure"):
        _iteration_cursor(run_dir)

    iteration.unlink()
    decoy = run_dir / "decoy-iter"
    decoy.write_text("9\n", encoding="utf-8")
    os.chmod(decoy, 0o600)
    iteration.symlink_to(decoy)
    with pytest.raises(IterationBudgetError, match="iteration|symlink"):
        _iteration_cursor(run_dir)

    # Missing cursor remains an explicit error for authorize paths; plan_resume
    # may default only when the file is absent, never on corruption.
    iteration.unlink(missing_ok=True)
    decoy.unlink(missing_ok=True)
    with pytest.raises(IterationBudgetError, match="missing or invalid"):
        _iteration_cursor(run_dir)


def test_insecure_status_and_summary_fail_closed(tmp_path: Path) -> None:
    from dx.cli import main as cli_main
    from dx.snapshot import SUMMARY_FILENAME
    from dx.state_machine import read_state

    run_dir = _private_run(tmp_path)
    status = run_dir / "status"
    status.write_text("EXECUTING\n", encoding="utf-8")
    os.chmod(status, 0o644)
    with pytest.raises(StateTransitionError, match="cannot be read safely|insecure"):
        read_state(run_dir)

    summary = run_dir / SUMMARY_FILENAME
    secure_write_json(summary, {"telegram_messages": ["hi"]})
    os.chmod(summary, 0o644)
    assert (
        cli_main(
            [
                "create-request",
                "--run-dir",
                str(run_dir),
                "--task",
                "docs/tasks/DX-08A.md",
                "--base-commit",
                "abc",
                "--worktree",
                str(tmp_path),
                "--review-report",
                "review.json",
            ]
        )
        == 1
    )


def test_spoofed_artifacts_bound_kwarg_rejected(tmp_path: Path) -> None:
    """Public transition_run has no caller-controlled authority bypass."""
    import inspect

    run_dir = _private_run(tmp_path)
    transition_run(run_dir, RunEvent.RUN_STARTED)
    before = _snapshot(run_dir)
    assert "artifacts_bound" not in inspect.signature(transition_run).parameters
    with pytest.raises(TypeError):
        transition_run(run_dir, RunEvent.RUN_BLOCKED, artifacts_bound=True)  # type: ignore[call-arg]
    with pytest.raises(StateTransitionError, match="bound artifacts"):
        transition_run(run_dir, RunEvent.RUN_BLOCKED)
    assert read_status(run_dir) == "EXECUTING"
    assert _snapshot(run_dir) == before
    assert not (run_dir / "failure.json").exists()


def test_mismatched_event_status_event_and_empty_artifact_refused(
    tmp_path: Path,
) -> None:
    run_dir = _private_run(tmp_path)
    transition_run(run_dir, RunEvent.RUN_STARTED)
    transition_run(run_dir, RunEvent.REVIEW_STARTED)
    transition_run(run_dir, RunEvent.REVIEW_APPROVED)
    (run_dir / "status").write_text("AWAITING_HUMAN_APPROVAL\n", encoding="utf-8")
    os.chmod(run_dir / "status", 0o600)
    before = _snapshot(run_dir)

    mismatched = LogicalTransaction(
        run_dir=run_dir,
        event=RunEvent.RUN_BLOCKED.value,
        status_event=RunEvent.HUMAN_APPROVED,
    )
    mismatched.add_json("failure.json", {})
    with pytest.raises(TransactionError, match="does not match status_event|malformed"):
        mismatched.commit()
    assert read_status(run_dir) == "AWAITING_HUMAN_APPROVAL"
    assert not (run_dir / "human_approval_decision.json").exists()
    assert not (run_dir / "failure.json").exists()
    assert _snapshot(run_dir) == before

    empty = LogicalTransaction(
        run_dir=run_dir,
        event=RunEvent.RUN_BLOCKED.value,
        status_event=RunEvent.RUN_BLOCKED,
    )
    empty.add_json("failure.json", {})
    with pytest.raises(TransactionError, match="malformed required artifact"):
        empty.commit()
    assert read_status(run_dir) == "AWAITING_HUMAN_APPROVAL"
    assert _snapshot(run_dir) == before

    partial = LogicalTransaction(
        run_dir=run_dir,
        event=RunEvent.RUN_BLOCKED.value,
        status_event=RunEvent.RUN_BLOCKED,
    )
    partial.add_json("failure.json", {"schema_version": 1})
    with pytest.raises(TransactionError, match="malformed required artifact"):
        partial.commit()
    assert _snapshot(run_dir) == before


def test_lock_wrong_owner_directory_and_inode_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dx.persist as persist_mod

    run_dir = _private_run(tmp_path)
    lock_path = run_dir / ".state.lock"
    lock_path.write_text("x", encoding="utf-8")
    os.chmod(lock_path, 0o600)
    before = lock_path.read_bytes()
    before_mode = stat.S_IMODE(lock_path.stat().st_mode)
    before_ino = lock_path.stat().st_ino

    with pytest.raises(PersistError, match="unexpected owner"):
        secure_acquire_lock_fd(run_dir, ".state.lock", expected_owner=os.geteuid() + 1)
    assert lock_path.read_bytes() == before
    assert stat.S_IMODE(lock_path.stat().st_mode) == before_mode
    assert lock_path.stat().st_ino == before_ino

    wide_dir = tmp_path / "wide-run"
    wide_dir.mkdir(mode=0o755)
    os.chmod(wide_dir, 0o755)
    with pytest.raises(PersistError, match="insecure mode"):
        secure_acquire_lock_fd(wide_dir, ".state.lock")
    assert not (wide_dir / ".state.lock").exists()

    foreign = _private_run(tmp_path, "foreign-owner-dir")
    with pytest.raises(PersistError, match="unexpected owner"):
        secure_acquire_lock_fd(foreign, ".state.lock", expected_owner=os.geteuid() + 2)
    assert not (foreign / ".state.lock").exists()

    swap_dir = _private_run(tmp_path, "inode-swap")
    swap_lock = swap_dir / ".state.lock"
    swap_lock.write_text("y", encoding="utf-8")
    os.chmod(swap_lock, 0o600)
    real_fstat = os.fstat

    def lying_fstat(fd: int) -> os.stat_result:
        st = real_fstat(fd)
        return os.stat_result(
            (
                st.st_mode,
                st.st_ino + 4242,
                st.st_dev,
                st.st_nlink,
                st.st_uid,
                st.st_gid,
                st.st_size,
                getattr(st, "st_atime", 0),
                getattr(st, "st_mtime", 0),
                getattr(st, "st_ctime", 0),
            )
        )

    monkeypatch.setattr(persist_mod.os, "fstat", lying_fstat)
    with pytest.raises(PersistError, match="inode changed"):
        secure_acquire_lock_fd(swap_dir, ".state.lock")
    monkeypatch.setattr(persist_mod.os, "fstat", real_fstat)
    assert swap_lock.read_bytes() == b"y"
    assert stat.S_IMODE(swap_lock.stat().st_mode) == 0o600


def test_partial_os_write_refused_for_replace_and_exclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dx.persist as persist_mod
    from dx.persist import secure_exclusive_write_json, secure_write_bytes

    run_dir = _private_run(tmp_path)
    real_write = persist_mod.os.write

    def short_write(fd: int, data: bytes | memoryview) -> int:
        raw = bytes(data) if not isinstance(data, (bytes, bytearray)) else bytes(data)
        if len(raw) > 3:
            return real_write(fd, raw[:3])
        return real_write(fd, raw)

    monkeypatch.setattr(persist_mod.os, "write", short_write)

    replace_path = run_dir / "partial-replace.bin"
    # Looping writer should still complete when short writes keep progressing.
    secure_write_bytes(replace_path, b"0123456789")
    assert replace_path.read_bytes() == b"0123456789"

    calls = {"n": 0}

    def one_shot_short(fd: int, data: bytes | memoryview) -> int:
        calls["n"] += 1
        raw = bytes(data) if not isinstance(data, (bytes, bytearray)) else bytes(data)
        if calls["n"] == 1 and len(raw) > 3:
            return real_write(fd, raw[:3])
        return 0

    monkeypatch.setattr(persist_mod.os, "write", one_shot_short)
    stuck = run_dir / "stuck.bin"
    with pytest.raises(PersistError, match="short write"):
        secure_write_bytes(stuck, b"0123456789")
    assert not stuck.exists()

    calls["n"] = 0
    exclusive = run_dir / "partial-exclusive.json"
    with pytest.raises(PersistError, match="short write"):
        secure_exclusive_write_json(exclusive, {"a": 1})
    assert not exclusive.exists()


def _make_resume_run(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    import subprocess

    from dx.profile import ProjectProfile
    from dx.runstate import write_run_metadata

    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "--initial-branch=main", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "dx08a@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "DX-08A"],
        check=True,
        capture_output=True,
    )
    task = repo / "docs" / "tasks" / "DX-08A.md"
    task.parent.mkdir(parents=True)
    task.write_text("# DX-08A\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "base"],
        check=True,
        capture_output=True,
    )
    base = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    worktree = tmp_path / "worktree"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "--detach", str(worktree), base],
        check=True,
        capture_output=True,
    )
    (worktree / "tracked.txt").write_text("changed\n", encoding="utf-8")
    run_dir = tmp_path / "state" / "runs" / "dx08a-run"
    run_dir.mkdir(parents=True, mode=0o700)
    write_run_metadata(
        run_dir,
        {
            "repo": str(repo.resolve()),
            "task_file": "docs/tasks/DX-08A.md",
            "base_commit": base,
            "worktree": str(worktree.resolve()),
            "max_iterations": 3,
            "env_file": None,
            "profile": ProjectProfile().public_dict(),
        },
    )
    iteration = run_dir / "iteration"
    iteration.write_text("1\n", encoding="utf-8")
    os.chmod(iteration, 0o600)
    return repo, worktree, run_dir, base


def test_resume_rejects_unsafe_snapshots_and_reports(tmp_path: Path) -> None:
    from dx.approval import compute_diff_hash
    from dx.runstate import RunStateError, plan_resume
    from dx.schemas import FUTURE_SCHEMA_REFUSAL

    _repo, worktree, run_dir, base = _make_resume_run(tmp_path)
    transition_run(run_dir, RunEvent.RUN_STARTED)
    transition_run(run_dir, RunEvent.REVIEW_STARTED)
    snapshot = compute_diff_hash(worktree, base)
    snap = run_dir / "review-1-snapshot.json"
    cursor = run_dir / "cursor-1.json"
    secure_write_json(
        snap,
        {"schema_version": 1, "iteration": 1, "diff_hash": snapshot},
    )
    secure_write_json(cursor, _cursor_agent_result("ok"))

    # Dangling symlink must not be treated as absent.
    snap.unlink()
    snap.symlink_to(run_dir / "missing-target.json")
    with pytest.raises(RunStateError, match="symlink|invalid review snapshot"):
        plan_resume(run_dir)
    assert snap.is_symlink()

    # Present symlink to a valid-looking payload still fails closed.
    decoy = run_dir / "decoy-snap.json"
    secure_write_json(
        decoy,
        {"schema_version": 1, "iteration": 1, "diff_hash": snapshot},
    )
    snap.unlink(missing_ok=True)
    snap.symlink_to(decoy)
    with pytest.raises(RunStateError, match="symlink|invalid review snapshot"):
        plan_resume(run_dir)

    # Wrong mode.
    snap.unlink(missing_ok=True)
    secure_write_json(
        snap,
        {"schema_version": 1, "iteration": 1, "diff_hash": snapshot},
    )
    os.chmod(snap, 0o644)
    with pytest.raises(RunStateError, match="insecure|invalid review snapshot"):
        plan_resume(run_dir)

    # Malformed / unknown fields.
    os.chmod(snap, 0o600)
    secure_write_json(
        snap,
        {
            "schema_version": 1,
            "iteration": 1,
            "diff_hash": snapshot,
            "extra": True,
        },
    )
    with pytest.raises(RunStateError, match="unknown or missing fields"):
        plan_resume(run_dir)

    # Future schema.
    secure_write_json(
        snap,
        {"schema_version": 99, "iteration": 1, "diff_hash": snapshot},
    )
    with pytest.raises(RunStateError, match=FUTURE_SCHEMA_REFUSAL):
        plan_resume(run_dir)

    # Valid snapshot + dangling cursor report must fail closed.
    secure_write_json(
        snap,
        {"schema_version": 1, "iteration": 1, "diff_hash": snapshot},
    )
    cursor.unlink(missing_ok=True)
    cursor.symlink_to(run_dir / "missing-cursor.json")
    with pytest.raises(RunStateError, match="symlink"):
        plan_resume(run_dir)


def test_unsafe_heartbeat_snapshot_write_and_evidence_fail_closed(
    tmp_path: Path,
) -> None:
    from dx.atomic import atomic_write_json
    from dx.runstate import RunStateError, attach_evidence

    run_dir = _private_run(tmp_path)
    heartbeat = run_dir / "heartbeat.json"
    decoy = run_dir / "heartbeat-decoy.json"
    secure_write_json(decoy, {"schema_version": 1, "state": "active"})
    heartbeat.symlink_to(decoy)
    with pytest.raises((PersistError, ValueError, OSError), match="symlink"):
        atomic_write_json(
            heartbeat,
            {
                "schema_version": 1,
                "phase": "executor",
                "iteration": 1,
                "state": "active",
            },
        )
    assert heartbeat.is_symlink()

    _repo, _worktree, evidence_run, _base = _make_resume_run(tmp_path / "ev")
    source = tmp_path / "ev-source.txt"
    source.write_text("payload\n", encoding="utf-8")
    # First attach succeeds via secure API.
    from dx.runstate import attach_evidence as attach

    entry = attach(evidence_run, source)
    dest = evidence_run / "evidence" / entry["name"]
    assert dest.read_text(encoding="utf-8") == "payload\n"

    # Symlinked evidence destination must not be treated as a hash hit.
    dest.unlink()
    dest.symlink_to(source)
    with pytest.raises(RunStateError, match="tampered|symlink|cannot safely"):
        attach(evidence_run, source)

    # Symlinked evidence manifest fails closed.
    dest.unlink(missing_ok=True)
    secure_write_bytes = __import__("dx.persist", fromlist=["secure_write_bytes"]).secure_write_bytes
    secure_write_bytes(dest, b"payload\n", mode=0o600, containment_root=evidence_run)
    manifest = evidence_run / "evidence.json"
    if manifest.exists():
        manifest.unlink()
    manifest.symlink_to(source)
    with pytest.raises(RunStateError, match="manifest|symlink"):
        attach(evidence_run, source)


def _arm_awaiting(run_dir: Path) -> None:
    transition_run(run_dir, RunEvent.RUN_STARTED)
    transition_run(run_dir, RunEvent.REVIEW_STARTED)
    transition_run(run_dir, RunEvent.REVIEW_APPROVED)
    (run_dir / "status").write_text("AWAITING_HUMAN_APPROVAL\n", encoding="utf-8")
    os.chmod(run_dir / "status", 0o600)


@pytest.mark.parametrize(
    "case_id,mutate,match",
    [
        (
            "future-schema",
            lambda d: d.__setitem__("schema_version", 999),
            "newer than this runner|malformed|schema",
        ),
        ("reject-decision", lambda d: d.__setitem__("decision", "reject"), "must be approve"),
        ("foreign-run", lambda d: d.__setitem__("run_id", "foreign-run"), "run_id mismatch"),
        ("bad-hash", lambda d: d.__setitem__("diff_hash", "f" * 64), "diff_hash mismatch"),
        (
            "bad-token",
            lambda d: d.__setitem__("callback_token", "z" * 32),
            "callback_token mismatch",
        ),
        ("uid-zero", lambda d: d.__setitem__("telegram_user_id", 0), "telegram_user_id"),
        ("uid-neg", lambda d: d.__setitem__("telegram_user_id", -1), "telegram_user_id"),
        ("chat-str", lambda d: d.__setitem__("telegram_chat_id", "1"), "telegram_chat_id"),
        ("chat-zero", lambda d: d.__setitem__("telegram_chat_id", 0), "telegram_chat_id"),
    ],
)
def test_critical_decision_binding_matrix_refuses_invalid(
    tmp_path: Path, case_id: str, mutate, match: str
) -> None:
    run_dir = _private_run(tmp_path, f"bind-{case_id}")
    _arm_awaiting(run_dir)
    artifacts = _artifact_for(RunEvent.HUMAN_APPROVED.value, run_dir)
    decision = dict(artifacts["human_approval_decision.json"])
    mutate(decision)
    before = _snapshot(run_dir)
    txn = LogicalTransaction(
        run_dir=run_dir,
        event=RunEvent.HUMAN_APPROVED.value,
        status_event=RunEvent.HUMAN_APPROVED,
    )
    txn.add_json(
        "human_approval_request.json",
        artifacts["human_approval_request.json"],
    )
    txn.add_json("human_approval_decision.json", decision)
    with pytest.raises(TransactionError, match=match):
        txn.commit()
    assert read_status(run_dir) == "AWAITING_HUMAN_APPROVAL"
    assert not (run_dir / "human_approval_decision.json").exists()
    assert _snapshot(run_dir) == before


def test_critical_decision_missing_request_and_unconsumed_token_refused(
    tmp_path: Path,
) -> None:
    run_dir = _private_run(tmp_path, "missing-req")
    _arm_awaiting(run_dir)
    decision = {
        "schema_version": 1,
        "decision": "approve",
        "run_id": run_dir.name,
        "diff_hash": "a" * 64,
        "callback_token": "b" * 32,
        "telegram_user_id": 1,
        "telegram_chat_id": 1,
        "decided_at": "2026-07-25T00:00:00Z",
    }
    before = _snapshot(run_dir)
    missing = LogicalTransaction(
        run_dir=run_dir,
        event=RunEvent.HUMAN_APPROVED.value,
        status_event=RunEvent.HUMAN_APPROVED,
    )
    missing.add_json("human_approval_decision.json", decision)
    with pytest.raises(TransactionError, match="corresponding request"):
        missing.commit()
    assert _snapshot(run_dir) == before

    request = _approval_request_payload(run_dir, token_consumed=False)
    secure_write_json(run_dir / "human_approval_request.json", request)
    before = _snapshot(run_dir)
    unconsumed = LogicalTransaction(
        run_dir=run_dir,
        event=RunEvent.HUMAN_APPROVED.value,
        status_event=RunEvent.HUMAN_APPROVED,
    )
    unconsumed.add_json("human_approval_decision.json", decision)
    with pytest.raises(TransactionError, match="token not consumed"):
        unconsumed.commit()
    assert read_status(run_dir) == "AWAITING_HUMAN_APPROVAL"
    assert not (run_dir / "human_approval_decision.json").exists()
    assert (run_dir / "human_approval_request.json").read_bytes() == before[
        "human_approval_request.json"
    ]


def test_critical_decision_recovery_refuses_forged_bindings(tmp_path: Path) -> None:
    from dx.txn import (
        JOURNAL_COMMITTING,
        recover_run_transactions,
        validate_journal,
    )
    from dx.persist import canonical_json_hash
    from dx.schemas import RUNNER_VERSION, TXN_JOURNAL_SCHEMA_VERSION

    run_dir = _private_run(tmp_path, "recover-forge")
    _arm_awaiting(run_dir)
    # Durable request with schema 999 must not authorize a forged decision.
    forged_request = _approval_request_payload(run_dir, token_consumed=True)
    forged_request["schema_version"] = 999
    secure_write_json(run_dir / "human_approval_request.json", forged_request)
    decision = {
        "schema_version": 999,
        "decision": "reject",
        "run_id": "foreign-run",
        "diff_hash": "f" * 64,
        "callback_token": "z" * 32,
        "telegram_user_id": -1,
        "telegram_chat_id": 0,
        "decided_at": "2026-07-25T00:00:00Z",
    }
    hashes = {"human_approval_decision.json": canonical_json_hash(decision)}
    journal = {
        "schema_version": TXN_JOURNAL_SCHEMA_VERSION,
        "runner_version": RUNNER_VERSION,
        "run_id": run_dir.name,
        "phase": JOURNAL_COMMITTING,
        "event": RunEvent.HUMAN_APPROVED.value,
        "status_event": RunEvent.HUMAN_APPROVED.value,
        "previous_state": "AWAITING_HUMAN_APPROVAL",
        "artifact_hashes": hashes,
        "artifacts": {"human_approval_decision.json": decision},
        "exclusive_artifacts": ["human_approval_decision.json"],
        "origin": "recovery",
        "created_at": "2026-07-25T00:00:00Z",
    }
    validate_journal(journal, expected_run_id=run_dir.name)
    secure_write_json(run_dir / ".txn.json", journal)
    before_status = read_status(run_dir)
    with pytest.raises(TransactionError):
        recover_run_transactions(run_dir)
    assert read_status(run_dir) == before_status
    assert before_status == "AWAITING_HUMAN_APPROVAL"
    assert not (run_dir / "human_approval_decision.json").exists()


def test_supervisor_terminates_group_on_status_or_heartbeat_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import signal
    import subprocess
    import time

    from dx.runtime import supervise_command
    from dx.state_machine import StateTransitionError

    worktree = tmp_path / "wt"
    worktree.mkdir()
    subprocess.run(["git", "init", str(worktree)], check=True, capture_output=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    child_pid = tmp_path / "child.pid"
    script = (
        "import os, pathlib, time; "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )

    # Insecure status forces read_status to fail during the heartbeat loop.
    status = run_dir / "status"
    status.write_text("EXECUTING\n", encoding="utf-8")
    os.chmod(status, 0o644)

    signals: list[int] = []
    real_killpg = os.killpg

    def tracking_killpg(pgid: int, sig: int) -> None:
        signals.append(sig)
        return real_killpg(pgid, sig)

    monkeypatch.setattr(os, "killpg", tracking_killpg)
    with pytest.raises(StateTransitionError, match="cannot be read safely|insecure"):
        supervise_command(
            command=[sys.executable, "-c", script],
            phase="executor",
            iteration=1,
            cwd=worktree,
            run_dir=run_dir,
            environment={"PATH": os.environ["PATH"]},
            secret_values={},
            timeout_seconds=30,
            heartbeat_seconds=1,
            terminate_grace_seconds=1,
        )
    assert signal.SIGTERM in signals or signal.SIGKILL in signals
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and child_pid.exists() and Path(
        f"/proc/{child_pid.read_text().strip()}"
    ).exists():
        time.sleep(0.05)
    if child_pid.exists():
        assert not Path(f"/proc/{child_pid.read_text().strip()}").exists()

    # Heartbeat symlink/write failure must also reap the group.
    run_dir2 = tmp_path / "run-hb"
    run_dir2.mkdir(mode=0o700)
    (run_dir2 / "status").write_text("EXECUTING\n", encoding="utf-8")
    os.chmod(run_dir2 / "status", 0o600)
    heartbeat = run_dir2 / "heartbeat.json"
    decoy = run_dir2 / "hb-decoy.json"
    secure_write_json(decoy, {"schema_version": 1})
    heartbeat.symlink_to(decoy)
    child_pid2 = tmp_path / "child2.pid"
    script2 = (
        "import os, pathlib, time; "
        f"pathlib.Path({str(child_pid2)!r}).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    signals.clear()
    with pytest.raises((PersistError, ValueError, OSError), match="symlink"):
        supervise_command(
            command=[sys.executable, "-c", script2],
            phase="executor",
            iteration=1,
            cwd=worktree,
            run_dir=run_dir2,
            environment={"PATH": os.environ["PATH"]},
            secret_values={},
            timeout_seconds=30,
            heartbeat_seconds=1,
            terminate_grace_seconds=1,
        )
    assert signal.SIGTERM in signals or signal.SIGKILL in signals
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and child_pid2.exists() and Path(
        f"/proc/{child_pid2.read_text().strip()}"
    ).exists():
        time.sleep(0.05)
    if child_pid2.exists():
        assert not Path(f"/proc/{child_pid2.read_text().strip()}").exists()


def _valid_technical_summary() -> dict:
    return {
        "schema_version": 1,
        "task_id": "DX-08A",
        "task_title": "test",
        "repository": "repo",
        "base_commit": "abc",
        "iteration": 1,
        "max_iterations": 3,
        "reviewed_diff_hash": "a" * 64,
        "files": ["a.py"],
        "file_count": 1,
        "additions": 1,
        "deletions": 0,
        "executor_summary": "ok",
        "test_counts": {"passed": 1, "failed": 0, "skipped": 0, "errors": 0},
        "test_commands": ["pytest"],
        "validation_status": "passed",
        "reviewer_status": "APPROVED",
        "reviewer_summary": "ok",
        "findings": [],
        "residual_risks": [],
        "documentation": [],
        "prepared_at": "2026-07-25T00:00:00Z",
        "telegram_messages": ["hello"],
    }


def test_technical_summary_reports_and_evidence_fail_closed_on_contract(
    tmp_path: Path,
) -> None:
    from dx.cli import main as cli_main
    from dx.runstate import RunStateError, _regular_report_present, attach_evidence
    from dx.schemas import FUTURE_SCHEMA_REFUSAL
    from dx.snapshot import SUMMARY_FILENAME

    run_dir = _private_run(tmp_path, "contracts")

    # Future / unknown-field / malformed telegram_messages on technical summary.
    summary = run_dir / SUMMARY_FILENAME
    bad_future = _valid_technical_summary()
    bad_future["schema_version"] = 99
    secure_write_json(summary, bad_future)
    assert (
        cli_main(
            [
                "create-request",
                "--run-dir",
                str(run_dir),
                "--task",
                "docs/tasks/DX-08A.md",
                "--base-commit",
                "abc",
                "--worktree",
                str(tmp_path),
                "--review-report",
                "review.json",
            ]
        )
        == 1
    )

    bad_unknown = _valid_technical_summary()
    bad_unknown["evil"] = True
    secure_write_json(summary, bad_unknown)
    assert (
        cli_main(
            [
                "create-request",
                "--run-dir",
                str(run_dir),
                "--task",
                "docs/tasks/DX-08A.md",
                "--base-commit",
                "abc",
                "--worktree",
                str(tmp_path),
                "--review-report",
                "review.json",
            ]
        )
        == 1
    )

    bad_messages = _valid_technical_summary()
    bad_messages["telegram_messages"] = [1, "x"]
    secure_write_json(summary, bad_messages)
    assert (
        cli_main(
            [
                "create-request",
                "--run-dir",
                str(run_dir),
                "--task",
                "docs/tasks/DX-08A.md",
                "--base-commit",
                "abc",
                "--worktree",
                str(tmp_path),
                "--review-report",
                "review.json",
            ]
        )
        == 1
    )

    # Zero-byte private report is corrupt, not absent.
    cursor = run_dir / "cursor-1.json"
    cursor.write_bytes(b"")
    os.chmod(cursor, 0o600)
    with pytest.raises(RunStateError, match="empty or truncated"):
        _regular_report_present(cursor, label="cursor-1.json")

    # Zero-byte report with insecure mode must fail closed (not "absent").
    os.chmod(cursor, 0o644)
    with pytest.raises(RunStateError, match="cannot be read safely|insecure"):
        _regular_report_present(cursor, label="cursor-1.json")

    # Malformed / future / unknown-field resume reports.
    cursor.unlink()
    secure_write_json(cursor, {**_cursor_agent_result("ok"), "evil": True})
    with pytest.raises(RunStateError, match="unknown fields"):
        _regular_report_present(cursor, label="cursor-1.json")

    secure_write_json(
        cursor, {**_cursor_agent_result("ok"), "schema_version": 99}
    )
    with pytest.raises(RunStateError, match=FUTURE_SCHEMA_REFUSAL):
        _regular_report_present(cursor, label="cursor-1.json")

    # Empty / partial report contracts must fail closed.
    secure_write_json(cursor, {})
    with pytest.raises(RunStateError, match="missing required fields"):
        _regular_report_present(cursor, label="cursor-1.json")
    # Legacy synthetic summary-only payloads are not the production contract.
    secure_write_json(cursor, {"summary": "ok"})
    with pytest.raises(RunStateError, match="unknown fields|missing required"):
        _regular_report_present(cursor, label="cursor-1.json")
    review_report = run_dir / "review-1.json"
    secure_write_json(review_report, {})
    with pytest.raises(RunStateError, match="missing required fields"):
        _regular_report_present(review_report, label="review-1.json")
    secure_write_json(review_report, {"schema_version": 1})
    with pytest.raises(RunStateError, match="missing required fields"):
        _regular_report_present(review_report, label="review-1.json")
    secure_write_json(
        review_report,
        {
            "status": "APPROVED",
            "summary": None,
            "findings": [],
            "tests_required": [],
        },
    )
    with pytest.raises(RunStateError, match="field types are invalid"):
        _regular_report_present(review_report, label="review-1.json")

    result = run_dir / "reviewer-1-result.json"
    secure_write_json(result, {"schema_version": 1})
    with pytest.raises(RunStateError, match="missing required fields"):
        _regular_report_present(result, label="reviewer-1-result.json")
    secure_write_json(
        result,
        {
            "schema_version": 1,
            "phase": "reviewer",
            "iteration": 1,
            "state": "completed",
            "reason": None,
            "exit_code": 0,
            "child_exit_code": 0,
            "elapsed_seconds": 1.0,
            "last_activity_at": "2026-07-25T00:00:00Z",
            "changed_files": 0,
            "finished_at": "2026-07-25T00:00:00Z",
            "extra": True,
        },
    )
    with pytest.raises(RunStateError, match="unknown fields"):
        _regular_report_present(result, label="reviewer-1-result.json")

    # Null-valued technical summary fields fail closed.
    from dx.snapshot import SnapshotError, validate_technical_summary

    null_summary = _valid_technical_summary()
    for key in (
        "task_id",
        "task_title",
        "repository",
        "base_commit",
        "executor_summary",
        "validation_status",
        "reviewer_status",
        "reviewer_summary",
        "prepared_at",
    ):
        null_summary[key] = None
    with pytest.raises(SnapshotError, match="field types are invalid"):
        validate_technical_summary(null_summary)

    # Evidence manifest future schema with a previously unseen source must
    # refuse before publishing a blob or creating locks/dirs.
    _repo, _worktree, evidence_run, _base = _make_resume_run(tmp_path / "ev2")
    source = tmp_path / "ev2-source.txt"
    source.write_text("payload-unseen\n", encoding="utf-8")
    secure_write_json(
        evidence_run / "evidence.json",
        {"schema_version": 99, "items": []},
    )
    before_evidence = _snapshot(evidence_run)
    before_names = sorted(p.name for p in evidence_run.iterdir())
    with pytest.raises(RunStateError, match=FUTURE_SCHEMA_REFUSAL):
        attach_evidence(evidence_run, source)
    assert _snapshot(evidence_run) == before_evidence
    assert sorted(p.name for p in evidence_run.iterdir()) == before_names
    assert not (evidence_run / "evidence").exists()
    assert not (evidence_run / ".resume.lock").exists()

    # After a valid attach, corrupt manifest variants still refuse without
    # publishing a new blob for an unseen source.
    (evidence_run / "evidence.json").unlink()
    good_source = tmp_path / "ev2-good.txt"
    good_source.write_text("good-payload\n", encoding="utf-8")
    attach_evidence(evidence_run, good_source)
    manifest = evidence_run / "evidence.json"
    unseen = tmp_path / "ev2-unseen2.txt"
    unseen.write_text("another-payload\n", encoding="utf-8")

    secure_write_json(manifest, {"schema_version": 99, "items": []})
    before = _snapshot(evidence_run)
    with pytest.raises(RunStateError, match=FUTURE_SCHEMA_REFUSAL):
        attach_evidence(evidence_run, unseen)
    assert _snapshot(evidence_run) == before

    secure_write_json(
        manifest,
        {"schema_version": 1, "items": [], "evil": True},
    )
    before = _snapshot(evidence_run)
    with pytest.raises(RunStateError, match="unknown fields"):
        attach_evidence(evidence_run, unseen)
    assert _snapshot(evidence_run) == before

    secure_write_json(
        manifest,
        {
            "schema_version": 1,
            "items": [
                {
                    "name": "x",
                    "sha256": "not-a-hash",
                    "size_bytes": "1",
                    "attached_at": None,
                    "trust": 1,
                }
            ],
        },
    )
    before = _snapshot(evidence_run)
    with pytest.raises(RunStateError, match="item fields|invalid evidence"):
        attach_evidence(evidence_run, unseen)
    assert _snapshot(evidence_run) == before


def test_failure_replay_refuses_malformed_future_and_mismatched(
    tmp_path: Path,
) -> None:
    from dx.cli import main as cli_main
    from dx.persist import canonical_json_hash
    from dx.txn import _validate_failure_payload

    run_dir = _private_run(tmp_path, "fail-replay")
    transition_run(run_dir, RunEvent.RUN_STARTED)
    txn = LogicalTransaction(
        run_dir=run_dir,
        event=RunEvent.RUN_BLOCKED.value,
        status_event=RunEvent.RUN_BLOCKED,
    )
    good = _failure_payload()
    txn.add_json("failure.json", good)
    txn.commit()
    assert read_status(run_dir) == "BLOCKED"
    first_bytes = (run_dir / "failure.json").read_bytes()

    # Valid-but-different proposed reason: first durable failure wins.
    other = dict(good)
    other["reason"] = "other-reason"
    replay_other = LogicalTransaction(
        run_dir=run_dir,
        event=RunEvent.RUN_BLOCKED.value,
        status_event=RunEvent.RUN_BLOCKED,
    )
    replay_other.add_json("failure.json", other)
    outcome = replay_other.commit()
    assert outcome["result"] == "already_applied"
    assert outcome["artifact_hashes"]["failure.json"] == canonical_json_hash(good)
    assert (run_dir / "failure.json").read_bytes() == first_bytes

    # Corrupt / future-schema / missing on-disk binding must refuse without
    # mutation when status is already at the critical target — destination
    # state alone never authorizes inventing or repairing the binding.
    secure_write_json(run_dir / "failure.json", {"corrupt": True})
    audit_before = (
        (run_dir / "audit-trail.json").read_bytes()
        if (run_dir / "audit-trail.json").exists()
        else b""
    )
    before_corrupt = _snapshot(run_dir)
    corrupt_existing = LogicalTransaction(
        run_dir=run_dir,
        event=RunEvent.RUN_BLOCKED.value,
        status_event=RunEvent.RUN_BLOCKED,
    )
    corrupt_existing.add_json("failure.json", good)
    with pytest.raises(TransactionError, match="matching bound artifact|refusing replay"):
        corrupt_existing.commit()
    assert _snapshot(run_dir) == before_corrupt
    assert secure_read_json(run_dir / "failure.json") == {"corrupt": True}
    assert (
        (run_dir / "audit-trail.json").read_bytes()
        if (run_dir / "audit-trail.json").exists()
        else b""
    ) == audit_before
    assert not (run_dir / ".txn.json").exists()

    # Restore a valid failure so exact-match replay can be exercised next.
    secure_write_json(run_dir / "failure.json", good)
    exact = (run_dir / "failure.json").read_bytes()
    replay = LogicalTransaction(
        run_dir=run_dir,
        event=RunEvent.RUN_BLOCKED.value,
        status_event=RunEvent.RUN_BLOCKED,
    )
    replay.add_json("failure.json", good)
    assert replay.commit()["result"] == "already_applied"
    assert (run_dir / "failure.json").read_bytes() == exact

    # Proposed malformed failure refused before journal/audit mutation.
    audit_before = (
        (run_dir / "audit-trail.json").read_bytes()
        if (run_dir / "audit-trail.json").exists()
        else b""
    )
    corrupt_txn = LogicalTransaction(
        run_dir=run_dir,
        event=RunEvent.RUN_BLOCKED.value,
        status_event=RunEvent.RUN_BLOCKED,
    )
    corrupt_txn.add_json("failure.json", {"corrupt": True})
    with pytest.raises(TransactionError, match="malformed|failure"):
        corrupt_txn.commit()
    assert (run_dir / "failure.json").read_bytes() == exact
    assert (
        (run_dir / "audit-trail.json").read_bytes()
        if (run_dir / "audit-trail.json").exists()
        else b""
    ) == audit_before
    assert not (run_dir / ".txn.json").exists()

    # Future-schema proposed payload fails closed.
    future = dict(good)
    future["schema_version"] = 99
    future_txn = LogicalTransaction(
        run_dir=run_dir,
        event=RunEvent.RUN_BLOCKED.value,
        status_event=RunEvent.RUN_BLOCKED,
    )
    future_txn.add_json("failure.json", future)
    with pytest.raises(TransactionError, match="newer|future|schema|malformed"):
        future_txn.commit()
    assert (run_dir / "failure.json").read_bytes() == exact
    assert not (run_dir / ".txn.json").exists()

    # CLI fallback: when commit cannot run (insecure lock) and existing failure
    # is corrupt, must not return success.
    secure_write_json(run_dir / "failure.json", {"corrupt": True})
    lock_path = run_dir / ".state.lock"
    if lock_path.exists():
        os.chmod(lock_path, 0o644)
    else:
        lock_path.write_text("x", encoding="utf-8")
        os.chmod(lock_path, 0o644)
    assert (
        cli_main(
            [
                "record-failure",
                "--run-dir",
                str(run_dir),
                "--reason",
                "cli-retry",
                "--phase",
                "loop",
                "--iteration",
                "1",
            ]
        )
        == 1
    )
    assert secure_read_json(run_dir / "failure.json") == {"corrupt": True}

    # CLI fallback with valid existing failure + insecure lock still succeeds.
    secure_write_json(run_dir / "failure.json", good)
    assert (
        cli_main(
            [
                "record-failure",
                "--run-dir",
                str(run_dir),
                "--reason",
                "cli-retry",
                "--phase",
                "loop",
                "--iteration",
                "1",
            ]
        )
        == 0
    )
    _validate_failure_payload(secure_read_json(run_dir / "failure.json"))
    assert secure_read_json(run_dir / "failure.json") == good


@pytest.mark.parametrize(
    "case_id,mutate,match",
    [
        (
            "rejected-status",
            lambda r: r.__setitem__("technical_status", "REJECTED"),
            "technical_status must be APPROVED",
        ),
        (
            "blocked-status",
            lambda r: r.__setitem__("technical_status", "BLOCKED"),
            "technical_status must be APPROVED",
        ),
        (
            "short-hash",
            lambda r: r.__setitem__("diff_hash", "abc"),
            "diff_hash",
        ),
        (
            "nonhex-hash",
            lambda r: r.__setitem__("diff_hash", "g" * 64),
            "diff_hash",
        ),
        (
            "short-token",
            lambda r: r.__setitem__("callback_token", "ab"),
            "callback_token",
        ),
        (
            "nonhex-token",
            lambda r: r.__setitem__("callback_token", "z" * 32),
            "callback_token",
        ),
    ],
)
def test_approval_request_semantic_matrix_refuses_invalid(
    tmp_path: Path, case_id: str, mutate, match: str
) -> None:
    run_dir = _private_run(tmp_path, f"req-{case_id}")
    _arm_for_critical(run_dir, RunEvent.APPROVAL_REQUESTED.value)
    request = _approval_request_payload(run_dir)
    mutate(request)
    before = _snapshot(run_dir)
    txn = LogicalTransaction(
        run_dir=run_dir,
        event=RunEvent.APPROVAL_REQUESTED.value,
        status_event=RunEvent.APPROVAL_REQUESTED,
    )
    txn.add_json("human_approval_request.json", request)
    with pytest.raises(TransactionError, match=match):
        txn.commit()
    assert read_status(run_dir) == "APPROVED"
    assert not (run_dir / "human_approval_request.json").exists()
    assert _snapshot(run_dir) == before
    assert not (run_dir / ".txn.json").exists()


def test_iteration_budget_empty_chain_and_recovery_refused(tmp_path: Path) -> None:
    from dx.txn import JOURNAL_COMMITTING, recover_run_transactions
    from dx.persist import canonical_json_hash
    from dx.schemas import RUNNER_VERSION, TXN_JOURNAL_SCHEMA_VERSION

    run_dir = _private_run(tmp_path, "budget-empty")
    _arm_for_critical(run_dir, RunEvent.ITERATION_BUDGET_EXTENDED.value)
    before = _snapshot(run_dir)
    empty = {
        "schema_version": 1,
        "run_id": run_dir.name,
        "original_limit": 3,
        "effective_limit": 5,
        "extensions": [],
        "updated_at": "2026-07-25T00:00:00Z",
    }
    txn = LogicalTransaction(
        run_dir=run_dir,
        event=RunEvent.ITERATION_BUDGET_EXTENDED.value,
        status_event=RunEvent.ITERATION_BUDGET_EXTENDED,
    )
    txn.add_json("iteration-budget.json", empty)
    with pytest.raises(TransactionError, match="iteration-budget|extensions"):
        txn.commit()
    assert read_status(run_dir) == "BLOCKED"
    assert not (run_dir / "iteration-budget.json").exists()
    assert _snapshot(run_dir) == before
    assert not (run_dir / ".txn.json").exists()

    # Valid chain still commits.
    valid = _iteration_budget_payload(run_dir)
    ok = LogicalTransaction(
        run_dir=run_dir,
        event=RunEvent.ITERATION_BUDGET_EXTENDED.value,
        status_event=RunEvent.ITERATION_BUDGET_EXTENDED,
    )
    ok.add_json("iteration-budget.json", valid)
    assert ok.commit()["result"] in {"committed", "already_applied"}
    assert secure_read_json(run_dir / "iteration-budget.json") == valid

    # Recovery journal carrying empty chain refuses before mutation.
    run2 = _private_run(tmp_path, "budget-recover")
    _arm_for_critical(run2, RunEvent.ITERATION_BUDGET_EXTENDED.value)
    audit_before = (
        (run2 / "audit-trail.json").read_bytes() if (run2 / "audit-trail.json").exists() else b""
    )
    forged = {
        "schema_version": 1,
        "run_id": run2.name,
        "original_limit": 3,
        "effective_limit": 5,
        "extensions": [],
        "updated_at": "2026-07-25T00:00:00Z",
    }
    journal = {
        "schema_version": TXN_JOURNAL_SCHEMA_VERSION,
        "runner_version": RUNNER_VERSION,
        "run_id": run2.name,
        "phase": JOURNAL_COMMITTING,
        "event": RunEvent.ITERATION_BUDGET_EXTENDED.value,
        "status_event": RunEvent.ITERATION_BUDGET_EXTENDED.value,
        "previous_state": "BLOCKED",
        "artifact_hashes": {"iteration-budget.json": canonical_json_hash(forged)},
        "artifacts": {"iteration-budget.json": forged},
        "exclusive_artifacts": [],
        "origin": "recovery",
        "created_at": "2026-07-25T00:00:00Z",
    }
    secure_write_json(run2 / ".txn.json", journal)
    with pytest.raises(TransactionError, match="iteration-budget|extensions"):
        recover_run_transactions(run2)
    assert not (run2 / "iteration-budget.json").exists()
    assert read_status(run2) == "BLOCKED"
    assert (
        (run2 / "audit-trail.json").read_bytes() if (run2 / "audit-trail.json").exists() else b""
    ) == audit_before


def test_symlinked_reviewer_executor_and_report_inputs_fail_closed(
    tmp_path: Path,
) -> None:
    from dx.cli import main as cli_main
    from dx.snapshot import SnapshotError, build_snapshot_manifest, prepare_review_artifacts
    from dx.profile import ProjectProfile

    run_dir = _private_run(tmp_path, "symlink-io")
    decoy = run_dir / "decoy-review.json"
    secure_write_json(
        decoy,
        {
            "status": "APPROVED",
            "summary": "ok",
            "findings": [],
            "tests_required": [],
        },
    )
    linked = run_dir / "review-linked.json"
    linked.symlink_to(decoy)
    assert cli_main(["review-status", "--file", str(linked)]) == 1
    assert linked.is_symlink()

    repo, worktree, prep_run, base = _make_resume_run(tmp_path / "prep")
    decoy2 = prep_run / "decoy-review.json"
    secure_write_json(
        decoy2,
        {
            "status": "APPROVED",
            "summary": "ok",
            "findings": [],
            "tests_required": [],
        },
    )
    executor = prep_run / "cursor-1.json"
    secure_write_json(executor, _cursor_agent_result("done"))
    reviewer_link = prep_run / "reviewer-report.json"
    reviewer_link.symlink_to(decoy2)
    manifest = build_snapshot_manifest(worktree, base)
    profile = ProjectProfile()
    with pytest.raises(SnapshotError, match="symlink|invalid reviewer"):
        prepare_review_artifacts(
            run_dir=prep_run,
            repo=repo,
            worktree=worktree,
            task_file="docs/tasks/DX-08A.md",
            task_id="DX-08A",
            task_slug="dx-08a",
            base_commit=base,
            iteration=1,
            max_iterations=3,
            executor_report=executor,
            reviewer_report=reviewer_link,
            reviewed_hash=manifest["snapshot_hash"],
            profile=profile,
        )

    executor_link = prep_run / "executor-link.json"
    executor_link.symlink_to(executor)
    real_reviewer = prep_run / "reviewer-real.json"
    secure_write_json(
        real_reviewer,
        {
            "status": "APPROVED",
            "summary": "ok",
            "findings": [],
            "tests_required": [],
        },
    )
    with pytest.raises(SnapshotError, match="symlink|cannot be read safely"):
        prepare_review_artifacts(
            run_dir=prep_run,
            repo=repo,
            worktree=worktree,
            task_file="docs/tasks/DX-08A.md",
            task_id="DX-08A",
            task_slug="dx-08a",
            base_commit=base,
            iteration=1,
            max_iterations=3,
            executor_report=executor_link,
            reviewer_report=real_reviewer,
            reviewed_hash=manifest["snapshot_hash"],
            profile=profile,
        )


def test_lock_intermediate_symlink_and_post_flock_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dx.atomic as atomic_mod
    import dx.persist as persist_mod

    real_run = _private_run(tmp_path, "real-run")
    parent_link = tmp_path / "via-parent"
    parent_link.symlink_to(tmp_path, target_is_directory=True)
    aliased = parent_link / "real-run"
    assert aliased.is_dir()
    before_listing = {p.name: p.read_bytes() for p in real_run.iterdir() if p.is_file()}
    with pytest.raises((PersistError, ValueError), match="symlink"):
        secure_acquire_lock_fd(aliased, ".state.lock")
    with pytest.raises((PersistError, ValueError), match="symlink"):
        with run_scoped_lock(aliased, lock_name=".state.lock"):
            pass
    after_listing = {p.name: p.read_bytes() for p in real_run.iterdir() if p.is_file()}
    assert after_listing == before_listing
    assert not (real_run / ".state.lock").exists()

    # Pathname replacement after open / during flock must fail revalidation.
    swap_dir = _private_run(tmp_path, "post-flock-swap")
    lock_path = swap_dir / ".state.lock"
    lock_path.write_text("original\n", encoding="utf-8")
    os.chmod(lock_path, 0o600)
    original_bytes = lock_path.read_bytes()
    original_ino = lock_path.stat().st_ino

    real_flock = atomic_mod.fcntl.flock

    def flock_then_swap(fd: int, operation: int) -> None:
        real_flock(fd, operation)
        os.unlink(lock_path)
        replacement = swap_dir / ".state.lock"
        replacement.write_text("replacement\n", encoding="utf-8")
        os.chmod(replacement, 0o600)

    monkeypatch.setattr(atomic_mod.fcntl, "flock", flock_then_swap)
    with pytest.raises(
        (PersistError, ValueError), match="replaced after flock|inode"
    ):
        with run_scoped_lock(swap_dir, lock_name=".state.lock"):
            pytest.fail("must not enter critical section after inode replacement")
    monkeypatch.setattr(atomic_mod.fcntl, "flock", real_flock)

    assert lock_path.read_bytes() == b"replacement\n"
    assert lock_path.stat().st_ino != original_ino
    assert original_bytes == b"original\n"

    with run_scoped_lock(swap_dir, lock_name=".state.lock"):
        assert lock_path.read_bytes() == b"replacement\n"

    fd = secure_acquire_lock_fd(swap_dir, ".state.lock")
    try:
        real_fstat = persist_mod.os.fstat

        def lying_fstat(check_fd: int) -> os.stat_result:
            st = real_fstat(check_fd)
            return os.stat_result(
                (
                    st.st_mode,
                    st.st_ino + 99,
                    st.st_dev,
                    st.st_nlink,
                    st.st_uid,
                    st.st_gid,
                    st.st_size,
                    getattr(st, "st_atime", 0),
                    getattr(st, "st_mtime", 0),
                    getattr(st, "st_ctime", 0),
                )
            )

        monkeypatch.setattr(persist_mod.os, "fstat", lying_fstat)
        with pytest.raises(PersistError, match="replaced after flock|inode"):
            persist_mod.revalidate_lock_fd(swap_dir, ".state.lock", fd)
        monkeypatch.setattr(persist_mod.os, "fstat", real_fstat)
    finally:
        os.close(fd)
    assert lock_path.read_bytes() == b"replacement\n"


def _force_status(run_dir: Path, status: str) -> None:
    (run_dir / "status").write_text(f"{status}\n", encoding="utf-8")
    os.chmod(run_dir / "status", 0o600)


def _critical_target(event: str) -> str:
    from dx.state_machine import TRANSITIONS

    return TRANSITIONS[RunEvent(event)].target.value


def _mismatched_binding(event: str, run_dir: Path) -> dict[str, dict]:
    """On-disk binding that is contract-shaped but not equal to the proposal."""
    artifacts = _artifact_for(event, run_dir)
    required = next(iter(CRITICAL_BINDINGS[event]))
    payload = dict(artifacts[required])
    if required == "failure.json":
        payload["reason"] = "mismatched-on-disk"
    elif required == "human_approval_request.json":
        payload["task_id"] = "MISMATCH"
    elif required == "human_approval_decision.json":
        payload["telegram_user_id"] = 999
    elif required == "human_rejection.json":
        payload["reason"] = "mismatched-rejection"
    elif required == "iteration-budget.json":
        payload["updated_at"] = "1999-01-01T00:00:00Z"
    return {required: payload}


def _future_binding(event: str, run_dir: Path) -> dict[str, dict]:
    artifacts = _artifact_for(event, run_dir)
    required = next(iter(CRITICAL_BINDINGS[event]))
    payload = dict(artifacts[required])
    payload["schema_version"] = 99
    return {required: payload}


def _corrupt_binding(event: str) -> dict[str, dict]:
    required = next(iter(CRITICAL_BINDINGS[event]))
    return {required: {"corrupt": True}}


@pytest.mark.parametrize("event", sorted(CRITICAL_BINDINGS))
@pytest.mark.parametrize(
    "binding_case",
    ["missing", "corrupt", "future-schema", "mismatched"],
)
def test_critical_replay_refuses_inventing_binding(
    tmp_path: Path, event: str, binding_case: str
) -> None:
    """Destination status alone must never authorize inventing a binding."""
    run_dir = _private_run(tmp_path, f"replay-{event}-{binding_case}"[:80])
    _arm_for_critical(run_dir, event)
    _force_status(run_dir, _critical_target(event))
    required = next(iter(CRITICAL_BINDINGS[event]))
    if (run_dir / required).exists():
        (run_dir / required).unlink()

    # Build the proposal first — iteration-budget helpers may create review
    # binding files as side effects that must be included in the baseline.
    proposal = _artifact_for(event, run_dir)
    if binding_case == "corrupt":
        secure_write_json(run_dir / required, _corrupt_binding(event)[required])
    elif binding_case == "future-schema":
        for name, payload in _future_binding(event, run_dir).items():
            secure_write_json(run_dir / name, payload)
    elif binding_case == "mismatched":
        for name, payload in _mismatched_binding(event, run_dir).items():
            secure_write_json(run_dir / name, payload)
    elif binding_case == "missing":
        if (run_dir / required).exists():
            (run_dir / required).unlink()

    before = _snapshot(run_dir)
    names_before = sorted(p.name for p in run_dir.iterdir())
    audit_before = (
        (run_dir / "audit-trail.json").read_bytes()
        if (run_dir / "audit-trail.json").exists()
        else b""
    )
    txn = LogicalTransaction(
        run_dir=run_dir,
        event=event,
        status_event=RunEvent(event),
    )
    for name, payload in proposal.items():
        txn.add_json(name, payload)

    # Valid-but-different failure.json is first-failure-wins (already_applied),
    # not an invent/repair of a missing binding.
    if event == RunEvent.RUN_BLOCKED.value and binding_case == "mismatched":
        outcome = txn.commit()
        assert outcome["result"] == "already_applied"
        assert (run_dir / "failure.json").read_bytes() == before["failure.json"]
        assert not (run_dir / ".txn.json").exists()
        return

    with pytest.raises(
        TransactionError, match="matching bound artifact|refusing replay"
    ):
        txn.commit()
    assert read_status(run_dir) == _critical_target(event)
    assert _snapshot(run_dir) == before
    assert sorted(p.name for p in run_dir.iterdir()) == names_before
    assert not (run_dir / ".txn.json").exists()
    assert (
        (run_dir / "audit-trail.json").read_bytes()
        if (run_dir / "audit-trail.json").exists()
        else b""
    ) == audit_before


def test_reserved_artifact_names_cannot_replace_locks_or_authority(
    tmp_path: Path,
) -> None:
    import fcntl
    import threading

    from dx.txn import RESERVED_TRANSACTION_NAMES

    run_dir = _private_run(tmp_path, "reserved-names")
    transition_run(run_dir, RunEvent.RUN_STARTED)
    before = _snapshot(run_dir)

    for name in sorted(RESERVED_TRANSACTION_NAMES):
        txn = LogicalTransaction(
            run_dir=run_dir,
            event=RunEvent.RUN_STARTED.value,
            status_event=None,
        )
        with pytest.raises(TransactionError, match="reserved artifact name"):
            txn.add_json(name, {"schema_version": 1, "evil": True})
        assert _snapshot(run_dir) == before

    # Holding .state.lock, a transaction must not be able to replace its inode.
    lock_path = run_dir / ".state.lock"
    with run_scoped_lock(run_dir, lock_name=".state.lock"):
        held_ino = lock_path.stat().st_ino
        held_bytes = lock_path.read_bytes()
        txn = LogicalTransaction(
            run_dir=run_dir,
            event="spoof",
            status_event=None,
        )
        with pytest.raises(TransactionError, match="reserved artifact name"):
            txn.add_json(".state.lock", {"pwn": True})
        # Concurrent second lock attempt must block/fail while we hold the lock.
        second_error: list[BaseException] = []

        def try_second_lock() -> None:
            try:
                fd = secure_acquire_lock_fd(run_dir, ".state.lock")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    second_error.append(RuntimeError("second lock acquired concurrently"))
                except BlockingIOError as exc:
                    second_error.append(exc)
                finally:
                    os.close(fd)
            except BaseException as exc:  # noqa: BLE001 — capture for assertion
                second_error.append(exc)

        thread = threading.Thread(target=try_second_lock)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert second_error and isinstance(second_error[0], BlockingIOError)
        assert lock_path.stat().st_ino == held_ino
        assert lock_path.read_bytes() == held_bytes
    assert _snapshot(run_dir) == before or ".state.lock" in _snapshot(run_dir)


def test_production_lock_paths_refuse_intermediate_symlink_and_post_flock_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse
    import fcntl

    import dx.cli as cli_mod
    import dx.persist as persist_mod
    import dx.runstate as runstate_mod
    from dx.cli import cmd_resume_exec
    from dx.runstate import (
        IterationBudgetError,
        RunStateError,
        _probe_delivery_lock,
        authorize_iteration_extension,
    )

    real_run = _private_run(tmp_path, "prod-lock-run")
    transition_run(real_run, RunEvent.RUN_STARTED)
    parent_link = tmp_path / "via-parent"
    parent_link.symlink_to(tmp_path, target_is_directory=True)
    aliased = parent_link / "prod-lock-run"
    before = _snapshot(real_run)

    args = argparse.Namespace(
        run_dir=str(aliased),
        review_only=False,
        additional_iterations=None,
        env_file=None,
    )
    assert cmd_resume_exec(args) == 1
    assert _snapshot(real_run) == before
    assert not (real_run / ".resume.lock").exists()

    with pytest.raises((IterationBudgetError, RunStateError, PersistError), match="symlink"):
        authorize_iteration_extension(aliased, 1, origin="cli")
    assert _snapshot(real_run) == before
    assert not (real_run / ".resume.lock").exists()

    with pytest.raises(IterationBudgetError, match="symlink|cannot probe"):
        _probe_delivery_lock(aliased)
    assert _snapshot(real_run) == before
    assert not (real_run / ".delivery.lock").exists()

    # Post-flock inode swap on production flock callers.
    for module, lock_name, invoke in (
        (
            cli_mod,
            ".resume.lock",
            lambda: cmd_resume_exec(
                argparse.Namespace(
                    run_dir=str(real_run),
                    review_only=False,
                    additional_iterations=None,
                    env_file=None,
                )
            ),
        ),
        (
            runstate_mod,
            ".delivery.lock",
            lambda: _probe_delivery_lock(real_run),
        ),
    ):
        lock_path = real_run / lock_name
        if lock_path.exists():
            lock_path.unlink()
        lock_path.write_text("original\n", encoding="utf-8")
        os.chmod(lock_path, 0o600)
        original_ino = lock_path.stat().st_ino
        real_flock = module.fcntl.flock

        def flock_then_swap(fd: int, operation: int, _lp=lock_path, _rf=real_flock) -> None:
            _rf(fd, operation)
            os.unlink(_lp)
            replacement = _lp
            replacement.write_text("replacement\n", encoding="utf-8")
            os.chmod(replacement, 0o600)

        monkeypatch.setattr(module.fcntl, "flock", flock_then_swap)
        if lock_name == ".resume.lock":
            assert invoke() == 1
        else:
            with pytest.raises(IterationBudgetError, match="replaced after flock|inode|cannot probe"):
                invoke()
        monkeypatch.setattr(module.fcntl, "flock", real_flock)
        assert lock_path.stat().st_ino != original_ino

    # authorize_iteration_extension goes through run_scoped_lock (atomic.fcntl).
    import dx.atomic as atomic_mod

    lock_path = real_run / ".resume.lock"
    lock_path.write_text("auth-original\n", encoding="utf-8")
    os.chmod(lock_path, 0o600)
    original_ino = lock_path.stat().st_ino
    real_flock = atomic_mod.fcntl.flock

    def flock_then_swap_auth(fd: int, operation: int) -> None:
        real_flock(fd, operation)
        os.unlink(lock_path)
        lock_path.write_text("auth-replacement\n", encoding="utf-8")
        os.chmod(lock_path, 0o600)

    monkeypatch.setattr(atomic_mod.fcntl, "flock", flock_then_swap_auth)
    with pytest.raises((IterationBudgetError, ValueError, PersistError), match="replaced|inode|symlink"):
        authorize_iteration_extension(real_run, 1, origin="cli")
    monkeypatch.setattr(atomic_mod.fcntl, "flock", real_flock)
    assert lock_path.stat().st_ino != original_ino


def test_reports_validation_and_evidence_fail_closed_byte_for_byte(
    tmp_path: Path,
) -> None:
    from dx.runstate import RunStateError, _regular_report_present, attach_evidence
    from dx.schemas import FUTURE_SCHEMA_REFUSAL
    from dx.snapshot import (
        SnapshotError,
        _test_summary,
        build_snapshot_manifest,
        prepare_review_artifacts,
    )
    from dx.profile import ProjectProfile

    run_dir = _private_run(tmp_path, "fail-closed-io")

    # Zero-byte private report is corrupt.
    cursor = run_dir / "cursor-1.json"
    cursor.write_bytes(b"")
    os.chmod(cursor, 0o600)
    before = _snapshot(run_dir)
    with pytest.raises(RunStateError, match="empty or truncated"):
        _regular_report_present(cursor, label="cursor-1.json")
    assert _snapshot(run_dir) == before

    # Symlinked validation logs fail closed from _test_summary.
    executor = run_dir / "executor-1.json"
    secure_write_json(executor, _cursor_agent_result("ok"))
    real_log = run_dir / "validation-real.log"
    real_log.write_text("1 passed\n", encoding="utf-8")
    os.chmod(real_log, 0o600)
    linked = run_dir / "validation-evil.log"
    linked.symlink_to(real_log)
    before = _snapshot(run_dir)
    with pytest.raises(SnapshotError, match="validation input cannot be read safely"):
        _test_summary(run_dir, executor)
    assert _snapshot(run_dir) == before
    linked.unlink()

    # Symlinked validation results and malformed findings fail closed.
    repo, worktree, prep_run, base = _make_resume_run(tmp_path / "prep-fc")
    manifest = build_snapshot_manifest(worktree, base)
    profile = ProjectProfile()
    reviewer = prep_run / "reviewer.json"
    secure_write_json(
        reviewer,
        {
            "status": "APPROVED",
            "summary": "ok",
            "findings": [
                {
                    "severity": "low",
                    "title": "t",
                    "details": "d",
                    "files": [],
                }
            ],
            "tests_required": [],
        },
    )
    executor_report = prep_run / "executor.json"
    secure_write_json(executor_report, _cursor_agent_result("done"))
    result_real = prep_run / "validation-1-result.json"
    secure_write_json(
        result_real,
        {
            "schema_version": 1,
            "phase": "validation",
            "iteration": 1,
            "state": "completed",
            "reason": None,
            "exit_code": 0,
            "child_exit_code": 0,
            "elapsed_seconds": 1.0,
            "last_activity_at": "2026-07-25T00:00:00Z",
            "changed_files": 0,
            "finished_at": "2026-07-25T00:00:00Z",
        },
    )
    result_link = prep_run / "validation-2-result.json"
    result_link.symlink_to(result_real)
    before = _snapshot(prep_run)
    with pytest.raises(SnapshotError, match="validation result cannot be read safely"):
        prepare_review_artifacts(
            run_dir=prep_run,
            repo=repo,
            worktree=worktree,
            task_file="docs/tasks/DX-08A.md",
            task_id="DX-08A",
            task_slug="dx-08a",
            base_commit=base,
            iteration=1,
            max_iterations=3,
            executor_report=executor_report,
            reviewer_report=reviewer,
            reviewed_hash=manifest["snapshot_hash"],
            profile=profile,
        )
    assert _snapshot(prep_run) == before
    result_link.unlink()

    secure_write_json(
        reviewer,
        {
            "status": "APPROVED",
            "summary": "ok",
            "findings": [{"severity": "low", "title": "t"}],
            "tests_required": [],
        },
    )
    before = _snapshot(prep_run)
    with pytest.raises(SnapshotError, match="malformed nested reviewer finding"):
        prepare_review_artifacts(
            run_dir=prep_run,
            repo=repo,
            worktree=worktree,
            task_file="docs/tasks/DX-08A.md",
            task_id="DX-08A",
            task_slug="dx-08a",
            base_commit=base,
            iteration=1,
            max_iterations=3,
            executor_report=executor_report,
            reviewer_report=reviewer,
            reviewed_hash=manifest["snapshot_hash"],
            profile=profile,
        )
    assert _snapshot(prep_run) == before

    # Invalid evidence manifest + unseen source: byte-for-byte refusal.
    _repo, _worktree, evidence_run, _base = _make_resume_run(tmp_path / "ev-fc")
    secure_write_json(
        evidence_run / "evidence.json",
        {"schema_version": 99, "items": []},
    )
    unseen = tmp_path / "unseen-fc.txt"
    unseen.write_text("brand-new\n", encoding="utf-8")
    before = _snapshot(evidence_run)
    names_before = sorted(p.name for p in evidence_run.iterdir())
    with pytest.raises(RunStateError, match=FUTURE_SCHEMA_REFUSAL):
        attach_evidence(evidence_run, unseen)
    assert _snapshot(evidence_run) == before
    assert sorted(p.name for p in evidence_run.iterdir()) == names_before
    assert not (evidence_run / "evidence").exists()
    assert not (evidence_run / ".resume.lock").exists()


def test_resume_accepts_production_cursor_agent_envelope(
    tmp_path: Path,
) -> None:
    """Interrupted EXECUTING/REVIEWING resume must accept Cursor JSON stdout."""
    from dx.approval import compute_diff_hash
    from dx.runstate import plan_resume

    _repo, worktree, run_dir, base = _make_resume_run(tmp_path)
    transition_run(run_dir, RunEvent.RUN_STARTED)
    secure_write_json(run_dir / "cursor-1.json", _cursor_agent_result("executor done"))
    planned = plan_resume(run_dir)
    assert planned["resume_phase"] == "executor"
    assert planned["iteration"] == 1
    assert read_status(run_dir) == "EXECUTING"

    transition_run(run_dir, RunEvent.REVIEW_STARTED)
    assert read_status(run_dir) == "REVIEWING"
    # REVIEWING with a production cursor envelope and no snapshot yet still
    # resumes the reviewer (not the executor).
    planned = plan_resume(run_dir)
    assert planned["resume_phase"] == "reviewer"
    assert planned["iteration"] == 1

    snapshot = compute_diff_hash(worktree, base)
    secure_write_json(
        run_dir / "review-1-snapshot.json",
        {"schema_version": 1, "iteration": 1, "diff_hash": snapshot},
    )
    planned = plan_resume(run_dir)
    assert planned["resume_phase"] == "reviewer"
    assert planned["iteration"] == 1


def test_prepare_review_artifacts_fails_closed_on_unsafe_task_and_future_validation(
    tmp_path: Path,
) -> None:
    from dx.schemas import FUTURE_SCHEMA_REFUSAL
    from dx.snapshot import (
        MANIFEST_FILENAME,
        SUMMARY_FILENAME,
        SnapshotError,
        build_snapshot_manifest,
        prepare_review_artifacts,
    )
    from dx.profile import ProjectProfile

    repo, worktree, prep_run, base = _make_resume_run(tmp_path / "prep-task")
    profile = ProjectProfile()
    reviewer = prep_run / "reviewer.json"
    secure_write_json(
        reviewer,
        {
            "status": "APPROVED",
            "summary": "ok",
            "findings": [],
            "tests_required": [],
        },
    )
    executor_report = prep_run / "cursor-1.json"
    secure_write_json(executor_report, _cursor_agent_result("done"))

    # Seed published artifacts so we can prove refusals leave them unchanged.
    seed_manifest = prep_run / MANIFEST_FILENAME
    seed_summary = prep_run / SUMMARY_FILENAME
    secure_write_json(
        seed_manifest,
        {"schema_version": 1, "entries": [], "snapshot_hash": "x" * 64},
    )
    secure_write_json(seed_summary, {"schema_version": 1, "seed": True})

    # Symlinked task file must fail closed without republishing.
    real_task = worktree / "docs" / "tasks" / "DX-08A.md"
    decoy_task = worktree / "docs" / "tasks" / "decoy.md"
    decoy_task.write_text("# decoy\n", encoding="utf-8")
    real_task.unlink()
    real_task.symlink_to(decoy_task)
    manifest = build_snapshot_manifest(worktree, base)
    before = _snapshot(prep_run)
    with pytest.raises(SnapshotError, match="task file cannot be read safely|symlink"):
        prepare_review_artifacts(
            run_dir=prep_run,
            repo=repo,
            worktree=worktree,
            task_file="docs/tasks/DX-08A.md",
            task_id="DX-08A",
            task_slug="dx-08a",
            base_commit=base,
            iteration=1,
            max_iterations=3,
            executor_report=executor_report,
            reviewer_report=reviewer,
            reviewed_hash=manifest["snapshot_hash"],
            profile=profile,
        )
    assert _snapshot(prep_run) == before
    assert seed_manifest.read_bytes() == before[MANIFEST_FILENAME]
    assert seed_summary.read_bytes() == before[SUMMARY_FILENAME]

    # Restore a regular task file for the future-schema validation result case.
    real_task.unlink()
    real_task.write_text("# DX-08A — title\n", encoding="utf-8")
    manifest = build_snapshot_manifest(worktree, base)

    future = prep_run / "validation-9-result.json"
    secure_write_json(
        future,
        {
            "schema_version": 99,
            "phase": "validation",
            "iteration": 1,
            "state": "completed",
            "reason": None,
            "exit_code": 0,
            "child_exit_code": 0,
            "elapsed_seconds": 1.0,
            "last_activity_at": "2026-07-25T00:00:00Z",
            "changed_files": 0,
            "finished_at": "2026-07-25T00:00:00Z",
        },
    )
    before = _snapshot(prep_run)
    with pytest.raises(SnapshotError, match=FUTURE_SCHEMA_REFUSAL):
        prepare_review_artifacts(
            run_dir=prep_run,
            repo=repo,
            worktree=worktree,
            task_file="docs/tasks/DX-08A.md",
            task_id="DX-08A",
            task_slug="dx-08a",
            base_commit=base,
            iteration=1,
            max_iterations=3,
            executor_report=executor_report,
            reviewer_report=reviewer,
            reviewed_hash=manifest["snapshot_hash"],
            profile=profile,
        )
    assert _snapshot(prep_run) == before
    assert seed_manifest.read_bytes() == before[MANIFEST_FILENAME]
    assert seed_summary.read_bytes() == before[SUMMARY_FILENAME]


def _valid_reviewer_payload(**overrides: object) -> dict:
    payload: dict = {
        "status": "APPROVED",
        "summary": "ok",
        "findings": [],
        "tests_required": [],
    }
    payload.update(overrides)
    return payload


def _chmod_world_readable(path: Path, mode: int) -> None:
    os.chmod(path, mode)
    assert stat.S_IMODE(path.stat().st_mode) == mode


def test_production_report_paths_reject_insecure_modes_byte_for_byte(
    tmp_path: Path,
) -> None:
    """0644/0666 reviewer/executor/validation inputs fail closed on production paths."""
    from dx.cli import main as cli_main
    from dx.snapshot import (
        MANIFEST_FILENAME,
        SUMMARY_FILENAME,
        SnapshotError,
        _test_summary,
        build_snapshot_manifest,
        prepare_review_artifacts,
    )
    from dx.profile import ProjectProfile

    run_dir = _private_run(tmp_path, "mode-io")
    reviewer = run_dir / "review.json"
    secure_write_json(reviewer, _valid_reviewer_payload())
    assert cli_main(["review-status", "--file", str(reviewer)]) == 0
    for mode in (0o644, 0o666):
        _chmod_world_readable(reviewer, mode)
        before = reviewer.read_bytes()
        assert cli_main(["review-status", "--file", str(reviewer)]) == 1
        assert reviewer.read_bytes() == before
        assert stat.S_IMODE(reviewer.stat().st_mode) == mode
    os.chmod(reviewer, 0o600)

    repo, worktree, prep_run, base = _make_resume_run(tmp_path / "prep-mode")
    profile = ProjectProfile()
    manifest = build_snapshot_manifest(worktree, base)
    seed_manifest = prep_run / MANIFEST_FILENAME
    seed_summary = prep_run / SUMMARY_FILENAME
    secure_write_json(
        seed_manifest,
        {"schema_version": 1, "entries": [], "snapshot_hash": "x" * 64},
    )
    secure_write_json(seed_summary, {"schema_version": 1, "seed": True})
    private_reviewer = prep_run / "reviewer.json"
    private_executor = prep_run / "cursor-1.json"
    secure_write_json(private_reviewer, _valid_reviewer_payload())
    secure_write_json(private_executor, _cursor_agent_result("done"))
    validation_log = prep_run / "validation-1.log"
    validation_log.write_text("1 passed\n", encoding="utf-8")
    os.chmod(validation_log, 0o600)

    def _invoke() -> None:
        prepare_review_artifacts(
            run_dir=prep_run,
            repo=repo,
            worktree=worktree,
            task_file="docs/tasks/DX-08A.md",
            task_id="DX-08A",
            task_slug="dx-08a",
            base_commit=base,
            iteration=1,
            max_iterations=3,
            executor_report=private_executor,
            reviewer_report=private_reviewer,
            reviewed_hash=manifest["snapshot_hash"],
            profile=profile,
        )

    for mode in (0o644, 0o666):
        _chmod_world_readable(private_reviewer, mode)
        before = _snapshot(prep_run)
        with pytest.raises(SnapshotError, match="invalid reviewer report|insecure mode"):
            _invoke()
        assert _snapshot(prep_run) == before
        assert seed_manifest.read_bytes() == before[MANIFEST_FILENAME]
        assert seed_summary.read_bytes() == before[SUMMARY_FILENAME]
        assert private_reviewer.read_bytes() == before[private_reviewer.name]
        assert stat.S_IMODE(private_reviewer.stat().st_mode) == mode
    os.chmod(private_reviewer, 0o600)

    for mode in (0o644, 0o666):
        _chmod_world_readable(private_executor, mode)
        before = _snapshot(prep_run)
        with pytest.raises(
            SnapshotError, match="executor report cannot be read safely|insecure mode"
        ):
            _invoke()
        assert _snapshot(prep_run) == before
        assert seed_manifest.read_bytes() == before[MANIFEST_FILENAME]
        assert seed_summary.read_bytes() == before[SUMMARY_FILENAME]
        assert private_executor.read_bytes() == before[private_executor.name]
        assert stat.S_IMODE(private_executor.stat().st_mode) == mode
    os.chmod(private_executor, 0o600)

    for mode in (0o644, 0o666):
        _chmod_world_readable(validation_log, mode)
        before = _snapshot(prep_run)
        with pytest.raises(
            SnapshotError, match="validation input cannot be read safely|insecure mode"
        ):
            _test_summary(prep_run, private_executor)
        with pytest.raises(
            SnapshotError, match="validation input cannot be read safely|insecure mode"
        ):
            _invoke()
        assert _snapshot(prep_run) == before
        assert seed_manifest.read_bytes() == before[MANIFEST_FILENAME]
        assert seed_summary.read_bytes() == before[SUMMARY_FILENAME]
        assert validation_log.read_bytes() == before[validation_log.name]
        assert stat.S_IMODE(validation_log.stat().st_mode) == mode
    os.chmod(validation_log, 0o600)

    validation_result = prep_run / "validation-1-result.json"
    secure_write_json(
        validation_result,
        {
            "schema_version": 1,
            "phase": "validation",
            "iteration": 1,
            "state": "completed",
            "reason": None,
            "exit_code": 0,
            "child_exit_code": 0,
            "elapsed_seconds": 1.0,
            "last_activity_at": "2026-07-25T00:00:00Z",
            "changed_files": 0,
            "finished_at": "2026-07-25T00:00:00Z",
        },
    )
    for mode in (0o644, 0o666):
        _chmod_world_readable(validation_result, mode)
        before = _snapshot(prep_run)
        with pytest.raises(
            SnapshotError,
            match="validation result cannot be read safely|insecure mode",
        ):
            _invoke()
        assert _snapshot(prep_run) == before
        assert seed_manifest.read_bytes() == before[MANIFEST_FILENAME]
        assert seed_summary.read_bytes() == before[SUMMARY_FILENAME]
        assert validation_result.read_bytes() == before[validation_result.name]
        assert stat.S_IMODE(validation_result.stat().st_mode) == mode


def test_prepare_review_artifacts_rejects_malformed_reviewer_and_executor_envelopes(
    tmp_path: Path,
) -> None:
    """Direct prepare_review_artifacts must not publish from malformed contracts."""
    from dx.snapshot import (
        MANIFEST_FILENAME,
        SUMMARY_FILENAME,
        SnapshotError,
        build_snapshot_manifest,
        prepare_review_artifacts,
    )
    from dx.profile import ProjectProfile

    repo, worktree, prep_run, base = _make_resume_run(tmp_path / "prep-contract")
    profile = ProjectProfile()
    manifest = build_snapshot_manifest(worktree, base)
    seed_manifest = prep_run / MANIFEST_FILENAME
    seed_summary = prep_run / SUMMARY_FILENAME
    secure_write_json(
        seed_manifest,
        {"schema_version": 1, "entries": [], "snapshot_hash": "y" * 64},
    )
    secure_write_json(seed_summary, {"schema_version": 1, "seed": True})
    reviewer = prep_run / "reviewer.json"
    executor = prep_run / "cursor-1.json"
    secure_write_json(executor, _cursor_agent_result("done"))

    def _invoke() -> None:
        prepare_review_artifacts(
            run_dir=prep_run,
            repo=repo,
            worktree=worktree,
            task_file="docs/tasks/DX-08A.md",
            task_id="DX-08A",
            task_slug="dx-08a",
            base_commit=base,
            iteration=1,
            max_iterations=3,
            executor_report=executor,
            reviewer_report=reviewer,
            reviewed_hash=manifest["snapshot_hash"],
            profile=profile,
        )

    malformed_reviewers = [
        {"status": "APPROVED", "summary": "ok"},  # missing fields
        {
            "status": "APPROVED",
            "summary": "ok",
            "findings": [],
            "tests_required": [],
            "extra": True,
        },
        {
            "status": "SHIP_IT",
            "summary": "ok",
            "findings": [],
            "tests_required": [],
        },
        {
            "status": "APPROVED",
            "summary": "ok",
            "findings": [{"severity": "low", "title": "t", "details": "d"}],
            "tests_required": [],
        },  # finding missing files
        {
            "status": "APPROVED",
            "summary": "ok",
            "findings": "not-a-list",
            "tests_required": [],
        },
    ]
    for bad in malformed_reviewers:
        secure_write_json(reviewer, bad)
        before = _snapshot(prep_run)
        with pytest.raises(
            SnapshotError,
            match=(
                "missing or unknown fields|invalid reviewer status|"
                "malformed nested reviewer finding|invalid reviewer findings"
            ),
        ):
            _invoke()
        assert _snapshot(prep_run) == before
        assert seed_manifest.read_bytes() == before[MANIFEST_FILENAME]
        assert seed_summary.read_bytes() == before[SUMMARY_FILENAME]

    secure_write_json(reviewer, _valid_reviewer_payload())
    malformed_executors = [
        {"summary": "synthetic fixture is not production"},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 1,
            "duration_api_ms": 1,
            "result": "done",
            "session_id": "s",
            "request_id": "r",
            "usage": {},
            "extra": True,
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": "nope",
            "duration_ms": 1,
            "duration_api_ms": 1,
            "result": "done",
            "session_id": "s",
            "request_id": "r",
            "usage": {},
        },
        {
            "type": "assistant",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 1,
            "duration_api_ms": 1,
            "result": "done",
            "session_id": "s",
            "request_id": "r",
            "usage": {},
        },
    ]
    for bad in malformed_executors:
        secure_write_json(executor, bad)
        before = _snapshot(prep_run)
        with pytest.raises(
            SnapshotError,
            match=(
                "executor report has unknown fields|"
                "executor report missing required fields|"
                "executor report field types are invalid|"
                "executor report must be a JSON object"
            ),
        ):
            _invoke()
        assert _snapshot(prep_run) == before
        assert seed_manifest.read_bytes() == before[MANIFEST_FILENAME]
        assert seed_summary.read_bytes() == before[SUMMARY_FILENAME]
        assert executor.read_bytes() == before[executor.name]


def _seed_resume_lock(run_dir: Path) -> Path:
    """Establish the authorize concurrency fence before a refusal probe.

    ``authorize_iteration_extension`` always takes ``.resume.lock``. Seeding it
    first lets refusal tests prove full run-directory byte-for-byte invariance
    (including the lock entry) without weakening that fence.
    """
    lock_path = run_dir / ".resume.lock"
    if not lock_path.exists():
        lock_path.write_bytes(b"")
        os.chmod(lock_path, 0o600)
    assert lock_path.is_file()
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
    return lock_path


def _authorize_critical_fingerprint(run_dir: Path) -> dict[str, object]:
    """Complete run-directory snapshot for authorize refusal invariance.

    Includes lock entries. Callers must seed ``.resume.lock`` first so the
    concurrency protocol does not add a new directory entry on refusal.
    """
    from dx.runstate import ITERATION_BUDGET

    entries: dict[str, object] = {}
    for path in sorted(run_dir.iterdir()):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            entries[path.name] = {
                "kind": "symlink",
                "mode": stat.S_IMODE(info.st_mode),
                "target": os.readlink(path),
            }
        elif stat.S_ISREG(info.st_mode):
            entries[path.name] = {
                "kind": "file",
                "mode": stat.S_IMODE(info.st_mode),
                "nlink": info.st_nlink,
                "ino": info.st_ino,
                "bytes": path.read_bytes(),
            }
        else:
            entries[path.name] = {
                "kind": "other",
                "mode": stat.S_IMODE(info.st_mode),
            }
    return {
        "status": read_status(run_dir),
        "budget_exists": (run_dir / ITERATION_BUDGET).exists(),
        "names": sorted(entries),
        "entries": entries,
    }


def _replace_cursor_report(run_dir: Path, *, mode: int = 0o600, raw: bytes | None = None, payload: dict | None = None) -> Path:
    cursor = run_dir / "cursor-3.json"
    if cursor.exists() or cursor.is_symlink():
        cursor.unlink()
    if raw is not None:
        cursor.write_bytes(raw)
    else:
        assert payload is not None
        secure_write_json(cursor, payload)
    os.chmod(cursor, mode)
    return cursor


@pytest.mark.parametrize(
    "case",
    [
        "malformed",
        "truncated",
        "synthetic_summary",
        "unknown_fields",
        "missing_fields",
        "invalid_types",
        "symlink",
        "hard_link",
        "owner",
        "mode_0644",
        "mode_0666",
        "mode_0400",
        "mode_0500",
        "mode_0700",
        "empty",
    ],
)
def test_authorize_iteration_refuses_insecure_cursor_report(
    tmp_path: Path,
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DX-08A1: insecure cursor-<n>.json must not mutate the run directory."""
    from test_agent_dx04 import make_exhausted_run
    from dx.persist import PersistError
    from dx.runstate import ITERATION_BUDGET, IterationBudgetError, authorize_iteration_extension

    env = make_exhausted_run(tmp_path / case)
    run_dir = env["run_dir"]
    cursor = run_dir / "cursor-3.json"
    decoy = run_dir / "cursor-decoy.json"
    # Intended precondition: resume lock fence already present so refusal can
    # prove full directory invariance including lock entries.
    _seed_resume_lock(run_dir)

    if case == "malformed":
        _replace_cursor_report(run_dir, raw=b"not json\n")
    elif case == "truncated":
        _replace_cursor_report(run_dir, raw=b'{"type": "result", "subtype":')
    elif case == "synthetic_summary":
        _replace_cursor_report(run_dir, payload={"summary": "not production"})
    elif case == "unknown_fields":
        _replace_cursor_report(
            run_dir,
            payload=_cursor_agent_result(extra=True),
        )
    elif case == "missing_fields":
        payload = _cursor_agent_result()
        del payload["usage"]
        _replace_cursor_report(run_dir, payload=payload)
    elif case == "invalid_types":
        _replace_cursor_report(
            run_dir,
            payload=_cursor_agent_result(is_error="nope"),  # type: ignore[arg-type]
        )
    elif case == "symlink":
        secure_write_json(decoy, _cursor_agent_result("decoy"))
        cursor.unlink()
        cursor.symlink_to(decoy)
    elif case == "hard_link":
        alias = run_dir / "cursor-3.alias"
        os.link(cursor, alias)
        assert cursor.lstat().st_nlink > 1
    elif case == "owner":
        import dx.persist as persist_mod

        real_check = persist_mod._check_owner_mode

        def _reject_cursor_owner(
            info: os.stat_result,
            path: Path,
            *,
            require_private: bool,
            allow_exec: bool,
            expected_owner: int | None = None,
        ) -> None:
            if Path(path).name == "cursor-3.json":
                raise PersistError(
                    f"unexpected owner for {path}: uid={os.geteuid() + 1}, "
                    f"expected={os.geteuid()}"
                )
            return real_check(
                info,
                path,
                require_private=require_private,
                allow_exec=allow_exec,
                expected_owner=expected_owner,
            )

        monkeypatch.setattr(persist_mod, "_check_owner_mode", _reject_cursor_owner)
    elif case == "mode_0644":
        os.chmod(cursor, 0o644)
    elif case == "mode_0666":
        os.chmod(cursor, 0o666)
    elif case == "mode_0400":
        # Valid envelope content, but mode is not exactly 0600.
        _replace_cursor_report(run_dir, payload=_cursor_agent_result(), mode=0o400)
    elif case == "mode_0500":
        _replace_cursor_report(run_dir, payload=_cursor_agent_result(), mode=0o500)
    elif case == "mode_0700":
        _replace_cursor_report(run_dir, payload=_cursor_agent_result(), mode=0o700)
    elif case == "empty":
        _replace_cursor_report(run_dir, raw=b"")
    else:
        raise AssertionError(case)

    before = _authorize_critical_fingerprint(run_dir)
    assert ".resume.lock" in before["names"]
    with pytest.raises(
        IterationBudgetError,
        match=(
            r"executor report|cursor-3\.json|symlink|hard link|insecure mode|"
            r"unexpected owner|invalid JSON|missing or empty|contract is invalid|"
            r"unknown fields|missing required|field types|expected 0o600"
        ),
    ):
        authorize_iteration_extension(run_dir, 3, origin="cli")
    assert _authorize_critical_fingerprint(run_dir) == before
    assert not (run_dir / ITERATION_BUDGET).exists()
    assert read_status(run_dir) == "BLOCKED"
    if case == "symlink":
        assert cursor.is_symlink()
    if case.startswith("mode_"):
        expected_mode = {
            "mode_0644": 0o644,
            "mode_0666": 0o666,
            "mode_0400": 0o400,
            "mode_0500": 0o500,
            "mode_0700": 0o700,
        }[case]
        assert stat.S_IMODE(cursor.lstat().st_mode) == expected_mode


def test_authorize_iteration_accepts_production_cursor_envelope(
    tmp_path: Path,
) -> None:
    """DX-08A1: a real Cursor Agent envelope still authorizes the extension."""
    from test_agent_dx04 import make_exhausted_run
    from dx.runstate import ITERATION_BUDGET, authorize_iteration_extension, load_iteration_budget

    env = make_exhausted_run(tmp_path / "valid-envelope")
    run_dir = env["run_dir"]
    _replace_cursor_report(run_dir, payload=_cursor_agent_result("executor iteration 3"))
    before_names = sorted(p.name for p in run_dir.iterdir())
    result = authorize_iteration_extension(run_dir, 3, origin="cli")
    assert result["result"] == "authorized"
    assert result["previous_limit"] == 3
    assert result["effective_limit"] == 6
    budget = load_iteration_budget(run_dir, 3)
    assert budget["effective_limit"] == 6
    assert (run_dir / ITERATION_BUDGET).is_file()
    assert read_status(run_dir) == "CHANGES_REQUESTED"
    assert set(before_names).issubset({p.name for p in run_dir.iterdir()})
