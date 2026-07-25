"""DX-08 secure persistence, transactions, audit, and migrations."""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import socket
import stat
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS = REPO_ROOT / "scripts" / "agents"
sys.path.insert(0, str(AGENTS))

from dx.atomic import atomic_write_json, exclusive_write_json, read_json  # noqa: E402
from dx.audit import (  # noqa: E402
    GENESIS_PREV_HASH,
    append_audit_event,
    compute_entry_hash,
    load_audit_trail,
    validate_audit_trail,
)
from dx.cli import main as cli_main  # noqa: E402
from dx.migrate import (  # noqa: E402
    MIGRATIONS,
    MigrationError,
    inspect_run,
    migrate_run,
    verify_run_state,
)
from dx.persist import (  # noqa: E402
    PersistError,
    apply_secure_umask,
    assert_contained,
    canonical_json_hash,
    cleanup_orphan_temps,
    fsync_directory,
    secure_read_json,
    secure_write_bytes,
    secure_write_json,
)
from dx.schemas import (  # noqa: E402
    ITERATION_BUDGET_SCHEMA_VERSION,
    PERSISTENCE_SCHEMA_VERSION,
    RUNNER_VERSION,
)
from dx.state_machine import RunEvent, read_status, transition_run  # noqa: E402
from dx.txn import (  # noqa: E402
    JOURNAL_COMMITTING,
    LogicalTransaction,
    TransactionError,
    recover_run_transactions,
    validate_journal,
    _write_journal,
)


def _private_run(tmp_path: Path, name: str = "run-1") -> Path:
    run_dir = tmp_path / name
    run_dir.mkdir(mode=0o700)
    return run_dir


def _chmod_private(path: Path) -> None:
    os.chmod(path, 0o600)


def _journal_doc(
    run_dir: Path,
    *,
    phase: str,
    event: str,
    status_event: str,
    previous_state: str | None,
    artifacts: dict,
    exclusive: list[str] | None = None,
    origin: str = "runner",
    created_at: str = "2026-07-25T00:00:00Z",
) -> dict:
    hashes = {name: canonical_json_hash(payload) for name, payload in artifacts.items()}
    return {
        "schema_version": 1,
        "runner_version": RUNNER_VERSION,
        "run_id": run_dir.name,
        "phase": phase,
        "event": event,
        "status_event": status_event,
        "previous_state": previous_state,
        "artifact_hashes": hashes,
        "artifacts": artifacts,
        "exclusive_artifacts": exclusive or [],
        "origin": origin,
        "created_at": created_at,
    }


# ---------------------------------------------------------------------------
# Secure API: type / mode / owner / containment / inode / dir fsync
# ---------------------------------------------------------------------------


def test_secure_read_rejects_symlink_fifo_socket_and_device(tmp_path: Path) -> None:
    run_dir = _private_run(tmp_path)
    target = run_dir / "payload.json"
    secure_write_json(target, {"ok": True})
    link = run_dir / "link.json"
    link.symlink_to(target)
    with pytest.raises(PersistError, match="symlink"):
        secure_read_json(link, require_private=True)

    fifo = run_dir / "fifo.json"
    os.mkfifo(fifo)
    with pytest.raises(PersistError, match="FIFO"):
        secure_read_json(fifo, require_private=True)

    sock_path = run_dir / "sock.json"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(sock_path))
        with pytest.raises(PersistError, match="socket"):
            secure_read_json(sock_path, require_private=True)
    finally:
        sock.close()

    # Character device path (no privilege needed to lstat).
    with pytest.raises(PersistError, match="device"):
        secure_read_json(Path("/dev/null"), require_private=False)


def test_secure_read_rejects_insecure_mode_and_hardlink(tmp_path: Path) -> None:
    run_dir = _private_run(tmp_path)
    path = run_dir / "wide.json"
    secure_write_json(path, {"a": 1})
    os.chmod(path, 0o644)
    with pytest.raises(PersistError, match="insecure mode"):
        secure_read_json(path, require_private=True)
    # Production read_json defaults to require_private=True.
    with pytest.raises(ValueError, match="insecure mode"):
        read_json(path)

    private = run_dir / "private.json"
    secure_write_json(private, {"a": 1})
    hard = run_dir / "hard.json"
    os.link(private, hard)
    with pytest.raises(PersistError, match="hard link"):
        secure_read_json(private, require_private=True)


def test_secure_write_refuses_to_silently_replace_symlink(tmp_path: Path) -> None:
    run_dir = _private_run(tmp_path)
    real = run_dir / "real.json"
    secure_write_json(real, {"ok": True})
    link = run_dir / "link.json"
    link.symlink_to(real)
    with pytest.raises(PersistError, match="symlink"):
        secure_write_json(link, {"repaired": True})
    assert link.is_symlink()
    assert secure_read_json(real) == {"ok": True}


def test_secure_write_refuses_hardlinked_insecure_target(tmp_path: Path) -> None:
    run_dir = _private_run(tmp_path)
    path = run_dir / "linked.json"
    secure_write_json(path, {"a": 1})
    alias = run_dir / "alias.json"
    os.link(path, alias)
    with pytest.raises(PersistError, match="hard link"):
        secure_write_json(path, {"b": 2})


def test_secure_write_fsyncs_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _private_run(tmp_path)
    synced: list[str] = []
    original = fsync_directory

    def tracking(path: Path | str) -> None:
        synced.append(str(path))
        return original(path)

    monkeypatch.setattr("dx.persist.fsync_directory", tracking)
    secure_write_json(run_dir / "doc.json", {"x": 1})
    assert any(Path(item).resolve() == run_dir.resolve() for item in synced)


