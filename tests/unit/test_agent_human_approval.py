"""Unit tests for the agent-loop human approval contract (DX-01)."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

AGENTS_DIR = Path(__file__).resolve().parents[2] / "scripts" / "agents"
sys.path.insert(0, str(AGENTS_DIR))

import dx.approval as approval_mod  # noqa: E402
import dx.atomic as atomic_mod  # noqa: E402
from dx.approval import (  # noqa: E402
    STATUS_APPROVED,
    STATUS_AWAITING,
    STATUS_BLOCKED,
    STATUS_HUMAN_APPROVED,
    ApprovalError,
    apply_human_approval,
    compute_diff_hash,
    create_approval_request,
    enqueue_notification,
    load_decision,
    load_request,
    read_status,
    validate_decision_matches_request,
    verify_reviewed_snapshot,
    wait_for_decision,
)
from dx.atomic import exclusive_write_json  # noqa: E402

_BINDING_KEYS = (
    "task",
    "task_id",
    "base_commit",
    "worktree",
    "review_report",
    "diff_hash",
    "run_id",
)


def _security_binding_fields(payload: dict) -> dict:
    return {key: payload[key] for key in _BINDING_KEYS}


@pytest.fixture
def git_worktree(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "dx@example.com")
    _git(repo, "config", "user.name", "DX Test")
    tracked = repo / "tracked.txt"
    tracked.write_text("base\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD").strip()
    tracked.write_text("changed\n", encoding="utf-8")
    (repo / "extra.txt").write_text("untracked\n", encoding="utf-8")
    return repo, base


def _git(repo: Path, *args: str) -> str:
    import subprocess

    return subprocess.check_output(["git", "-C", str(repo), *args], text=True)


def _approve(run_dir: Path, request: dict, user_id: int = 42) -> tuple[str, dict]:
    return apply_human_approval(
        run_dir=run_dir,
        callback_token=request["callback_token"],
        telegram_user_id=user_id,
        telegram_chat_id=user_id,
        allowed_user_id=user_id,
        allowed_chat_id=user_id,
    )


def _arm_technical_approved(run_dir: Path) -> None:
    """Mirror run_task: record technical APPROVED before opening the human gate."""
    approval_mod.write_status(run_dir, STATUS_APPROVED)


def test_diff_hash_stable_and_sensitive(git_worktree: tuple[Path, str]) -> None:
    worktree, base = git_worktree
    h1 = compute_diff_hash(worktree, base)
    h2 = compute_diff_hash(worktree, base)
    assert h1 == h2
    (worktree / "extra.txt").write_text("untracked-changed\n", encoding="utf-8")
    assert compute_diff_hash(worktree, base) != h1


def test_approved_to_awaiting_to_human_approved(git_worktree: tuple[Path, str], tmp_path: Path) -> None:
    worktree, base = git_worktree
    run_dir = tmp_path / "runs" / "dx-01-test"
    review = run_dir / "review-1.json"
    run_dir.mkdir(parents=True)
    review.write_text(json.dumps({"status": "APPROVED", "summary": "ok", "findings": [], "tests_required": []}), encoding="utf-8")

    _arm_technical_approved(run_dir)
    request = create_approval_request(
        run_dir=run_dir,
        task="docs/tasks/DX-01.md",
        base_commit=base,
        worktree=worktree,
        review_report=str(review),
    )
    assert request["technical_status"] == STATUS_APPROVED
    assert read_status(run_dir) == STATUS_AWAITING
    assert load_request(run_dir)["diff_hash"] == request["diff_hash"]

    notify = enqueue_notification(
        run_dir=run_dir,
        kind="awaiting_human_approval",
        summary="pending",
        report_hint="review-1.json",
    )
    assert notify is not None
    assert notify["offer_approval_button"] is True
    assert "callback_token" in notify

    result, decision = _approve(run_dir, request)
    assert result == "accepted"
    assert decision["run_id"] == request["run_id"]
    assert decision["diff_hash"] == request["diff_hash"]
    assert read_status(run_dir) == STATUS_HUMAN_APPROVED
    assert load_decision(run_dir)["decision"] == "approve"
    assert load_request(run_dir)["token_consumed"] is True
    verify = verify_reviewed_snapshot(run_dir)
    assert verify["matches"] is True


def test_replay_callback_is_idempotent(git_worktree: tuple[Path, str], tmp_path: Path) -> None:
    worktree, base = git_worktree
    run_dir = tmp_path / "runs" / "run-replay"
    run_dir.mkdir(parents=True)
    _arm_technical_approved(run_dir)
    request = create_approval_request(
        run_dir=run_dir,
        task="docs/tasks/DX-01.md",
        base_commit=base,
        worktree=worktree,
        review_report="review.json",
    )
    assert _approve(run_dir, request, user_id=7)[0] == "accepted"
    first = load_decision(run_dir)
    assert _approve(run_dir, request, user_id=7)[0] == "idempotent_replay"
    assert load_decision(run_dir) == first
    assert read_status(run_dir) == STATUS_HUMAN_APPROVED


def test_callback_replay_repairs_status_after_decision_publication_crash(
    git_worktree: tuple[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Crash after exclusive decision publish but before HUMAN_APPROVED status.

    Replay must repair status to HUMAN_APPROVED under the run lock and return
    idempotent_replay; bridge callers observe the repaired persisted state.
    """
    worktree, base = git_worktree
    run_dir = tmp_path / "runs" / "run-crash-pub"
    run_dir.mkdir(parents=True)
    _arm_technical_approved(run_dir)
    request = create_approval_request(
        run_dir=run_dir,
        task="docs/tasks/DX-01.md",
        base_commit=base,
        worktree=worktree,
        review_report="review.json",
    )

    original_write_status = approval_mod.write_status
    crash_once = {"armed": True}

    def crash_before_human_approved(target_run_dir: Path, status: str) -> None:
        if status == STATUS_HUMAN_APPROVED and crash_once["armed"]:
            crash_once["armed"] = False
            # Decision artifact must already be the real published file.
            assert (Path(target_run_dir) / "human_approval_decision.json").is_file()
            raise RuntimeError("simulated crash after decision publication")
        original_write_status(target_run_dir, status)

    monkeypatch.setattr(approval_mod, "write_status", crash_before_human_approved)

    with pytest.raises(RuntimeError, match="simulated crash after decision publication"):
        _approve(run_dir, request, user_id=9)

    assert load_decision(run_dir) is not None
    validate_decision_matches_request(run_dir)
    assert read_status(run_dir) == STATUS_AWAITING

    result, decision = _approve(run_dir, request, user_id=9)
    assert result == "idempotent_replay"
    assert decision["run_id"] == request["run_id"]
    assert decision["diff_hash"] == request["diff_hash"]
    assert read_status(run_dir) == STATUS_HUMAN_APPROVED
    assert validate_decision_matches_request(run_dir) == decision


