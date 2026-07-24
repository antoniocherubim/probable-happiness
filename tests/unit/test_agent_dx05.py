"""DX-05 async approval queue and delivery worker."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

AGENTS = Path(__file__).resolve().parents[2] / "scripts" / "agents"
sys.path.insert(0, str(AGENTS))

from dx.approval import (  # noqa: E402
    STATUS_DELIVERY_FAILED,
    STATUS_HUMAN_APPROVED,
    STATUS_PUSHED,
    apply_human_approval,
    apply_human_rejection,
    read_status,
    write_status,
)
from dx.atomic import atomic_write_json  # noqa: E402
from dx.bridge import Bridge  # noqa: E402
from dx.config import BridgeConfig  # noqa: E402
from dx.delivery import deliver_run  # noqa: E402
from dx.delivery_job import (  # noqa: E402
    JOB_FILENAME,
    DeliveryJobError,
    build_job_document,
    callback_delivery_message,
    decision_content_hash,
    discover_delivery_job_runs,
    ensure_delivery_job,
    load_delivery_job,
    process_delivery_run,
    run_delivery_worker,
    validate_job_document,
    validate_job_for_worker,
)
from dx.telegram import FakeTelegramAPI, TelegramClient  # noqa: E402
from test_agent_dx03 import make_delivery_run  # noqa: E402


TOKEN = "123456:DX05-TEST-TOKEN"


def _bridge_for_run(run_dir: Path, fake: FakeTelegramAPI) -> Bridge:
    config = BridgeConfig(
        bot_token=TOKEN,
        allowed_user_id=7,
        allowed_chat_id=7,
    )
    client = TelegramClient(TOKEN, transport=fake.as_transport())
    return Bridge(config, client, run_dir.parent)


def test_bridge_module_does_not_import_delivery() -> None:
    import dx.bridge as bridge_mod

    assert "dx.delivery" not in sys.modules or "delivery" not in dir(bridge_mod)
    assert not hasattr(bridge_mod, "deliver_run")
    source = Path(bridge_mod.__file__).read_text(encoding="utf-8")
    assert "from .delivery import" not in source
    assert "import delivery" not in source or "delivery_job" in source


def test_callback_never_calls_deliver_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = make_delivery_run(tmp_path / "case", approve=False)
    run_dir = env["run_dir"]
    request = json.loads((run_dir / "human_approval_request.json").read_text(encoding="utf-8"))

    def boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("deliver_run must not be called from the bridge")

    monkeypatch.setattr("dx.delivery.deliver_run", boom)
    fake = FakeTelegramAPI(allowed_token=TOKEN)
    bridge = _bridge_for_run(run_dir, fake)
    fake.push_callback(user_id=7, chat_id=7, data=request["callback_token"])
    bridge.process_updates_once()

    assert read_status(run_dir) == STATUS_HUMAN_APPROVED
    job = load_delivery_job(run_dir)
    assert job is not None
    assert job["status"] == "pending"
    assert job["branch"] == "cp-00"
    assert "callback_token" not in json.dumps(job)
    assert fake.answered_callbacks[-1]["text"] == "Aprovado; entrega enfileirada."


def test_callback_stays_immediate_when_remote_would_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_delivery_run(tmp_path / "slow", approve=False)
    run_dir = env["run_dir"]
    request = json.loads((run_dir / "human_approval_request.json").read_text(encoding="utf-8"))

    def hang(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        time.sleep(30)
        raise AssertionError("bridge must not wait on delivery")

    monkeypatch.setattr("dx.delivery.deliver_run", hang)
    fake = FakeTelegramAPI(allowed_token=TOKEN)
    bridge = _bridge_for_run(run_dir, fake)
    fake.push_callback(user_id=7, chat_id=7, data=request["callback_token"])
    started = time.monotonic()
    bridge.process_updates_once()
    elapsed = time.monotonic() - started
    assert elapsed < 2.0
    assert load_delivery_job(run_dir) is not None


def test_job_create_replay_and_divergence(tmp_path: Path) -> None:
    env = make_delivery_run(tmp_path / "job")
    run_dir = env["run_dir"]
    first = ensure_delivery_job(run_dir)
    assert first["result"] in {"created", "existing"}
    job = first["job"]
    assert isinstance(job, dict)
    second = ensure_delivery_job(run_dir)
    assert second["result"] == "existing"
    assert second["job"]["job_id"] == job["job_id"]

    tampered = dict(job)
    tampered["branch"] = "evil-branch"
    atomic_write_json(run_dir / JOB_FILENAME, tampered)
    blocked = ensure_delivery_job(run_dir)
    assert blocked["result"] == "blocked_divergent"
    assert read_status(run_dir) == STATUS_HUMAN_APPROVED
    with pytest.raises(DeliveryJobError, match="diverg"):
        validate_job_for_worker(run_dir)


def test_crash_between_decision_status_and_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = make_delivery_run(tmp_path / "crash", approve=False)
    run_dir = env["run_dir"]
    request = json.loads((run_dir / "human_approval_request.json").read_text(encoding="utf-8"))

    original_write_status = sys.modules["dx.approval"].write_status
    armed = {"trip": True}

    def crash_after_decision(path: Path, status: str) -> None:
        if armed["trip"] and status == STATUS_HUMAN_APPROVED:
            armed["trip"] = False
            # Publish decision already happened; skip status + job to simulate crash.
            return
        original_write_status(path, status)

    monkeypatch.setattr("dx.approval.write_status", crash_after_decision)
    result, decision = apply_human_approval(
        run_dir=run_dir,
        callback_token=request["callback_token"],
        telegram_user_id=7,
        telegram_chat_id=7,
        allowed_user_id=7,
        allowed_chat_id=7,
    )
    assert result == "accepted"
    assert decision
    assert (run_dir / "human_approval_decision.json").is_file()
    # Status may still be AWAITING; job may be absent.
    monkeypatch.setattr("dx.approval.write_status", original_write_status)
    replay, _ = apply_human_approval(
        run_dir=run_dir,
        callback_token=request["callback_token"],
        telegram_user_id=7,
        telegram_chat_id=7,
        allowed_user_id=7,
        allowed_chat_id=7,
    )
    assert replay == "idempotent_replay"
    assert read_status(run_dir) == STATUS_HUMAN_APPROVED
    assert load_delivery_job(run_dir) is not None


def test_legacy_approved_run_gets_deterministic_job_on_resume_path(tmp_path: Path) -> None:
    env = make_delivery_run(tmp_path / "legacy")
    run_dir = env["run_dir"]
    job_path = run_dir / JOB_FILENAME
    if job_path.exists():
        job_path.unlink()
    assert load_delivery_job(run_dir) is None
    created = ensure_delivery_job(run_dir)
    assert created["result"] == "created"
    again = ensure_delivery_job(run_dir)
    assert again["result"] == "existing"
    assert again["job"]["job_id"] == created["job"]["job_id"]


def test_rejection_and_delivery_none_never_create_job(tmp_path: Path) -> None:
    env = make_delivery_run(tmp_path / "reject", approve=False)
    run_dir = env["run_dir"]
    request = json.loads((run_dir / "human_approval_request.json").read_text(encoding="utf-8"))
    apply_human_rejection(
        run_dir=run_dir,
        callback_token=request["callback_token"],
        telegram_user_id=7,
        telegram_chat_id=7,
        allowed_user_id=7,
        allowed_chat_id=7,
    )
    assert load_delivery_job(run_dir) is None

    none_case = make_delivery_run(tmp_path / "none", approve=False)
    metadata = json.loads((none_case["run_dir"] / "run.json").read_text(encoding="utf-8"))
    metadata["delivery"] = {"mode": "none"}
    atomic_write_json(none_case["run_dir"] / "run.json", metadata)
    req = json.loads(
        (none_case["run_dir"] / "human_approval_request.json").read_text(encoding="utf-8")
    )
    apply_human_approval(
        run_dir=none_case["run_dir"],
        callback_token=req["callback_token"],
        telegram_user_id=7,
        telegram_chat_id=7,
        allowed_user_id=7,
        allowed_chat_id=7,
    )
    assert ensure_delivery_job(none_case["run_dir"])["result"] == "disabled"
    assert load_delivery_job(none_case["run_dir"]) is None
    assert callback_delivery_message(none_case["run_dir"]) == "Aprovado."


def test_worker_completes_same_safe_delivery(tmp_path: Path) -> None:
    env = make_delivery_run(tmp_path / "worker")
    run_dir = env["run_dir"]
    ensure_delivery_job(run_dir)
    result = process_delivery_run(run_dir)
    assert result["status"] == STATUS_PUSHED
    assert result["branch"] == "cp-00"
    # Idempotent second pass
    again = process_delivery_run(run_dir)
    assert again["status"] == STATUS_PUSHED
    assert again["commit_oid"] == result["commit_oid"]


def test_pushed_does_not_require_retroactive_job(tmp_path: Path) -> None:
    env = make_delivery_run(tmp_path / "pushed")
    run_dir = env["run_dir"]
    ensure_delivery_job(run_dir)
    deliver_run(run_dir)
    assert read_status(run_dir) == STATUS_PUSHED
    (run_dir / JOB_FILENAME).unlink(missing_ok=True)
    outcome = ensure_delivery_job(run_dir)
    assert outcome["result"] == "already_pushed"
    assert load_delivery_job(run_dir) is None
    assert "já publicada" in callback_delivery_message(run_dir)


def test_concurrent_callbacks_create_one_job(tmp_path: Path) -> None:
    env = make_delivery_run(tmp_path / "race", approve=False)
    run_dir = env["run_dir"]
    request = json.loads((run_dir / "human_approval_request.json").read_text(encoding="utf-8"))
    results: list[str] = []

    def claim() -> None:
        result, _ = apply_human_approval(
            run_dir=run_dir,
            callback_token=request["callback_token"],
            telegram_user_id=7,
            telegram_chat_id=7,
            allowed_user_id=7,
            allowed_chat_id=7,
        )
        results.append(result)

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["accepted", "idempotent_replay"] or results.count("accepted") == 1
    assert results.count("accepted") == 1
    jobs = list(run_dir.glob("delivery-job.json"))
    assert len(jobs) == 1


def test_concurrent_workers_are_idempotent(tmp_path: Path) -> None:
    env = make_delivery_run(tmp_path / "workers")
    run_dir = env["run_dir"]
    ensure_delivery_job(run_dir)
    outcomes: list[dict[str, Any]] = []
    errors: list[str] = []

    def work() -> None:
        try:
            outcomes.append(process_delivery_run(run_dir))
        except Exception as exc:  # noqa: BLE001 — collect for assertion
            errors.append(str(exc))

    threads = [threading.Thread(target=work) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert len(outcomes) == 2
    assert {item["status"] for item in outcomes} == {STATUS_PUSHED}
    assert outcomes[0]["commit_oid"] == outcomes[1]["commit_oid"]


def test_worker_once_and_continuous_discover(tmp_path: Path) -> None:
    env = make_delivery_run(tmp_path / "discover")
    run_dir = env["run_dir"]
    project_state = run_dir.parent.parent
    ensure_delivery_job(run_dir)
    assert discover_delivery_job_runs(project_state) == [run_dir.resolve()]
    once = run_delivery_worker(project_state, once=True)
    assert once["cycles"] == 1
    assert once["processed"]
    assert once["processed"][0]["ok"] is True
    continuous = run_delivery_worker(project_state, once=False, max_cycles=2, poll_interval=0.01)
    assert continuous["cycles"] == 2


def test_delivery_failed_message_preserves_approval(tmp_path: Path) -> None:
    env = make_delivery_run(tmp_path / "failed")
    run_dir = env["run_dir"]
    ensure_delivery_job(run_dir)
    write_status(run_dir, STATUS_DELIVERY_FAILED)
    atomic_write_json(
        run_dir / "delivery.json",
        {
            "schema_version": 1,
            "status": STATUS_DELIVERY_FAILED,
            "reason": "remote_branch_exists",
            "branch": "cp-00",
        },
    )
    assert "aprovação foi preservada" in callback_delivery_message(run_dir)


def test_mutated_worktree_blocks_worker_not_decision(tmp_path: Path) -> None:
    env = make_delivery_run(tmp_path / "mutate")
    run_dir = env["run_dir"]
    worktree = Path(env["worktree"])
    ensure_delivery_job(run_dir)
    (worktree / "app.txt").write_text("mutated after approval\n", encoding="utf-8")
    with pytest.raises(Exception, match="approved_snapshot_changed|snapshot"):
        process_delivery_run(run_dir)
    assert (run_dir / "human_approval_decision.json").is_file()
    assert load_delivery_job(run_dir) is not None


def test_job_binding_hashes_exclude_secrets(tmp_path: Path) -> None:
    env = make_delivery_run(tmp_path / "hash")
    run_dir = env["run_dir"]
    decision = json.loads((run_dir / "human_approval_decision.json").read_text(encoding="utf-8"))
    digest = decision_content_hash(decision)
    assert "callback_token" not in digest
    assert len(digest) == 64
    job = build_job_document(
        run_id=str(decision["run_id"]),
        decision=decision,
        remote="origin",
        branch="cp-00",
    )
    raw = json.dumps(job)
    assert "callback_token" not in raw
    assert TOKEN not in raw
    assert "file://" not in raw
    assert "http" not in raw
    validate_job_document(job, job)


def test_cli_delivery_worker_once(tmp_path: Path) -> None:
    import subprocess

    env = make_delivery_run(tmp_path / "cli")
    run_dir = env["run_dir"]
    ensure_delivery_job(run_dir)
    root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [
            str(root / "agent-loop"),
            "delivery-worker",
            "--run-dir",
            str(run_dir),
            "--once",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert read_status(run_dir) == STATUS_PUSHED