def test_exclusive_write_fsyncs_after_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _private_run(tmp_path)
    synced: list[str] = []
    original = fsync_directory

    def tracking(path: Path | str) -> None:
        synced.append(str(path))
        return original(path)

    monkeypatch.setattr("dx.persist.fsync_directory", tracking)
    assert exclusive_write_json(run_dir / "once.json", {"winner": 1}) is True
    assert any(Path(item).resolve() == run_dir.resolve() for item in synced)


def test_containment_rejects_escape(tmp_path: Path) -> None:
    root = _private_run(tmp_path, "root")
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(PersistError, match="escapes"):
        assert_contained(outside, root)


def test_secure_write_with_containment_rejects_escape(tmp_path: Path) -> None:
    root = _private_run(tmp_path, "root")
    outside = tmp_path / "escape.json"
    with pytest.raises(PersistError, match="escapes"):
        secure_write_json(outside, {"x": 1}, containment_root=root)


def test_inode_exchange_detected_on_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _private_run(tmp_path)
    path = run_dir / "swap.json"
    secure_write_json(path, {"v": 1})
    import dx.persist as persist_mod

    real_fstat = os.fstat

    def lying_fstat(fd: int) -> os.stat_result:
        st = real_fstat(fd)
        # Simulate TOCTOU inode exchange between lstat and fstat.
        return os.stat_result(
            (
                st.st_mode,
                st.st_ino + 999,
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
        persist_mod.secure_open_read(path, max_bytes=1024)


def test_wrong_owner_rejected(tmp_path: Path) -> None:
    run_dir = _private_run(tmp_path)
    path = run_dir / "owned.json"
    secure_write_json(path, {"a": 1})
    with pytest.raises(PersistError, match="unexpected owner"):
        secure_read_json(path, expected_owner=os.geteuid() + 1)


def test_shared_state_root_mode_rejected(tmp_path: Path) -> None:
    shared = tmp_path / "shared-root"
    shared.mkdir(mode=0o755)
    os.chmod(shared, 0o755)
    from dx.persist import ensure_private_dir

    with pytest.raises(PersistError, match="insecure mode"):
        ensure_private_dir(shared)


def test_unsupported_directory_fsync_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _private_run(tmp_path)

    def boom(path: Path | str) -> None:
        raise PersistError(
            f"directory fsync unsupported or failed for {path}: "
            "Use a local POSIX filesystem"
        )

    monkeypatch.setattr("dx.persist.fsync_directory", boom)
    with pytest.raises(PersistError, match="directory fsync unsupported"):
        secure_write_json(run_dir / "doc.json", {"x": 1})


def test_orphan_temp_cleanup_validates_name_owner(tmp_path: Path) -> None:
    run_dir = _private_run(tmp_path)
    good = run_dir / ".status.tmp"
    good.write_text("partial", encoding="utf-8")
    os.chmod(good, 0o600)
    bad_name = run_dir / "not-a-temp.json"
    bad_name.write_text("{}", encoding="utf-8")
    removed = cleanup_orphan_temps(run_dir)
    assert ".status.tmp" in removed
    assert bad_name.exists()


def test_closed_schema_rejected_on_read(tmp_path: Path) -> None:
    run_dir = _private_run(tmp_path)
    path = run_dir / "closed.json"
    secure_write_json(path, {"a": 1, "b": 2})
    with pytest.raises(PersistError, match="schema mismatch"):
        secure_read_json(path, allowed_keys=frozenset({"a"}))


# ---------------------------------------------------------------------------
# Transactions / crash frontiers
# ---------------------------------------------------------------------------


def test_transaction_blocked_plus_failure_recovers_after_journal(
    tmp_path: Path,
) -> None:
    run_dir = _private_run(tmp_path)
    transition_run(run_dir, RunEvent.RUN_STARTED)
    payload = {
        "schema_version": 1,
        "reason": "boom",
        "phase": "loop",
        "iteration": 1,
        "report": None,
        "recorded_at": "2026-07-25T00:00:00Z",
    }
    _write_journal(
        run_dir,
        _journal_doc(
            run_dir,
            phase=JOURNAL_COMMITTING,
            event=RunEvent.RUN_BLOCKED.value,
            status_event=RunEvent.RUN_BLOCKED.value,
            previous_state="EXECUTING",
            artifacts={"failure.json": payload},
        ),
    )
    secure_write_json(run_dir / "failure.json", payload)
    recovered = recover_run_transactions(run_dir)
    assert recovered["result"] == "recovered"
    assert (run_dir / "status").read_text(encoding="utf-8").strip() == "BLOCKED"
    assert not (run_dir / ".txn.json").exists()
    assert not (run_dir / "human_approval_decision.json").exists()
    trail = load_audit_trail(run_dir)
    assert trail["events"][-1]["event"] == RunEvent.RUN_BLOCKED.value
    assert trail["events"][-1]["previous_state"] == "EXECUTING"
    assert trail["events"][-1]["timestamp"] == "2026-07-25T00:00:00Z"


def test_recovery_after_status_before_audit_clears_journal_and_writes_audit(
    tmp_path: Path,
) -> None:
    """Crash after artifact+status but before audit must not report clean success
    while leaving `.txn.json` and a missing audit event."""
    run_dir = _private_run(tmp_path)
    transition_run(run_dir, RunEvent.RUN_STARTED)
    payload = {
        "schema_version": 1,
        "reason": "x",
        "phase": "loop",
        "iteration": 0,
        "report": None,
        "recorded_at": "2026-07-25T00:00:00Z",
    }
    # Simulate: artifacts + BLOCKED status present, journal still committing,
    # no audit event for run_blocked yet.
    secure_write_json(run_dir / "failure.json", payload)
    (run_dir / "status").write_text("BLOCKED\n", encoding="utf-8")
    _chmod_private(run_dir / "status")
    _write_journal(
        run_dir,
        _journal_doc(
            run_dir,
            phase=JOURNAL_COMMITTING,
            event=RunEvent.RUN_BLOCKED.value,
            status_event=RunEvent.RUN_BLOCKED.value,
            previous_state="EXECUTING",
            artifacts={"failure.json": payload},
            created_at="2026-07-25T00:00:00Z",
        ),
    )
    recovered = recover_run_transactions(run_dir)
    assert recovered["result"] == "recovered"
    assert not (run_dir / ".txn.json").exists()
    trail = load_audit_trail(run_dir)
    blocked = [e for e in trail["events"] if e["event"] == RunEvent.RUN_BLOCKED.value]
    assert blocked
    assert blocked[-1]["previous_state"] == "EXECUTING"
    assert blocked[-1]["timestamp"] == "2026-07-25T00:00:00Z"
    assert blocked[-1]["new_state"] == "BLOCKED"


def test_approval_request_and_outbox_commit_atomically(tmp_path: Path) -> None:
    run_dir = _private_run(tmp_path)
    transition_run(run_dir, RunEvent.RUN_STARTED)
    transition_run(run_dir, RunEvent.REVIEW_STARTED)
    transition_run(run_dir, RunEvent.REVIEW_APPROVED)
    request = {
        "schema_version": 1,
        "technical_status": "APPROVED",
        "task": "docs/tasks/DX-08.md",
        "task_id": "DX-08",
        "run_id": run_dir.name,
        "base_commit": "abc",
        "worktree": "/tmp/wt",
        "review_report": "review.json",
        "diff_hash": "a" * 64,
        "callback_token": "b" * 32,
        "token_consumed": False,
        "created_at": "2026-07-25T00:00:00Z",
    }
    notify = {
        "schema_version": 1,
        "kind": "awaiting_human_approval",
        "run_id": run_dir.name,
        "status": "AWAITING_HUMAN_APPROVAL",
        "summary": "await",
        "report_hint": "review.json",
        "created_at": "2026-07-25T00:00:00Z",
        "notification_id": "n1",
        "sent_at": None,
        "offer_approval_button": True,
        "callback_token": request["callback_token"],
        "diff_hash": request["diff_hash"],
        "task_id": "DX-08",
        "messages": ["await"],
        "sent_message_ids": [],
    }
    # Crash after request+outbox written, before status.
    artifacts = {
        "human_approval_request.json": request,
        "telegram_notify.json": notify,
    }
    _write_journal(
        run_dir,
        _journal_doc(
            run_dir,
            phase=JOURNAL_COMMITTING,
            event=RunEvent.APPROVAL_REQUESTED.value,
            status_event=RunEvent.APPROVAL_REQUESTED.value,
            previous_state="APPROVED",
            artifacts=artifacts,
        ),
    )
    secure_write_json(run_dir / "human_approval_request.json", request)
    secure_write_json(run_dir / "telegram_notify.json", notify)
    recovered = recover_run_transactions(run_dir)
    assert recovered["result"] == "recovered"
    assert read_status(run_dir) == "AWAITING_HUMAN_APPROVAL"
    assert not (run_dir / ".txn.json").exists()


def test_human_decision_crash_before_status_recovers(tmp_path: Path) -> None:
    run_dir = _private_run(tmp_path)
    transition_run(run_dir, RunEvent.RUN_STARTED)
    transition_run(run_dir, RunEvent.REVIEW_STARTED)
    transition_run(run_dir, RunEvent.REVIEW_APPROVED)
    request = {
        "schema_version": 1,
        "technical_status": "APPROVED",
        "task": "docs/tasks/DX-08.md",
        "task_id": "DX-08",
        "run_id": run_dir.name,
        "base_commit": "abc",
        "worktree": "/tmp/wt",
        "review_report": "review.json",
        "diff_hash": "a" * 64,
        "callback_token": "b" * 32,
        "token_consumed": False,
        "created_at": "2026-07-25T00:00:00Z",
    }
    txn = LogicalTransaction(
        run_dir=run_dir,
        event=RunEvent.APPROVAL_REQUESTED.value,
        status_event=RunEvent.APPROVAL_REQUESTED,
        origin="runner",
    )
    txn.add_json("human_approval_request.json", request)
    txn.commit()
    # Force AWAITING without going through full approval API.
    (run_dir / "status").write_text("AWAITING_HUMAN_APPROVAL\n", encoding="utf-8")
    _chmod_private(run_dir / "status")
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
    # Mark the durable request consumed so recovery binds decision↔request.
    request["token_consumed"] = True
    atomic_write_json(run_dir / "human_approval_request.json", request)
    _write_journal(
        run_dir,
        _journal_doc(
            run_dir,
            phase=JOURNAL_COMMITTING,
            event=RunEvent.HUMAN_APPROVED.value,
            status_event=RunEvent.HUMAN_APPROVED.value,
            previous_state="AWAITING_HUMAN_APPROVAL",
            artifacts={
                "human_approval_request.json": request,
                "human_approval_decision.json": decision,
            },
            exclusive=["human_approval_decision.json"],
            origin="bridge",
        ),
    )
    assert exclusive_write_json(run_dir / "human_approval_decision.json", decision)
    recovered = recover_run_transactions(run_dir)
    assert recovered["result"] == "recovered"
    assert read_status(run_dir) == "HUMAN_APPROVED"
    assert not (run_dir / ".txn.json").exists()


def test_transaction_preparing_aborts_without_promotion(tmp_path: Path) -> None:
    run_dir = _private_run(tmp_path)
    _write_journal(
        run_dir,
        _journal_doc(
            run_dir,
            phase="preparing",
            event=RunEvent.RUN_BLOCKED.value,
            status_event=RunEvent.RUN_BLOCKED.value,
            previous_state=None,
            artifacts={},
        ),
    )
    result = recover_run_transactions(run_dir)
    assert result["result"] == "aborted_preparing"
    assert not (run_dir / "status").exists()


def test_crash_points_matrix_journal_phases(tmp_path: Path) -> None:
    for phase, expected in (
        ("preparing", "aborted_preparing"),
        ("committing", "recovered"),
    ):
        run_dir = _private_run(tmp_path, f"crash-{phase}")
        if phase == "committing":
            transition_run(run_dir, RunEvent.RUN_STARTED)
        artifacts = {}
        if phase == "committing":
            artifacts = {
                "failure.json": {
                    "schema_version": 1,
                    "reason": "x",
                    "phase": "loop",
                    "iteration": 0,
                    "report": None,
                    "recorded_at": "2026-07-25T00:00:00Z",
                }
            }
            secure_write_json(run_dir / "failure.json", artifacts["failure.json"])
        _write_journal(
            run_dir,
            _journal_doc(
                run_dir,
                phase=phase,
                event=RunEvent.RUN_BLOCKED.value,
                status_event=RunEvent.RUN_BLOCKED.value,
                previous_state="EXECUTING" if phase == "committing" else None,
                artifacts=artifacts,
            ),
        )
        result = recover_run_transactions(run_dir)
        assert result["result"] == expected


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def test_audit_chain_detects_timestamp_tamper_and_replay(tmp_path: Path) -> None:
    run_dir = _private_run(tmp_path)
    append_audit_event(
        run_dir,
        event="run_started",
        previous_state=None,
        new_state="EXECUTING",
        origin="runner",
        timestamp="2026-07-25T00:00:00Z",
    )
    append_audit_event(
        run_dir,
        event="run_blocked",
        previous_state="EXECUTING",
        new_state="BLOCKED",
        artifact_hashes={"failure.json": "a" * 64},
        origin="runner",
        timestamp="2026-07-25T00:00:01Z",
    )
    trail = load_audit_trail(run_dir)
    assert trail["events"][0]["previous_hash"] == GENESIS_PREV_HASH
    validate_audit_trail(trail, expected_run_id=run_dir.name)

    trail["events"][1]["timestamp"] = "2099-01-01T00:00:00Z"
    with pytest.raises(Exception, match="hash mismatch|timestamp"):
        validate_audit_trail(trail, expected_run_id=run_dir.name)

    trail = load_audit_trail(run_dir)
    trail["events"][1]["previous_hash"] = "f" * 64
    trail["events"][1]["entry_hash"] = compute_entry_hash(trail["events"][1])
    with pytest.raises(Exception, match="hash chain"):
        validate_audit_trail(trail, expected_run_id=run_dir.name)


# ---------------------------------------------------------------------------
# Migrations / fixtures DX-01..DX-07 / rollback
# ---------------------------------------------------------------------------


def _legacy_run_fixture(run_dir: Path, *, task: str, extra: dict | None = None) -> None:
    payload = {
        "schema_version": 1,
        "run_id": run_dir.name,
        "repo": "/tmp/repo",
        "task_file": task,
        "base_commit": "abc",
        "worktree": "/tmp/wt",
        "max_iterations": 3,
        "profile": {"timeout_sec": 30, "bootstrap": []},
    }
    if extra:
        payload.update(extra)
    atomic_write_json(run_dir / "run.json", payload)


@pytest.mark.parametrize(
    "task_file",
    [
        "docs/tasks/DX-01.md",
        "docs/tasks/DX-02.md",
        "docs/tasks/DX-03.md",
        "docs/tasks/DX-04.md",
        "docs/tasks/DX-05.md",
        "docs/tasks/DX-06.md",
        "docs/tasks/DX-07.md",
    ],
)
def test_migration_fixtures_dx01_through_dx07(tmp_path: Path, task_file: str) -> None:
    run_dir = _private_run(tmp_path, Path(task_file).stem.lower())
    _legacy_run_fixture(run_dir, task=task_file)
    if "DX-01" in task_file:
        atomic_write_json(
            run_dir / "human_approval_request.json",
            {
                "technical_status": "APPROVED",
                "task": task_file,
                "task_id": "DX-01",
                "run_id": run_dir.name,
                "base_commit": "abc",
                "worktree": "/tmp/wt",
                "review_report": "review.json",
                "diff_hash": "a" * 64,
                "callback_token": "c" * 32,
                "token_consumed": False,
                "created_at": "2026-07-25T00:00:00Z",
            },
        )
    if "DX-04" in task_file:
        entry = {
            "idempotency_id": "x" * 64,
            "additional_iterations": 2,
            "previous_limit": 3,
            "effective_limit": 5,
            "origin": "cli",
            "authorized_at": "2026-07-25T00:00:00Z",
            "authorized_at_iteration": 3,
            "review_file": "review-3.json",
            "review_sha256": "a" * 64,
            "reviewed_diff_hash": "b" * 64,
            "blocked_reason": "max_review_iterations",
        }
        from dx.runstate import _extension_id

        entry["idempotency_id"] = _extension_id(run_dir.name, entry)
        atomic_write_json(
            run_dir / "iteration-budget.json",
            {
                "schema_version": 1,
                "run_id": run_dir.name,
                "original_limit": 3,
                "effective_limit": 5,
                "extensions": [entry],
                "updated_at": "2026-07-25T00:00:00Z",
            },
        )
    if "DX-05" in task_file:
        (run_dir / "status").write_text("PUSHED\n", encoding="utf-8")
        _chmod_private(run_dir / "status")
        atomic_write_json(run_dir / "delivery.json", {"mode": "legacy"})
    report = migrate_run(run_dir, dry_run=False)
    assert report["result"] == "migrated"
    assert report["status_before"] == report["status_after"]
    meta = secure_read_json(run_dir / "run.json")
    assert meta["persistence_schema"] == PERSISTENCE_SCHEMA_VERSION
    assert "dx08-frozen-profile" in [m.migration_id for m in MIGRATIONS]
    assert "dx08-approval-schema" in [m.migration_id for m in MIGRATIONS]


def test_migration_dry_run_apply_repeat_and_future_schema(tmp_path: Path) -> None:
    run_dir = _private_run(tmp_path)
    _legacy_run_fixture(run_dir, task="docs/tasks/DX-01.md")
    before_names = {p.name for p in run_dir.iterdir()}
    before_bytes = {
        p.name: p.read_bytes() for p in run_dir.iterdir() if p.is_file()
    }
    plan = migrate_run(run_dir, dry_run=True)
    assert plan["result"] == "dry_run"
    assert plan["persistence_schema"] == 1
    assert {p.name for p in run_dir.iterdir()} == before_names
    assert {
        p.name: p.read_bytes() for p in run_dir.iterdir() if p.is_file()
    } == before_bytes
    assert not (run_dir / ".migration.lock").exists()
    assert not (run_dir / ".state.lock").exists()

    first = migrate_run(run_dir, dry_run=False)
    assert first["result"] == "migrated"
    assert (run_dir / ".migration-backup").is_dir()
    meta = secure_read_json(run_dir / "run.json")
    assert meta["schema_version"] == 2
    assert meta["persistence_schema"] == PERSISTENCE_SCHEMA_VERSION
    assert meta["runner_version"] == RUNNER_VERSION
    assert first["status_before"] == first["status_after"]
    manifest = secure_read_json(
        run_dir / ".migration-backup" / "migration-manifest.json",
        require_private=False,
    )
    assert "audit_head_before" in manifest
    assert "artifact_hashes_before" in manifest
    assert "backup_sha256" in manifest

    second = migrate_run(run_dir, dry_run=False)
    assert second["result"] == "migrated"
    # Repeated migration is idempotent: either unchanged entries or empty applied.
    assert second.get("unchanged") or second.get("applied") == []

    atomic_write_json(
        run_dir / "run.json",
        {**meta, "persistence_schema": 99, "schema_version": 99},
    )
    before = (run_dir / "run.json").read_bytes()
    before_listing = sorted(p.name for p in run_dir.iterdir())
    with pytest.raises(MigrationError, match="newer|future|refusing"):
        migrate_run(run_dir, dry_run=False)
    assert (run_dir / "run.json").read_bytes() == before
    assert sorted(p.name for p in run_dir.iterdir()) == before_listing


def test_migration_rollback_respects_pre_migration_audit_head(tmp_path: Path) -> None:
    run_dir = _private_run(tmp_path)
    _legacy_run_fixture(run_dir, task="docs/tasks/DX-07.md")
    # Pre-existing audit event before migration.
    append_audit_event(
        run_dir,
        event="run_started",
        previous_state=None,
        new_state="EXECUTING",
        origin="runner",
        timestamp="2026-07-25T00:00:00Z",
    )
    trail_before = load_audit_trail(run_dir)
    head_before = trail_before["head_hash"]
    names_before = {p.name for p in run_dir.iterdir() if not p.name.startswith(".")}
    migrate_run(run_dir, dry_run=False)
    assert (run_dir / "audit-trail.json").exists()
    # Head unchanged after migration → rollback allowed.
    rolled = migrate_run(run_dir, rollback=True)
    assert rolled["result"] == "rolled_back"
    assert rolled["audit_head"] == head_before
    names_after = {p.name for p in run_dir.iterdir() if not p.name.startswith(".")}
    # Migration-created files that were absent before must be removed.
    assert names_after <= names_before | {"audit-trail.json"}
    # audit-trail existed before migration in this fixture, so it remains.
    assert (run_dir / "audit-trail.json").exists()

    # Fresh legacy run without audit → migration creates audit-trail → rollback removes it.
    run2 = _private_run(tmp_path, "legacy-no-audit")
    _legacy_run_fixture(run2, task="docs/tasks/DX-03.md")
    assert not (run2 / "audit-trail.json").exists()
    migrate_run(run2, dry_run=False)
    assert (run2 / "audit-trail.json").exists()
    migrate_run(run2, rollback=True)
    assert not (run2 / "audit-trail.json").exists()

    # Re-migrate, then append a genuinely new event → rollback refused.
    migrate_run(run_dir, dry_run=False)
    append_audit_event(
        run_dir,
        event="run_blocked",
        previous_state="EXECUTING",
        new_state="BLOCKED",
        origin="runner",
        timestamp="2026-07-25T00:00:01Z",
    )
    with pytest.raises(MigrationError, match="audit head changed"):
        migrate_run(run_dir, rollback=True)


def test_migration_preserves_iteration_budget_limits(tmp_path: Path) -> None:
    run_dir = _private_run(tmp_path)
    _legacy_run_fixture(run_dir, task="docs/tasks/DX-04.md")
    entry = {
        "idempotency_id": "x" * 64,
        "additional_iterations": 2,
        "previous_limit": 3,
        "effective_limit": 5,
        "origin": "cli",
        "authorized_at": "2026-07-25T00:00:00Z",
        "authorized_at_iteration": 3,
        "review_file": "review-3.json",
        "review_sha256": "a" * 64,
        "reviewed_diff_hash": "b" * 64,
        "blocked_reason": "max_review_iterations",
    }
    from dx.runstate import _extension_id

    entry["idempotency_id"] = _extension_id(run_dir.name, entry)
    atomic_write_json(
        run_dir / "iteration-budget.json",
        {
            "schema_version": 1,
            "run_id": run_dir.name,
            "original_limit": 3,
            "effective_limit": 5,
            "extensions": [entry],
            "updated_at": "2026-07-25T00:00:00Z",
        },
    )
    report = migrate_run(run_dir, dry_run=False)
    budget = secure_read_json(run_dir / "iteration-budget.json")
    assert budget["effective_limit"] == 5
    assert budget["original_limit"] == 3
    assert budget["schema_version"] == ITERATION_BUDGET_SCHEMA_VERSION
    assert budget["extensions"][0]["previous_entry_hash"] == "0" * 64
    assert report["status_before"] == report["status_after"]


def test_legacy_delivery_fixture_is_inspect_only(tmp_path: Path) -> None:
    run_dir = _private_run(tmp_path)
    _legacy_run_fixture(run_dir, task="docs/tasks/DX-05.md")
    (run_dir / "status").write_text("PUSHED\n", encoding="utf-8")
    _chmod_private(run_dir / "status")
    atomic_write_json(run_dir / "delivery.json", {"mode": "legacy"})
    report = migrate_run(run_dir, dry_run=False)
    assert "legacy delivery" in " ".join(report.get("notes", [])).lower() or report.get(
        "legacy_delivery"
    )
    assert (run_dir / "status").read_text(encoding="utf-8").strip() == "PUSHED"


def test_inspect_and_verify_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_dir = _private_run(tmp_path)
    atomic_write_json(
        run_dir / "run.json",
        {
            "schema_version": 2,
            "persistence_schema": 2,
            "runner_version": RUNNER_VERSION,
            "run_id": run_dir.name,
            "repo": "/tmp/repo",
            "task_file": "docs/tasks/DX-08.md",
            "base_commit": "abc",
            "worktree": "/tmp/wt",
            "max_iterations": 1,
        },
    )
    assert cli_main(["inspect", "--run-dir", str(run_dir)]) in {0, 2}
    out = capsys.readouterr().out
    assert "persistence_schema" in out
    assert "callback_token" not in out
    assert cli_main(["verify-state", "--run-dir", str(run_dir)]) in {0, 2}


# ---------------------------------------------------------------------------
# Multiprocess concurrency (canonical locks)
# ---------------------------------------------------------------------------


def _mp_migrate(run_dir: str, queue: mp.Queue) -> None:
    sys.path.insert(0, str(AGENTS))
    from dx.migrate import MigrationError, migrate_run

    deadline = time.monotonic() + 5
    while True:
        try:
            migrate_run(Path(run_dir), dry_run=False)
            queue.put("migrated")
            return
        except MigrationError as exc:
            message = str(exc)
            retryable = (
                "another migration holds the run lock" in message
                or "active state transition holds .state.lock" in message
            )
            if not retryable or time.monotonic() >= deadline:
                queue.put(f"migration_error:{exc}")
                return
            time.sleep(0.02)
        except BaseException as exc:  # noqa: BLE001
            queue.put(f"error:{type(exc).__name__}:{exc}")
            return


def _mp_transition(run_dir: str, queue: mp.Queue) -> None:
    sys.path.insert(0, str(AGENTS))
    from dx.state_machine import RunEvent, StateTransitionError, transition_run

    try:
        transition_run(Path(run_dir), RunEvent.RUN_STARTED)
        queue.put("transitioned")
    except StateTransitionError as exc:
        queue.put(f"transition_error:{exc}")
    except BaseException as exc:  # noqa: BLE001
        queue.put(f"error:{type(exc).__name__}:{exc}")


def test_multiprocess_migration_versus_transition(tmp_path: Path) -> None:
    run_dir = _private_run(tmp_path)
    _legacy_run_fixture(run_dir, task="docs/tasks/DX-07.md")
    queue: mp.Queue = mp.Queue()
    procs = [
        mp.Process(target=_mp_migrate, args=(str(run_dir), queue)),
        mp.Process(target=_mp_transition, args=(str(run_dir), queue)),
        mp.Process(target=_mp_migrate, args=(str(run_dir), queue)),
        mp.Process(target=_mp_transition, args=(str(run_dir), queue)),
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=60)
        assert proc.exitcode == 0
    outcomes = [queue.get(timeout=5) for _ in procs]
    assert any(item == "migrated" for item in outcomes)
    assert any(item.startswith("transition") or item == "transitioned" for item in outcomes)
    meta = secure_read_json(run_dir / "run.json")
    assert meta["persistence_schema"] == PERSISTENCE_SCHEMA_VERSION
    # Status is either EXECUTING (transition won) or empty (migration-only path
    # never invents status); never corrupted.
    status = read_status(run_dir)
    assert status in {"", "EXECUTING"}


def test_two_threads_migrate_and_read(tmp_path: Path) -> None:
    run_dir = _private_run(tmp_path)
    _legacy_run_fixture(run_dir, task="docs/tasks/DX-07.md")
    outcomes: list[str] = []
    errors: list[BaseException] = []

    def migrator() -> None:
        try:
            migrate_run(run_dir, dry_run=False)
            outcomes.append("migrated")
        except MigrationError as exc:
            if "lock" in str(exc).lower():
                outcomes.append("lock_busy")
            else:
                errors.append(exc)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader() -> None:
        try:
            verify_run_state(run_dir)
            inspect_run(run_dir)
            outcomes.append("read")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(migrator) for _ in range(2)]
        futures += [pool.submit(reader) for _ in range(2)]
        for future in futures:
            future.result(timeout=30)
    assert not errors
    assert "migrated" in outcomes


def test_umask_entrypoint_is_private(tmp_path: Path) -> None:
    previous = apply_secure_umask()
    try:
        path = tmp_path / "created.txt"
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o666)
        os.close(fd)
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode & 0o077 == 0
    finally:
        os.umask(previous)