def test_callback_from_other_run_rejected(git_worktree: tuple[Path, str], tmp_path: Path) -> None:
    worktree, base = git_worktree
    run_a = tmp_path / "runs" / "run-a"
    run_b = tmp_path / "runs" / "run-b"
    run_a.mkdir(parents=True)
    run_b.mkdir(parents=True)
    _arm_technical_approved(run_a)
    req_a = create_approval_request(
        run_dir=run_a,
        task="docs/tasks/A.md",
        base_commit=base,
        worktree=worktree,
        review_report="a.json",
    )
    _arm_technical_approved(run_b)
    create_approval_request(
        run_dir=run_b,
        task="docs/tasks/B.md",
        base_commit=base,
        worktree=worktree,
        review_report="b.json",
    )
    result, _ = apply_human_approval(
        run_dir=run_b,
        callback_token=req_a["callback_token"],
        telegram_user_id=1,
        telegram_chat_id=1,
        allowed_user_id=1,
        allowed_chat_id=1,
    )
    assert result == "rejected_token"
    assert read_status(run_b) == STATUS_AWAITING
    assert load_decision(run_b) is None


def test_post_hash_worktree_mutation_still_approves_reviewed_hash(
    git_worktree: tuple[Path, str],
    tmp_path: Path,
) -> None:
    """
    Live worktree mutation after the reviewed hash is bound must not change what
    is approved: HUMAN_APPROVED binds the immutable reviewed hash. Planner
    verification detects the drift before integration.
    """
    worktree, base = git_worktree
    run_dir = tmp_path / "runs" / "run-post-hash"
    run_dir.mkdir(parents=True)
    reviewed = compute_diff_hash(worktree, base)
    _arm_technical_approved(run_dir)
    request = create_approval_request(
        run_dir=run_dir,
        task="docs/tasks/DX-01.md",
        base_commit=base,
        worktree=worktree,
        review_report="review.json",
        diff_hash=reviewed,
    )
    (worktree / "tracked.txt").write_text("mutated-after-reviewed-hash\n", encoding="utf-8")
    assert compute_diff_hash(worktree, base) != reviewed

    result, decision = _approve(run_dir, request, user_id=1)
    assert result == "accepted"
    assert decision["diff_hash"] == reviewed
    assert read_status(run_dir) == STATUS_HUMAN_APPROVED

    verify = verify_reviewed_snapshot(run_dir)
    assert verify["matches"] is False
    assert verify["reviewed_diff_hash"] == reviewed
    assert verify["current_diff_hash"] != reviewed