# ---------------------------------------------------------------------------
# Reviewer-requested regressions (containment, locks, schemas, fault injection)
# ---------------------------------------------------------------------------


def test_containment_root_rejects_leaf_and_intermediate_symlinks(tmp_path: Path) -> None:
    run_dir = _private_run(tmp_path)
    real = run_dir / "real.json"
    secure_write_json(real, {"ok": True})
    leaf = run_dir / "leaf.json"
    leaf.symlink_to(real)
    with pytest.raises(PersistError, match="symlink"):
        secure_read_json(leaf, containment_root=run_dir)
    with pytest.raises(PersistError, match="symlink"):
        secure_write_json(leaf, {"nope": True}, containment_root=run_dir)

    sub = run_dir / "sub"
    outside = tmp_path / "outside-dir"
    outside.mkdir(mode=0o700)
    sub.symlink_to(outside)
    target = run_dir / "sub" / "nested.json"
    with pytest.raises(PersistError, match="symlink"):
        secure_write_json(target, {"x": 1}, containment_root=run_dir)
    with pytest.raises(PersistError, match="symlink"):
        secure_read_json(target, containment_root=run_dir)


def test_transaction_and_record_failure_refuse_symlink_failure(tmp_path: Path) -> None:
    run_dir = _private_run(tmp_path)
    transition_run(run_dir, RunEvent.RUN_STARTED)
    decoy = run_dir / "decoy.json"
    secure_write_json(decoy, {"schema_version": 1})
    failure = run_dir / "failure.json"
    failure.symlink_to(decoy)
    txn = LogicalTransaction(
        run_dir=run_dir,
        event=RunEvent.RUN_BLOCKED.value,
        status_event=RunEvent.RUN_BLOCKED,
        origin="runner",
    )
    txn.add_json(
        "failure.json",
        {
            "schema_version": 1,
            "reason": "boom",
            "phase": "loop",
            "iteration": 1,
            "report": None,
            "recorded_at": "2026-07-25T00:00:00Z",
        },
    )
    with pytest.raises((TransactionError, PersistError)):
        txn.commit()
    assert failure.is_symlink()
    assert cli_main(
        [
            "record-failure",
            "--run-dir",
            str(run_dir),
            "--reason",
            "boom",
            "--phase",
            "loop",
            "--iteration",
            "1",
        ]
    ) != 0
    assert failure.is_symlink()