def test_post_hash_mutation_during_claim_does_not_alter_approved_hash(
    monkeypatch: pytest.MonkeyPatch,
    git_worktree: tuple[Path, str],
    tmp_path: Path,
) -> None:
    """Mutation while claim holds the lock cannot change the reviewed hash being approved."""
    worktree, base = git_worktree
    run_dir = tmp_path / "runs" / "run-toctou-semantics"
    run_dir.mkdir(parents=True)
    reviewed = compute_diff_hash(worktree, base)
    _arm_technical_approved(run_dir)
    request = create_approval_request(
        run_dir=run_dir,
        task="docs/tasks/DX-01.md",
        base_commit=base,
        worktree=worktree,
        review_report="review.json",
        diff_hash=reviewed,
    )

    original_excl = approval_mod.exclusive_write_json
    in_publish = threading.Barrier(2)
    mutate_done = threading.Event()
    results: list[str] = []

    def gated_excl(path: Path, payload: dict, mode: int = 0o600) -> bool:
        if Path(path).name == "human_approval_decision.json":
            in_publish.wait(timeout=10)
            assert mutate_done.wait(timeout=10), "mutator did not finish"
            assert payload["diff_hash"] == reviewed
        return original_excl(path, payload, mode=mode)

    monkeypatch.setattr(approval_mod, "exclusive_write_json", gated_excl)

    def claimer() -> None:
        result, decision = _approve(run_dir, request, user_id=21)
        results.append(result)
        assert decision["diff_hash"] == reviewed

    def mutator() -> None:
        in_publish.wait(timeout=10)
        (worktree / "tracked.txt").write_text("mutated-during-claim\n", encoding="utf-8")
        mutate_done.set()

    threads = [threading.Thread(target=claimer), threading.Thread(target=mutator)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    assert results == ["accepted"]
    assert load_decision(run_dir)["diff_hash"] == reviewed
    assert read_status(run_dir) == STATUS_HUMAN_APPROVED
    assert verify_reviewed_snapshot(run_dir)["matches"] is False


def test_forged_and_incomplete_decisions_do_not_promote(
    git_worktree: tuple[Path, str],
    tmp_path: Path,
) -> None:
    worktree, base = git_worktree
    run_dir = tmp_path / "runs" / "run-forged"
    run_dir.mkdir(parents=True)
    _arm_technical_approved(run_dir)
    request = create_approval_request(
        run_dir=run_dir,
        task="docs/tasks/DX-01.md",
        base_commit=base,
        worktree=worktree,
        review_report="review.json",
    )

    minimal = {
        "decision": "approve",
        "run_id": request["run_id"],
        "diff_hash": request["diff_hash"],
    }
    (run_dir / "human_approval_decision.json").write_text(
        json.dumps(minimal) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ApprovalError):
        validate_decision_matches_request(run_dir)
    assert wait_for_decision(run_dir, timeout_sec=1, poll_interval=0.2) is False
    assert read_status(run_dir) == STATUS_AWAITING

    # Missing token_consumed / telegram fields / wrong schema.
    incomplete = {
        "schema_version": 1,
        "decision": "approve",
        "run_id": request["run_id"],
        "diff_hash": request["diff_hash"],
        "callback_token": request["callback_token"],
        "telegram_user_id": 9,
        "telegram_chat_id": 9,
    }
    (run_dir / "human_approval_decision.json").write_text(
        json.dumps(incomplete) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ApprovalError, match="token not consumed"):
        validate_decision_matches_request(run_dir)
    assert wait_for_decision(run_dir, timeout_sec=1, poll_interval=0.2) is False
    assert read_status(run_dir) == STATUS_AWAITING

    # Wrong callback token even with token_consumed flipped.
    forged = dict(incomplete)
    forged["callback_token"] = "deadbeef" * 4
    request_path = run_dir / "human_approval_request.json"
    req = json.loads(request_path.read_text(encoding="utf-8"))
    req["token_consumed"] = True
    request_path.write_text(json.dumps(req, indent=2) + "\n", encoding="utf-8")
    (run_dir / "human_approval_decision.json").write_text(
        json.dumps(forged) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ApprovalError, match="callback_token"):
        validate_decision_matches_request(run_dir)
    assert wait_for_decision(run_dir, timeout_sec=1, poll_interval=0.2) is False


def test_wait_timeout_preserves_awaiting(git_worktree: tuple[Path, str], tmp_path: Path) -> None:
    worktree, base = git_worktree
    run_dir = tmp_path / "runs" / "run-timeout"
    run_dir.mkdir(parents=True)
    _arm_technical_approved(run_dir)
    create_approval_request(
        run_dir=run_dir,
        task="docs/tasks/DX-01.md",
        base_commit=base,
        worktree=worktree,
        review_report="review.json",
    )
    assert wait_for_decision(run_dir, timeout_sec=1, poll_interval=0.2) is False
    assert read_status(run_dir) == STATUS_AWAITING
    assert load_decision(run_dir) is None


def test_wait_timeout_preserves_blocked_and_token_unclaimable(
    git_worktree: tuple[Path, str],
    tmp_path: Path,
) -> None:
    """Timeout must never rewrite BLOCKED → AWAITING or make its token claimable."""
    worktree, base = git_worktree
    run_dir = tmp_path / "runs" / "run-timeout-blocked"
    run_dir.mkdir(parents=True)
    _arm_technical_approved(run_dir)
    request = create_approval_request(
        run_dir=run_dir,
        task="docs/tasks/DX-01.md",
        base_commit=base,
        worktree=worktree,
        review_report="review.json",
    )
    approval_mod.write_status(run_dir, STATUS_BLOCKED)
    token = request["callback_token"]

    assert wait_for_decision(run_dir, timeout_sec=1, poll_interval=0.2) is False
    assert read_status(run_dir) == STATUS_BLOCKED
    assert load_decision(run_dir) is None
    assert load_request(run_dir)["callback_token"] == token
    assert load_request(run_dir)["token_consumed"] is False

    result, _ = _approve(run_dir, request, user_id=77)
    assert result == "rejected_wrong_state"
    assert read_status(run_dir) == STATUS_BLOCKED
    assert load_decision(run_dir) is None
    assert load_request(run_dir)["token_consumed"] is False


def test_diff_hash_untracked_symlink_target_and_type_and_mode(
    git_worktree: tuple[Path, str],
    tmp_path: Path,
) -> None:
    """Untracked fingerprint must cover symlink target, type, and executable bit."""
    worktree, base = git_worktree
    outside = tmp_path / "outside-worktree"
    outside.mkdir()
    target_a = outside / "a.txt"
    target_b = outside / "b.txt"
    target_a.write_text("same-bytes\n", encoding="utf-8")
    target_b.write_text("same-bytes\n", encoding="utf-8")

    link = worktree / "link.txt"
    link.symlink_to(target_a)
    h_link_a = compute_diff_hash(worktree, base)

    # Changed symlink target with identical referent contents must change hash.
    link.unlink()
    link.symlink_to(target_b)
    h_link_b = compute_diff_hash(worktree, base)
    assert h_link_b != h_link_a

    # Mutating the outside referent must NOT change the hash (no follow).
    target_b.write_text("mutated-outside\n", encoding="utf-8")
    assert compute_diff_hash(worktree, base) == h_link_b

    # Regular file vs symlink at the same path must differ even with same payload bytes.
    link.unlink()
    link.write_text(str(target_b), encoding="utf-8")
    h_regular = compute_diff_hash(worktree, base)
    assert h_regular != h_link_b

    # Executable-mode change on a regular untracked file must change hash.
    h_before_exec = compute_diff_hash(worktree, base)
    link.chmod(link.stat().st_mode | 0o111)
    h_after_exec = compute_diff_hash(worktree, base)
    assert h_after_exec != h_before_exec

    # Git ls-files ignores FIFOs/sockets; exercise the reject path directly.
    link.unlink()
    fifo = worktree / "pipe.fifo"
    os.mkfifo(fifo)
    with pytest.raises(ApprovalError, match="unsupported untracked type"):
        approval_mod._untracked_entry_fingerprint(worktree, "pipe.fifo")
    fifo.unlink()


def test_wait_succeeds_when_decision_arrives(git_worktree: tuple[Path, str], tmp_path: Path) -> None:
    worktree, base = git_worktree
    run_dir = tmp_path / "runs" / "run-wait"
    run_dir.mkdir(parents=True)
    _arm_technical_approved(run_dir)
    request = create_approval_request(
        run_dir=run_dir,
        task="docs/tasks/DX-01.md",
        base_commit=base,
        worktree=worktree,
        review_report="review.json",
    )

    def approve_later() -> None:
        time.sleep(0.3)
        _approve(run_dir, request, user_id=9)

    thread = threading.Thread(target=approve_later)
    thread.start()
    assert wait_for_decision(run_dir, timeout_sec=5, poll_interval=0.1) is True
    thread.join(timeout=2)
    assert read_status(run_dir) == STATUS_HUMAN_APPROVED


def test_wait_approval_at_timeout_boundary_remains_human_approved(
    monkeypatch: pytest.MonkeyPatch,
    git_worktree: tuple[Path, str],
    tmp_path: Path,
) -> None:
    """Approval landing between final poll and timeout cleanup must stay HUMAN_APPROVED."""
    worktree, base = git_worktree
    run_dir = tmp_path / "runs" / "run-timeout-race"
    run_dir.mkdir(parents=True)
    _arm_technical_approved(run_dir)
    request = create_approval_request(
        run_dir=run_dir,
        task="docs/tasks/DX-01.md",
        base_commit=base,
        worktree=worktree,
        review_report="review.json",
    )

    ticks = {"n": 0}

    def fake_monotonic() -> float:
        ticks["n"] += 1
        # 1: deadline = 0 + 5
        # 2: while 0 < 5 → enter (no decision yet)
        # sleep installs approval
        # 3: while 10 < 5 → exit to final boundary check
        mapping = {1: 0.0, 2: 0.0, 3: 10.0}
        return mapping.get(ticks["n"], 10.0)

    def fake_sleep(_interval: float) -> None:
        _approve(run_dir, request, user_id=11)

    monkeypatch.setattr(approval_mod.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(approval_mod.time, "sleep", fake_sleep)

    assert wait_for_decision(run_dir, timeout_sec=5, poll_interval=1.0) is True
    assert read_status(run_dir) == STATUS_HUMAN_APPROVED
    assert load_decision(run_dir) is not None
    assert load_decision(run_dir)["decision"] == "approve"


def test_wait_success_from_decision_publication_phases(
    git_worktree: tuple[Path, str],
    tmp_path: Path,
) -> None:
    """
    Success is derived from the validated decision across publication phases:
    (1) decision present / status still AWAITING → promote + True
    (2) fully published HUMAN_APPROVED → True
    (3) no decision → timeout False without inventing approval
    """
    worktree, base = git_worktree
    run_dir = tmp_path / "runs" / "run-phases"
    run_dir.mkdir(parents=True)
    _arm_technical_approved(run_dir)
    request = create_approval_request(
        run_dir=run_dir,
        task="docs/tasks/DX-01.md",
        base_commit=base,
        worktree=worktree,
        review_report="review.json",
    )

    # Phase: authentic decision published, status not yet terminal human approval.
    req = load_request(run_dir)
    req["token_consumed"] = True
    (run_dir / "human_approval_request.json").write_text(
        json.dumps(req, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    decision = {
        "schema_version": 1,
        "decision": "approve",
        "run_id": request["run_id"],
        "diff_hash": request["diff_hash"],
        "callback_token": request["callback_token"],
        "telegram_user_id": 31,
        "telegram_chat_id": 31,
        "request_created_at": request["created_at"],
        "decided_at": "2026-07-22T00:00:00Z",
    }
    exclusive_write_json(run_dir / "human_approval_decision.json", decision)
    assert read_status(run_dir) == STATUS_AWAITING
    assert wait_for_decision(run_dir, timeout_sec=2, poll_interval=0.1) is True
    assert read_status(run_dir) == STATUS_HUMAN_APPROVED

    # Phase: already fully published.
    assert wait_for_decision(run_dir, timeout_sec=1, poll_interval=0.1) is True
    assert read_status(run_dir) == STATUS_HUMAN_APPROVED

    # Phase: no decision → never auto-approve.
    empty = tmp_path / "runs" / "run-phases-empty"
    empty.mkdir(parents=True)
    _arm_technical_approved(empty)
    create_approval_request(
        run_dir=empty,
        task="docs/tasks/DX-01.md",
        base_commit=base,
        worktree=worktree,
        review_report="review.json",
    )
    assert wait_for_decision(empty, timeout_sec=1, poll_interval=0.2) is False
    assert read_status(empty) == STATUS_AWAITING
    assert load_decision(empty) is None


def test_wait_timeout_cleanup_serialized_with_claim_publication(
    monkeypatch: pytest.MonkeyPatch,
    git_worktree: tuple[Path, str],
    tmp_path: Path,
) -> None:
    """Timeout cleanup blocks on the same lock until claim finishes publishing the decision."""
    worktree, base = git_worktree
    run_dir = tmp_path / "runs" / "run-cleanup-lock"
    run_dir.mkdir(parents=True)
    _arm_technical_approved(run_dir)
    request = create_approval_request(
        run_dir=run_dir,
        task="docs/tasks/DX-01.md",
        base_commit=base,
        worktree=worktree,
        review_report="review.json",
    )

    original_excl = approval_mod.exclusive_write_json
    decision_written = threading.Event()
    finish_claim = threading.Event()

    def gated_excl(path: Path, payload: dict, mode: int = 0o600) -> bool:
        created = original_excl(path, payload, mode=mode)
        if created and Path(path).name == "human_approval_decision.json":
            # Still inside run_scoped_lock; pause before status promotion.
            decision_written.set()
            assert finish_claim.wait(timeout=10), "finish_claim not signaled"
        return created

    monkeypatch.setattr(approval_mod, "exclusive_write_json", gated_excl)

    ticks = {"n": 0}

    def fake_monotonic() -> float:
        ticks["n"] += 1
        mapping = {1: 0.0, 2: 0.0, 3: 10.0}
        return mapping.get(ticks["n"], 10.0)

    def fake_sleep(_interval: float) -> None:
        def publish() -> None:
            _approve(run_dir, request, user_id=41)

        threading.Thread(target=publish).start()
        assert decision_written.wait(timeout=10), "claim did not reach decision publish"

        def release_after_cleanup_contends() -> None:
            # Brief pause so wait can reach timeout cleanup and contend on the lock.
            time.sleep(0.05)
            finish_claim.set()

        threading.Thread(target=release_after_cleanup_contends).start()

    monkeypatch.setattr(approval_mod.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(approval_mod.time, "sleep", fake_sleep)

    assert wait_for_decision(run_dir, timeout_sec=5, poll_interval=1.0) is True
    assert read_status(run_dir) == STATUS_HUMAN_APPROVED
    assert load_decision(run_dir) is not None
    assert load_decision(run_dir)["decision"] == "approve"


def test_create_request_publication_serialized_with_callback_claim(
    monkeypatch: pytest.MonkeyPatch,
    git_worktree: tuple[Path, str],
    tmp_path: Path,
) -> None:
    """
    Interleave request publication and callback approval under the run lock.

    Without the lock, a claim between request write and AWAITING (or a late
    create after HUMAN_APPROVED) could leave status downgraded. With the lock,
    the callback waits until AWAITING is established, then the final status is
    HUMAN_APPROVED; a subsequent create retains that decision.
    """
    worktree, base = git_worktree
    run_dir = tmp_path / "runs" / "run-create-claim-race"
    run_dir.mkdir(parents=True)

    original_write_json = approval_mod.atomic_write_json
    request_published = threading.Event()
    finish_create = threading.Event()
    callback_started = threading.Event()
    claim_results: list[str] = []
    published_token: dict[str, str] = {}
    gated_once = {"done": False}

    def gated_write_json(path: Path, payload: dict, mode: int = 0o600) -> None:
        original_write_json(path, payload, mode=mode)
        if Path(path).name != "human_approval_request.json" or gated_once["done"]:
            return
        gated_once["done"] = True
        published_token["callback_token"] = payload["callback_token"]
        # Still holding run_scoped_lock; AWAITING not written yet.
        assert read_status(run_dir) == STATUS_APPROVED
        request_published.set()
        assert finish_create.wait(timeout=10), "finish_create not signaled"
        # Callback must still be blocked on the lock (not yet claimed).
        assert read_status(run_dir) == STATUS_APPROVED
        assert load_decision(run_dir) is None

    monkeypatch.setattr(approval_mod, "atomic_write_json", gated_write_json)

    def publisher() -> None:
        create_approval_request(
            run_dir=run_dir,
            task="docs/tasks/DX-01.md",
            base_commit=base,
            worktree=worktree,
            review_report="review.json",
        )

    def claimer() -> None:
        assert request_published.wait(timeout=10), "request not published"
        callback_started.set()
        # Contends on the same lock; cannot claim until AWAITING is established.
        result, _decision = apply_human_approval(
            run_dir=run_dir,
            callback_token=published_token["callback_token"],
            telegram_user_id=51,
            telegram_chat_id=51,
            allowed_user_id=51,
            allowed_chat_id=51,
        )
        claim_results.append(result)

    _arm_technical_approved(run_dir)
    pub = threading.Thread(target=publisher)
    claim = threading.Thread(target=claimer)
    pub.start()
    assert request_published.wait(timeout=10), "publisher did not reach request write"
    claim.start()
    assert callback_started.wait(timeout=10), "callback did not start"
    # Give the claimer time to block on the run-scoped lock.
    time.sleep(0.05)
    assert read_status(run_dir) == STATUS_APPROVED
    assert load_decision(run_dir) is None
    finish_create.set()
    pub.join(timeout=30)
    claim.join(timeout=30)
    assert not pub.is_alive()
    assert not claim.is_alive()

    assert claim_results == ["accepted"]
    assert read_status(run_dir) == STATUS_HUMAN_APPROVED
    assert load_decision(run_dir) is not None
    first_request = load_request(run_dir)

    # Re-entrant create with identical binding recovers/retains HUMAN_APPROVED.
    returned = create_approval_request(
        run_dir=run_dir,
        task="docs/tasks/DX-01.md",
        base_commit=base,
        worktree=worktree,
        review_report="review.json",
    )
    assert returned == first_request
    assert read_status(run_dir) == STATUS_HUMAN_APPROVED
    assert load_decision(run_dir)["callback_token"] == first_request["callback_token"]


def test_identical_recreate_idempotent_no_token_rotation(
    git_worktree: tuple[Path, str],
    tmp_path: Path,
) -> None:
    worktree, base = git_worktree
    run_dir = tmp_path / "runs" / "run-idempotent-recreate"
    run_dir.mkdir(parents=True)
    kwargs = dict(
        run_dir=run_dir,
        task="docs/tasks/DX-01.md",
        base_commit=base,
        worktree=worktree,
        review_report="review.json",
        task_id="DX-01",
    )
    _arm_technical_approved(run_dir)
    first = create_approval_request(**kwargs)
    second = create_approval_request(**kwargs)
    assert second == first
    assert second["callback_token"] == first["callback_token"]
    assert load_request(run_dir)["callback_token"] == first["callback_token"]
    assert read_status(run_dir) == STATUS_AWAITING
    assert load_decision(run_dir) is None

    assert _approve(run_dir, first, user_id=61)[0] == "accepted"
    third = create_approval_request(**kwargs)
    assert third["callback_token"] == first["callback_token"]
    assert _security_binding_fields(third) == _security_binding_fields(first)
    assert read_status(run_dir) == STATUS_HUMAN_APPROVED
    assert load_decision(run_dir)["callback_token"] == first["callback_token"]
    assert third["token_consumed"] is True


def test_interrupted_awaiting_publication_recovers_without_token_rotation(
    monkeypatch: pytest.MonkeyPatch,
    git_worktree: tuple[Path, str],
    tmp_path: Path,
) -> None:
    """
    Crash after atomic request publish but before AWAITING must recover on retry.

    Status stays technical APPROVED with the request on disk; identical recreate
    under the run lock must promote to AWAITING_HUMAN_APPROVAL without rotating
    callback_token, then the Telegram callback can succeed. Recreate must not
    reopen BLOCKED or downgrade HUMAN_APPROVED.
    """
    worktree, base = git_worktree
    run_dir = tmp_path / "runs" / "run-interrupted-awaiting"
    run_dir.mkdir(parents=True)
    kwargs = dict(
        run_dir=run_dir,
        task="docs/tasks/DX-01.md",
        base_commit=base,
        worktree=worktree,
        review_report="review.json",
        task_id="DX-01",
    )

    original_write_json = approval_mod.atomic_write_json
    crash_once = {"armed": True}

    def crash_after_request_publish(path: Path, payload: dict, mode: int = 0o600) -> None:
        original_write_json(path, payload, mode=mode)
        if Path(path).name != "human_approval_request.json" or not crash_once["armed"]:
            return
        crash_once["armed"] = False
        assert read_status(run_dir) == STATUS_APPROVED
        assert load_decision(run_dir) is None
        raise KeyboardInterrupt("simulated crash after request publish")

    monkeypatch.setattr(approval_mod, "atomic_write_json", crash_after_request_publish)

    _arm_technical_approved(run_dir)
    with pytest.raises(KeyboardInterrupt, match="simulated crash"):
        create_approval_request(**kwargs)

    assert (run_dir / "human_approval_request.json").is_file()
    assert read_status(run_dir) == STATUS_APPROVED
    frozen = load_request(run_dir)
    frozen_token = frozen["callback_token"]
    assert frozen["token_consumed"] is False
    assert load_decision(run_dir) is None

    recovered = create_approval_request(**kwargs)
    assert recovered == frozen
    assert recovered["callback_token"] == frozen_token
    assert load_request(run_dir)["callback_token"] == frozen_token
    assert read_status(run_dir) == STATUS_AWAITING
    assert load_decision(run_dir) is None

    result, decision = _approve(run_dir, recovered, user_id=63)
    assert result == "accepted"
    assert decision["callback_token"] == frozen_token
    assert read_status(run_dir) == STATUS_HUMAN_APPROVED

    # Recreate after success retains HUMAN_APPROVED (no downgrade / token rotate).
    retained = create_approval_request(**kwargs)
    assert retained["callback_token"] == frozen_token
    assert read_status(run_dir) == STATUS_HUMAN_APPROVED

    # BLOCKED leftover with identical pending request must not reopen to AWAITING.
    blocked_dir = tmp_path / "runs" / "run-blocked-no-reopen"
    blocked_dir.mkdir(parents=True)
    blocked_kwargs = dict(
        run_dir=blocked_dir,
        task="docs/tasks/DX-01.md",
        base_commit=base,
        worktree=worktree,
        review_report="review.json",
        task_id="DX-01",
    )
    _arm_technical_approved(blocked_dir)
    blocked_req = create_approval_request(**blocked_kwargs)
    approval_mod.write_status(blocked_dir, STATUS_BLOCKED)
    assert read_status(blocked_dir) == STATUS_BLOCKED
    blocked_again = create_approval_request(**blocked_kwargs)
    assert blocked_again["callback_token"] == blocked_req["callback_token"]
    assert read_status(blocked_dir) == STATUS_BLOCKED
    assert load_decision(blocked_dir) is None


def test_mismatched_recreate_after_human_approved_rejected(
    git_worktree: tuple[Path, str],
    tmp_path: Path,
) -> None:
    worktree, base = git_worktree
    run_dir = tmp_path / "runs" / "run-mismatch-after-approve"
    run_dir.mkdir(parents=True)
    _arm_technical_approved(run_dir)
    request = create_approval_request(
        run_dir=run_dir,
        task="docs/tasks/DX-01.md",
        base_commit=base,
        worktree=worktree,
        review_report="review.json",
        task_id="DX-01",
    )
    assert _approve(run_dir, request, user_id=62)[0] == "accepted"
    before_request = load_request(run_dir)
    before_decision = load_decision(run_dir)
    before_status = read_status(run_dir)

    with pytest.raises(ApprovalError, match="binding mismatch"):
        create_approval_request(
            run_dir=run_dir,
            task="docs/tasks/DX-01.md",
            base_commit=base,
            worktree=worktree,
            review_report="review-other.json",
            task_id="DX-01",
        )

    assert load_request(run_dir) == before_request
    assert load_decision(run_dir) == before_decision
    assert read_status(run_dir) == before_status == STATUS_HUMAN_APPROVED


def test_stale_orphan_decision_artifacts_handled_safely(
    git_worktree: tuple[Path, str],
    tmp_path: Path,
) -> None:
    worktree, base = git_worktree
    orphan_dir = tmp_path / "runs" / "run-orphan-decision"
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "human_approval_decision.json").write_text(
        json.dumps({"decision": "approve", "run_id": orphan_dir.name}),
        encoding="utf-8",
    )
    _arm_technical_approved(orphan_dir)
    with pytest.raises(ApprovalError, match="orphan decision"):
        create_approval_request(
            run_dir=orphan_dir,
            task="docs/tasks/DX-01.md",
            base_commit=base,
            worktree=worktree,
            review_report="review.json",
        )
    assert not (orphan_dir / "human_approval_request.json").exists()
    assert read_status(orphan_dir) == STATUS_APPROVED

    stale_dir = tmp_path / "runs" / "run-stale-decision"
    stale_dir.mkdir(parents=True)
    _arm_technical_approved(stale_dir)
    request = create_approval_request(
        run_dir=stale_dir,
        task="docs/tasks/DX-01.md",
        base_commit=base,
        worktree=worktree,
        review_report="review.json",
        task_id="DX-01",
    )
    stale_payload = {
        "schema_version": 1,
        "decision": "approve",
        "run_id": request["run_id"],
        "diff_hash": "0" * 64,
        "callback_token": request["callback_token"],
        "telegram_user_id": 1,
        "telegram_chat_id": 1,
    }
    (stale_dir / "human_approval_decision.json").write_text(
        json.dumps(stale_payload, indent=2) + "\n",
        encoding="utf-8",
    )
    returned = create_approval_request(
        run_dir=stale_dir,
        task="docs/tasks/DX-01.md",
        base_commit=base,
        worktree=worktree,
        review_report="review.json",
        task_id="DX-01",
    )
    assert returned == request
    assert returned["callback_token"] == request["callback_token"]
    assert read_status(stale_dir) == STATUS_AWAITING
    assert load_decision(stale_dir) == stale_payload


def test_callback_versus_mismatched_recreate_interleaving(
    monkeypatch: pytest.MonkeyPatch,
    git_worktree: tuple[Path, str],
    tmp_path: Path,
) -> None:
    """Callback claim and mismatched recreate serialize under the run lock."""
    worktree, base = git_worktree
    run_dir = tmp_path / "runs" / "run-callback-vs-mismatch"
    run_dir.mkdir(parents=True)
    _arm_technical_approved(run_dir)
    request = create_approval_request(
        run_dir=run_dir,
        task="docs/tasks/DX-01.md",
        base_commit=base,
        worktree=worktree,
        review_report="review.json",
        task_id="DX-01",
    )

    original_excl = approval_mod.exclusive_write_json
    decision_published = threading.Event()
    finish_claim = threading.Event()
    recreate_started = threading.Event()
    claim_results: list[str] = []
    recreate_errors: list[BaseException] = []

    def gated_excl(path: Path, payload: dict, mode: int = 0o600) -> bool:
        created = original_excl(path, payload, mode=mode)
        if created and Path(path).name == "human_approval_decision.json":
            decision_published.set()
            assert finish_claim.wait(timeout=10), "finish_claim not signaled"
        return created

    monkeypatch.setattr(approval_mod, "exclusive_write_json", gated_excl)

    def claimer() -> None:
        result, _decision = apply_human_approval(
            run_dir=run_dir,
            callback_token=request["callback_token"],
            telegram_user_id=71,
            telegram_chat_id=71,
            allowed_user_id=71,
            allowed_chat_id=71,
        )
        claim_results.append(result)

    def mismatched_recreate() -> None:
        assert decision_published.wait(timeout=10), "decision not published"
        recreate_started.set()
        try:
            create_approval_request(
                run_dir=run_dir,
                task="docs/tasks/DX-01.md",
                base_commit=base,
                worktree=worktree,
                review_report="review-mismatch.json",
                task_id="DX-01",
            )
        except BaseException as exc:  # noqa: BLE001 — capture for assertion
            recreate_errors.append(exc)

    claim = threading.Thread(target=claimer)
    recreate = threading.Thread(target=mismatched_recreate)
    claim.start()
    assert decision_published.wait(timeout=10), "claimer did not publish decision"
    recreate.start()
    assert recreate_started.wait(timeout=10), "recreate did not start"
    time.sleep(0.05)
    # Holding claim lock: status not yet HUMAN_APPROVED; request unchanged.
    assert load_request(run_dir)["callback_token"] == request["callback_token"]
    assert load_request(run_dir)["review_report"] == "review.json"
    finish_claim.set()
    claim.join(timeout=30)
    recreate.join(timeout=30)
    assert not claim.is_alive()
    assert not recreate.is_alive()

    assert claim_results == ["accepted"]
    assert len(recreate_errors) == 1
    assert isinstance(recreate_errors[0], ApprovalError)
    assert "binding mismatch" in str(recreate_errors[0])
    assert read_status(run_dir) == STATUS_HUMAN_APPROVED
    final_request = load_request(run_dir)
    assert final_request["callback_token"] == request["callback_token"]
    assert final_request["review_report"] == "review.json"
    assert final_request["diff_hash"] == request["diff_hash"]
    assert load_decision(run_dir)["callback_token"] == request["callback_token"]


def test_blocked_with_no_request_cannot_open_gate(
    git_worktree: tuple[Path, str],
    tmp_path: Path,
) -> None:
    """New approval request must not open from BLOCKED (only from technical APPROVED)."""
    worktree, base = git_worktree
    run_dir = tmp_path / "runs" / "run-blocked-no-request"
    run_dir.mkdir(parents=True)
    approval_mod.write_status(run_dir, STATUS_BLOCKED)

    with pytest.raises(ApprovalError, match="only open from technical APPROVED"):
        create_approval_request(
            run_dir=run_dir,
            task="docs/tasks/DX-01.md",
            base_commit=base,
            worktree=worktree,
            review_report="review.json",
            task_id="DX-01",
        )

    assert read_status(run_dir) == STATUS_BLOCKED
    assert not (run_dir / "human_approval_request.json").exists()
    assert load_decision(run_dir) is None
    assert not (run_dir / "telegram_notify.json").exists()


def test_blocked_with_identical_request_no_op_without_reopening(
    git_worktree: tuple[Path, str],
    tmp_path: Path,
) -> None:
    """Identical retry while BLOCKED must not reopen to AWAITING or rotate token."""
    worktree, base = git_worktree
    run_dir = tmp_path / "runs" / "run-blocked-identical"
    run_dir.mkdir(parents=True)
    _arm_technical_approved(run_dir)
    request = create_approval_request(
        run_dir=run_dir,
        task="docs/tasks/DX-01.md",
        base_commit=base,
        worktree=worktree,
        review_report="review.json",
        task_id="DX-01",
    )
    approval_mod.write_status(run_dir, STATUS_BLOCKED)
    token = request["callback_token"]

    again = create_approval_request(
        run_dir=run_dir,
        task="docs/tasks/DX-01.md",
        base_commit=base,
        worktree=worktree,
        review_report="review.json",
        task_id="DX-01",
    )
    assert again["callback_token"] == token
    assert read_status(run_dir) == STATUS_BLOCKED
    assert load_request(run_dir)["callback_token"] == token
    assert load_decision(run_dir) is None
    assert enqueue_notification(
        run_dir=run_dir,
        kind="awaiting_human_approval",
        summary="should not publish button from BLOCKED",
    ) is None
    assert not (run_dir / "telegram_notify.json").exists()


def test_human_approved_retry_retains_state_without_pending_notification(
    git_worktree: tuple[Path, str],
    tmp_path: Path,
) -> None:
    """After HUMAN_APPROVED, identical recreate retains final state and emits no pending notify."""
    worktree, base = git_worktree
    run_dir = tmp_path / "runs" / "run-human-approved-retry"
    run_dir.mkdir(parents=True)
    _arm_technical_approved(run_dir)
    request = create_approval_request(
        run_dir=run_dir,
        task="docs/tasks/DX-01.md",
        base_commit=base,
        worktree=worktree,
        review_report="review.json",
        task_id="DX-01",
    )
    first_notify = enqueue_notification(
        run_dir=run_dir,
        kind="awaiting_human_approval",
        summary="pending",
        report_hint="review.json",
    )
    assert first_notify is not None and first_notify["offer_approval_button"] is True
    assert _approve(run_dir, request, user_id=77)[0] == "accepted"
    assert read_status(run_dir) == STATUS_HUMAN_APPROVED
    decision = load_decision(run_dir)
    # Clear outbox so a mistaken republish would be visible as a new pending entry.
    notify_path = run_dir / "telegram_notify.json"
    if notify_path.is_file():
        notify_path.unlink()

    retained = create_approval_request(
        run_dir=run_dir,
        task="docs/tasks/DX-01.md",
        base_commit=base,
        worktree=worktree,
        review_report="review.json",
        task_id="DX-01",
    )
    assert retained["callback_token"] == request["callback_token"]
    assert read_status(run_dir) == STATUS_HUMAN_APPROVED
    assert load_decision(run_dir) == decision

    assert enqueue_notification(
        run_dir=run_dir,
        kind="awaiting_human_approval",
        summary="must not emit after HUMAN_APPROVED",
        report_hint="review.json",
    ) is None
    assert not notify_path.exists()


def test_create_callback_notification_race_skips_stale_button(
    monkeypatch: pytest.MonkeyPatch,
    git_worktree: tuple[Path, str],
    tmp_path: Path,
) -> None:
    """
    Callback between request creation and awaiting notify must not publish a stale button.

    enqueue_notification is lock-coordinated and conditional on still-AWAITING with
    matching pending request and no valid decision.
    """
    worktree, base = git_worktree
    run_dir = tmp_path / "runs" / "run-notify-race"
    run_dir.mkdir(parents=True)
    _arm_technical_approved(run_dir)
    request = create_approval_request(
        run_dir=run_dir,
        task="docs/tasks/DX-01.md",
        base_commit=base,
        worktree=worktree,
        review_report="review.json",
        task_id="DX-01",
    )
    assert read_status(run_dir) == STATUS_AWAITING

    # Simulate the cli race window: create returned, callback claims, then notify.
    assert _approve(run_dir, request, user_id=88)[0] == "accepted"
    assert read_status(run_dir) == STATUS_HUMAN_APPROVED

    assert enqueue_notification(
        run_dir=run_dir,
        kind="awaiting_human_approval",
        summary="stale button must not appear",
        report_hint="review.json",
    ) is None
    assert not (run_dir / "telegram_notify.json").exists()
    assert read_status(run_dir) == STATUS_HUMAN_APPROVED

    # Concurrent variant: claim holds lock mid-decision while notify waits, then
    # notify must still observe HUMAN_APPROVED and skip the button.
    run2 = tmp_path / "runs" / "run-notify-race-locked"
    run2.mkdir(parents=True)
    _arm_technical_approved(run2)
    req2 = create_approval_request(
        run_dir=run2,
        task="docs/tasks/DX-01.md",
        base_commit=base,
        worktree=worktree,
        review_report="review.json",
        task_id="DX-01",
    )

    original_excl = approval_mod.exclusive_write_json
    decision_published = threading.Event()
    finish_claim = threading.Event()
    notify_results: list[dict | None] = []

    def gated_excl(path: Path, payload: dict, mode: int = 0o600) -> bool:
        created = original_excl(path, payload, mode=mode)
        if created and Path(path).name == "human_approval_decision.json":
            decision_published.set()
            assert finish_claim.wait(timeout=10), "finish_claim not signaled"
        return created

    monkeypatch.setattr(approval_mod, "exclusive_write_json", gated_excl)

    def claimer() -> None:
        apply_human_approval(
            run_dir=run2,
            callback_token=req2["callback_token"],
            telegram_user_id=89,
            telegram_chat_id=89,
            allowed_user_id=89,
            allowed_chat_id=89,
        )

    def notifier() -> None:
        assert decision_published.wait(timeout=10), "decision not published"
        notify_results.append(
            enqueue_notification(
                run_dir=run2,
                kind="awaiting_human_approval",
                summary="racing notify",
                report_hint="review.json",
            )
        )

    claim = threading.Thread(target=claimer)
    notify = threading.Thread(target=notifier)
    claim.start()
    assert decision_published.wait(timeout=10), "claimer did not publish"
    notify.start()
    time.sleep(0.05)
    finish_claim.set()
    claim.join(timeout=30)
    notify.join(timeout=30)
    assert not claim.is_alive() and not notify.is_alive()
    assert notify_results == [None]
    assert read_status(run2) == STATUS_HUMAN_APPROVED
    assert not (run2 / "telegram_notify.json").exists()


def test_blocked_notify_has_no_approval_button(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "run-blocked"
    run_dir.mkdir(parents=True)
    (run_dir / "status").write_text(STATUS_BLOCKED + "\n", encoding="utf-8")
    payload = enqueue_notification(
        run_dir=run_dir,
        kind="blocked",
        summary="blocked for tests",
        report_hint="review-1.json",
    )
    assert payload["offer_approval_button"] is False
    assert "callback_token" not in payload


def test_exclusive_write_partial_reader_and_interrupt_safety(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Final pathname appears only after fsynced temp + hard-link; interrupt leaves no partial."""
    target = tmp_path / "human_approval_decision.json"
    payload = {"schema_version": 1, "decision": "approve", "run_id": "r1"}

    gate = threading.Event()
    mid_write = threading.Event()
    original_fsync = os.fsync

    def gated_fsync(fd: int) -> None:
        mid_write.set()
        assert gate.wait(timeout=10), "gate not opened"
        return original_fsync(fd)

    monkeypatch.setattr(atomic_mod.os, "fsync", gated_fsync)

    results: list[bool] = []

    def publisher() -> None:
        results.append(exclusive_write_json(target, payload))

    thread = threading.Thread(target=publisher)
    thread.start()
    assert mid_write.wait(timeout=10)
    assert not target.exists(), "final path must not exist before publish"
    gate.set()
    thread.join(timeout=10)
    assert results == [True]
    assert json.loads(target.read_text(encoding="utf-8")) == payload

    # Interrupt before hard-link: no final path, temp cleaned.
    target2 = tmp_path / "decision-interrupt.json"
    original_link = os.link

    def boom_link(src: str, dst: str) -> None:
        raise RuntimeError("simulated crash before link")

    monkeypatch.setattr(atomic_mod.os, "link", boom_link)
    monkeypatch.setattr(atomic_mod.os, "fsync", original_fsync)
    with pytest.raises(RuntimeError, match="simulated crash"):
        exclusive_write_json(target2, payload)
    assert not target2.exists()
    leftovers = list(tmp_path.glob(".decision-interrupt.json.*.tmp"))
    assert leftovers == []
    monkeypatch.setattr(atomic_mod.os, "link", original_link)


def test_exclusive_write_concurrent_one_winner(tmp_path: Path) -> None:
    target = tmp_path / "winner.json"
    workers = 8
    barrier = threading.Barrier(workers)
    outcomes: list[bool] = []
    guard = threading.Lock()

    def worker(idx: int) -> None:
        barrier.wait(timeout=10)
        won = exclusive_write_json(target, {"winner": idx, "schema_version": 1})
        with guard:
            outcomes.append(won)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == workers - 1
    body = json.loads(target.read_text(encoding="utf-8"))
    assert body["schema_version"] == 1
    assert isinstance(body["winner"], int)


def _cross_process_claim_worker(args: tuple) -> None:
    """
    Spawn-safe worker: file barrier, then claim callback; put result on queue.

    args = (agents_dir, run_dir, callback_token, user_id, chat_id,
            ready_path, go_path, queue)
    """
    import sys
    from pathlib import Path

    (
        agents_dir,
        run_dir,
        callback_token,
        user_id,
        chat_id,
        ready_path,
        go_path,
        queue,
    ) = args
    if agents_dir not in sys.path:
        sys.path.insert(0, agents_dir)
    from dx.approval import apply_human_approval

    Path(ready_path).write_text("1", encoding="utf-8")
    deadline = time.time() + 30
    while time.time() < deadline:
        if Path(go_path).is_file():
            break
        time.sleep(0.01)
    else:
        queue.put(("error", {"error": "go signal not received"}))
        return
    result, decision = apply_human_approval(
        run_dir=Path(run_dir),
        callback_token=callback_token,
        telegram_user_id=user_id,
        telegram_chat_id=chat_id,
        allowed_user_id=user_id,
        allowed_chat_id=chat_id,
    )
    queue.put((result, decision))


def test_concurrent_callback_claim_exactly_one_accepted(
    git_worktree: tuple[Path, str],
    tmp_path: Path,
) -> None:
    """Eight synchronized handlers: exactly one accepts; replays keep the original decision."""
    worktree, base = git_worktree
    run_dir = tmp_path / "runs" / "run-concurrent"
    run_dir.mkdir(parents=True)
    _arm_technical_approved(run_dir)
    request = create_approval_request(
        run_dir=run_dir,
        task="docs/tasks/DX-01.md",
        base_commit=base,
        worktree=worktree,
        review_report="review.json",
    )
    kwargs = dict(
        run_dir=run_dir,
        callback_token=request["callback_token"],
        telegram_user_id=13,
        telegram_chat_id=13,
        allowed_user_id=13,
        allowed_chat_id=13,
    )
    workers = 8
    barrier = threading.Barrier(workers)
    results: list[str] = []
    returned_decisions: list[dict] = []
    guard = threading.Lock()

    def handler() -> None:
        barrier.wait(timeout=10)
        result, decision = apply_human_approval(**kwargs)
        with guard:
            results.append(result)
            returned_decisions.append(decision)

    threads = [threading.Thread(target=handler) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    assert results.count("accepted") == 1, results
    assert results.count("idempotent_replay") == workers - 1, results
    final = load_decision(run_dir)
    assert final is not None
    assert final["decision"] == "approve"
    assert final["diff_hash"] == request["diff_hash"]
    assert read_status(run_dir) == STATUS_HUMAN_APPROVED
    assert load_request(run_dir)["token_consumed"] is True
    # Racing writers must not overwrite audit timestamps / decision payload.
    assert all(decision == final for decision in returned_decisions)
    assert len({decision.get("decided_at") for decision in returned_decisions}) == 1


def test_cross_process_callback_claim_exactly_one_accepted(
    git_worktree: tuple[Path, str],
    tmp_path: Path,
) -> None:
    """Cross-process race: flock + exclusive hard-link still yield one claim."""
    import multiprocessing as mp

    worktree, base = git_worktree
    run_dir = tmp_path / "runs" / "run-cross-process"
    run_dir.mkdir(parents=True)
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    _arm_technical_approved(run_dir)
    request = create_approval_request(
        run_dir=run_dir,
        task="docs/tasks/DX-01.md",
        base_commit=base,
        worktree=worktree,
        review_report="review.json",
    )
    workers = 8
    go_path = sync_dir / "go"
    ready_paths = [sync_dir / f"ready-{i}" for i in range(workers)]
    ctx = mp.get_context("spawn")
    queue: mp.Queue = ctx.Queue()
    processes = []
    for i in range(workers):
        args = (
            str(AGENTS_DIR),
            str(run_dir),
            request["callback_token"],
            17,
            17,
            str(ready_paths[i]),
            str(go_path),
            queue,
        )
        process = ctx.Process(target=_cross_process_claim_worker, args=(args,))
        processes.append(process)
        process.start()

    deadline = time.time() + 30
    while time.time() < deadline:
        if all(path.is_file() for path in ready_paths):
            break
        time.sleep(0.01)
    else:
        for process in processes:
            process.terminate()
            process.join(timeout=5)
        raise TimeoutError("workers did not become ready")

    go_path.write_text("go", encoding="utf-8")
    outcomes: list[tuple[str, dict]] = []
    for _ in range(workers):
        outcomes.append(queue.get(timeout=30))
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    results = [item[0] for item in outcomes]
    returned_decisions = [item[1] for item in outcomes]
    assert "error" not in results, outcomes
    assert results.count("accepted") == 1, results
    assert results.count("idempotent_replay") == workers - 1, results
    final = load_decision(run_dir)
    assert final is not None
    assert all(decision == final for decision in returned_decisions)
    assert len({decision.get("decided_at") for decision in returned_decisions}) == 1
    assert read_status(run_dir) == STATUS_HUMAN_APPROVED
    assert load_request(run_dir)["token_consumed"] is True


def test_external_state_root_discovers_nested_project_runs(
    git_worktree: tuple[Path, str], tmp_path: Path
) -> None:
    worktree, base = git_worktree
    state_root = tmp_path / "state"
    run_dir = state_root / "projects" / "repo-id" / "runs" / "ag-01-run"
    run_dir.mkdir(parents=True)
    _arm_technical_approved(run_dir)
    request = create_approval_request(
        run_dir=run_dir,
        task="docs/tasks/AG-01.md",
        base_commit=base,
        worktree=worktree,
        review_report="review.json",
    )
    enqueue_notification(
        run_dir=run_dir,
        kind="awaiting_human_approval",
        summary="ready",
        report_hint="review.json",
    )

    assert approval_mod.find_run_dir_by_token(
        state_root, request["callback_token"]
    ) == run_dir
    pending = approval_mod.list_pending_notifications(state_root)
    assert len(pending) == 1
    assert pending[0][0] == run_dir