def test_recovery_rejects_future_and_malformed_journals(tmp_path: Path) -> None:
    run_dir = _private_run(tmp_path)
    bad = _journal_doc(
        run_dir,
        phase=JOURNAL_COMMITTING,
        event=RunEvent.RUN_BLOCKED.value,
        status_event=RunEvent.RUN_BLOCKED.value,
        previous_state="EXECUTING",
        artifacts={},
    )
    bad["schema_version"] = 99
    _write_journal(run_dir, bad)
    with pytest.raises(TransactionError, match="newer|future|refusing|schema"):
        recover_run_transactions(run_dir)
    assert (run_dir / ".txn.json").exists()

    run2 = _private_run(tmp_path, "run-malformed")
    malformed = _journal_doc(
        run2,
        phase=JOURNAL_COMMITTING,
        event=RunEvent.RUN_BLOCKED.value,
        status_event=RunEvent.RUN_BLOCKED.value,
        previous_state="EXECUTING",
        artifacts={},
    )
    malformed["extra_field"] = True
    _write_journal(run2, malformed)
    with pytest.raises(TransactionError, match="unknown fields"):
        recover_run_transactions(run2)
    assert (run2 / ".txn.json").exists()


def test_load_run_metadata_rejects_future_persistence_and_runner(
    tmp_path: Path,
) -> None:
    from dx.runstate import RunStateError, load_run_metadata

    run_dir = _private_run(tmp_path)
    atomic_write_json(
        run_dir / "run.json",
        {
            "schema_version": 2,
            "persistence_schema": 999,
            "runner_version": "99.0.0",
            "run_id": run_dir.name,
            "repo": "/tmp/repo",
            "task_file": "docs/tasks/DX-08.md",
            "base_commit": "abc",
            "worktree": "/tmp/wt",
            "max_iterations": 1,
        },
    )
    with pytest.raises(RunStateError, match="newer|future|refusing"):
        load_run_metadata(run_dir)

    atomic_write_json(
        run_dir / "run.json",
        {
            "schema_version": 2,
            "persistence_schema": PERSISTENCE_SCHEMA_VERSION,
            "runner_version": "9.9.9",
            "run_id": run_dir.name,
            "repo": "/tmp/repo",
            "task_file": "docs/tasks/DX-08.md",
            "base_commit": "abc",
            "worktree": "/tmp/wt",
            "max_iterations": 1,
        },
    )
    with pytest.raises(RunStateError, match="newer|future|refusing"):
        load_run_metadata(run_dir)


def test_audit_rejects_future_persistence_schema(tmp_path: Path) -> None:
    from dx.audit import AuditError

    run_dir = _private_run(tmp_path)
    append_audit_event(
        run_dir,
        event="run_started",
        previous_state=None,
        new_state="EXECUTING",
        origin="runner",
        timestamp="2026-07-25T00:00:00Z",
    )
    trail = load_audit_trail(run_dir)
    trail["persistence_schema"] = 999
    secure_write_json(run_dir / "audit-trail.json", trail)
    with pytest.raises(AuditError, match="future|persistence"):
        load_audit_trail(run_dir)


def test_iteration_budget_updated_at_tamper_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dx.runstate import IterationBudgetError, load_iteration_budget
    from dx import runstate as runstate_mod

    run_dir = _private_run(tmp_path)
    entry = {
        "additional_iterations": 2,
        "previous_limit": 3,
        "effective_limit": 5,
        "origin": "cli",
        "authorized_at": "2026-07-25T00:00:00Z",
        "updated_at": "2026-07-25T00:00:00Z",
        "authorized_at_iteration": 3,
        "review_file": "review-3.json",
        "review_sha256": "a" * 64,
        "reviewed_diff_hash": "b" * 64,
        "blocked_reason": "max_review_iterations",
        "previous_entry_hash": "0" * 64,
    }
    from dx.runstate import _extension_id_v2

    entry["idempotency_id"] = _extension_id_v2(run_dir.name, entry)
    monkeypatch.setattr(runstate_mod, "_review_sha256", lambda path: "a" * 64)
    monkeypatch.setattr(
        runstate_mod, "_strict_review_snapshot_hash", lambda run_dir, iteration: "b" * 64
    )
    document = {
        "schema_version": 2,
        "run_id": run_dir.name,
        "original_limit": 3,
        "effective_limit": 5,
        "extensions": [entry],
        "updated_at": "2026-07-25T00:00:00Z",
    }
    secure_write_json(run_dir / "iteration-budget.json", document)
    # Valid document loads.
    loaded = load_iteration_budget(run_dir, 3)
    assert loaded["effective_limit"] == 5

    # Tamper document updated_at without retargeting tip entry / rebinding hashes.
    tampered = secure_read_json(run_dir / "iteration-budget.json")
    tampered["updated_at"] = "2099-01-01T00:00:00Z"
    secure_write_json(run_dir / "iteration-budget.json", tampered)
    with pytest.raises(IterationBudgetError, match="updated_at"):
        load_iteration_budget(run_dir, 3)

    # Tamper tip entry updated_at without recomputing idempotency/chain hashes.
    tip_tamper = secure_read_json(run_dir / "iteration-budget.json")
    tip_tamper["updated_at"] = "2026-07-25T00:00:00Z"
    tip_tamper["extensions"][0]["updated_at"] = "2099-01-01T00:00:00Z"
    secure_write_json(run_dir / "iteration-budget.json", tip_tamper)
    with pytest.raises(IterationBudgetError, match="idempotency|updated_at|binding"):
        load_iteration_budget(run_dir, 3)


def test_critical_txn_holds_state_lock_against_blocked_interleave(
    tmp_path: Path,
) -> None:
    """Specialty-lock transactions still serialize on .state.lock for the commit body."""
    from dx.atomic import run_scoped_lock
    from dx.state_machine import STATE_LOCK_FILENAME

    run_dir = _private_run(tmp_path)
    transition_run(run_dir, RunEvent.RUN_STARTED)
    started = threading.Event()
    release = threading.Event()
    outcomes: list[str] = []

    def hold_state_lock() -> None:
        with run_scoped_lock(run_dir, lock_name=STATE_LOCK_FILENAME):
            started.set()
            release.wait(timeout=5)

    def approval_like() -> None:
        started.wait(timeout=5)
        txn = LogicalTransaction(
            run_dir=run_dir,
            event=RunEvent.RUN_BLOCKED.value,
            status_event=RunEvent.RUN_BLOCKED,
            origin="runner",
            _lock_name=".approval.lock",
        )
        txn.add_json(
            "failure.json",
            {
                "schema_version": 1,
                "reason": "from-approval",
                "phase": "loop",
                "iteration": 0,
                "report": None,
                "recorded_at": "2026-07-25T00:00:01Z",
            },
        )
        try:
            txn.commit()
            outcomes.append("approval")
        except Exception as exc:  # noqa: BLE001
            outcomes.append(f"approval_err:{type(exc).__name__}")

    holder = threading.Thread(target=hold_state_lock)
    worker = threading.Thread(target=approval_like)
    holder.start()
    assert started.wait(timeout=5)
    worker.start()
    # While .state.lock is held, the approval transaction must not publish artifacts.
    time.sleep(0.3)
    assert not (run_dir / "failure.json").exists()
    assert not (run_dir / ".txn.json").exists()
    release.set()
    holder.join(timeout=10)
    worker.join(timeout=10)
    assert outcomes == ["approval"]
    assert not (run_dir / ".txn.json").exists()
    assert secure_read_json(run_dir / "failure.json")["reason"] == "from-approval"


def test_fault_injection_boundaries_for_secure_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dx.persist as persist_mod

    run_dir = _private_run(tmp_path)

    # Fail during file fsync.
    path_fsync = run_dir / "fsync.json"
    real_fsync = persist_mod.os.fsync

    def boom_fsync(fd: int) -> None:
        raise OSError("injected fsync failure")

    monkeypatch.setattr(persist_mod.os, "fsync", boom_fsync)
    with pytest.raises(OSError, match="injected fsync"):
        secure_write_json(path_fsync, {"a": 1})
    assert not path_fsync.exists()
    monkeypatch.setattr(persist_mod.os, "fsync", real_fsync)

    # Fail during replace.
    path_replace = run_dir / "replace.json"
    real_replace = persist_mod.os.replace

    def boom_replace(src: str, dst: str) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(persist_mod.os, "replace", boom_replace)
    with pytest.raises(OSError, match="injected replace"):
        secure_write_json(path_replace, {"a": 1})
    assert not path_replace.exists()
    monkeypatch.setattr(persist_mod.os, "replace", real_replace)

    # Fail during directory fsync after successful replace.
    path_dir = run_dir / "dirsync.json"
    real_dir = persist_mod.fsync_directory

    def boom_dir(path: Path | str) -> None:
        raise PersistError(f"directory fsync unsupported or failed for {path}")

    monkeypatch.setattr(persist_mod, "fsync_directory", boom_dir)
    with pytest.raises(PersistError, match="directory fsync"):
        secure_write_json(path_dir, {"a": 1})
    monkeypatch.setattr(persist_mod, "fsync_directory", real_dir)


def test_cli_persist_text_and_publish_file(tmp_path: Path) -> None:
    run_dir = _private_run(tmp_path)
    assert (
        cli_main(
            [
                "persist-text",
                "--run-dir",
                str(run_dir),
                "--name",
                "iteration",
                "--value",
                "3",
            ]
        )
        == 0
    )
    assert (run_dir / "iteration").read_text(encoding="utf-8").strip() == "3"
    staged = run_dir / "staged.json"
    staged.write_text('{"ok": true}\n', encoding="utf-8")
    os.chmod(staged, 0o600)
    assert (
        cli_main(
            [
                "publish-file",
                "--run-dir",
                str(run_dir),
                "--name",
                "published.json",
                "--source",
                str(staged),
            ]
        )
        == 0
    )
    assert secure_read_json(run_dir / "published.json") == {"ok": True}
    assert not staged.exists()
