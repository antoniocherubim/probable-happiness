"""DX-04 safe continuation after the reviewed iteration budget is exhausted."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS = REPO_ROOT / "scripts" / "agents"
sys.path.insert(0, str(AGENTS))

from dx.approval import compute_diff_hash, read_status, write_status  # noqa: E402
from dx.atomic import atomic_write_json  # noqa: E402
from dx.profile import ProjectProfile  # noqa: E402
from dx.runstate import (  # noqa: E402
    ITERATION_BUDGET,
    MAX_ADDITIONAL_ITERATIONS,
    IterationBudgetError,
    authorize_iteration_extension,
    load_iteration_budget,
    plan_resume,
    write_run_metadata,
)


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args],
        text=True,
        stderr=subprocess.STDOUT,
    ).strip()


def make_exhausted_run(
    tmp_path: Path,
    *,
    reason: str = "max_review_iterations",
) -> dict[str, object]:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "dx04@example.com")
    git(repo, "config", "user.name", "DX-04 Test")
    task = repo / "docs" / "tasks" / "DX-04.md"
    task.parent.mkdir(parents=True)
    task.write_text("# DX-04 — Continuação segura\n", encoding="utf-8")
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    worktree = tmp_path / "worktree"
    git(repo, "worktree", "add", "--detach", str(worktree), base)
    (worktree / "app.txt").write_text("base\niterations 1-3\n", encoding="utf-8")

    run_dir = tmp_path / "state" / "runs" / "dx-04-run"
    run_dir.mkdir(parents=True)
    write_run_metadata(
        run_dir,
        {
            "repo": str(repo.resolve()),
            "task_file": "docs/tasks/DX-04.md",
            "base_commit": base,
            "worktree": str(worktree.resolve()),
            "max_iterations": 3,
            "env_file": None,
            "profile": ProjectProfile().public_dict(),
            "delivery": {"mode": "none"},
        },
    )
    (run_dir / "iteration").write_text("3\n", encoding="utf-8")
    (run_dir / "cursor-3.json").write_text(
        json.dumps({"summary": "executor iteration 3"}),
        encoding="utf-8",
    )
    feedback = {
        "status": "CHANGES_REQUESTED",
        "summary": "KEEP_THIS_EXACT_FEEDBACK",
        "findings": [
            {
                "severity": "high",
                "title": "Missing behavior",
                "details": "Implement the final edge case.",
                "files": ["app.txt"],
            }
        ],
        "tests_required": ["pytest -q"],
    }
    review_path = run_dir / "review-3.json"
    review_path.write_text(json.dumps(feedback, sort_keys=True), encoding="utf-8")
    snapshot = compute_diff_hash(worktree, base)
    atomic_write_json(
        run_dir / "review-3-snapshot.json",
        {"schema_version": 1, "iteration": 3, "diff_hash": snapshot},
    )
    atomic_write_json(
        run_dir / "reviewer-3-result.json",
        {
            "schema_version": 1,
            "phase": "reviewer",
            "iteration": 3,
            "state": "completed",
            "exit_code": 0,
        },
    )
    atomic_write_json(
        run_dir / "failure.json",
        {
            "schema_version": 1,
            "reason": reason,
            "phase": "loop",
            "iteration": 3,
            "report": "review-3.json",
            "recorded_at": "2026-07-23T00:00:00Z",
        },
    )
    write_status(run_dir, "BLOCKED")
    return {
        "repo": repo,
        "worktree": worktree,
        "run_dir": run_dir,
        "base": base,
        "feedback": review_path.read_text(encoding="utf-8"),
    }


def test_blocked_limit_plus_three_continues_at_iteration_four(tmp_path: Path) -> None:
    env = make_exhausted_run(tmp_path)
    run_json_before = (env["run_dir"] / "run.json").read_bytes()
    result = authorize_iteration_extension(env["run_dir"], 3)
    plan = plan_resume(env["run_dir"])
    budget = load_iteration_budget(env["run_dir"], 3)

    assert result["result"] == "authorized"
    assert result["previous_limit"] == 3
    assert result["effective_limit"] == 6
    assert plan["resume_phase"] == "executor"
    assert plan["iteration"] == 4
    assert plan["original_max_iterations"] == 3
    assert plan["effective_max_iterations"] == 6
    assert budget["effective_limit"] == 6
    assert budget["extensions"][0]["origin"] == "cli"
    assert len(budget["extensions"][0]["idempotency_id"]) == 64
    assert (env["run_dir"] / "run.json").read_bytes() == run_json_before
    assert read_status(env["run_dir"]) == "CHANGES_REQUESTED"


def test_replay_after_each_interruption_point_never_adds_twice(tmp_path: Path) -> None:
    env = make_exhausted_run(tmp_path)
    first = authorize_iteration_extension(env["run_dir"], 3)

    # Crash after ledger but before status.
    write_status(env["run_dir"], "BLOCKED")
    pending_plan = plan_resume(env["run_dir"])
    assert pending_plan["resume_phase"] == "executor"
    assert pending_plan["iteration"] == 4
    second = authorize_iteration_extension(env["run_dir"], 3)
    assert second["result"] == "idempotent_replay"
    assert read_status(env["run_dir"]) == "CHANGES_REQUESTED"

    # Crash before/during iteration 4 executor.
    (env["run_dir"] / "iteration").write_text("4\n", encoding="utf-8")
    write_status(env["run_dir"], "EXECUTING")
    third = authorize_iteration_extension(env["run_dir"], 3)
    assert third["result"] == "idempotent_replay"
    assert third["idempotency_id"] == first["idempotency_id"]
    assert len(load_iteration_budget(env["run_dir"], 3)["extensions"]) == 1


def test_two_concurrent_authorizations_create_one_extension(tmp_path: Path) -> None:
    env = make_exhausted_run(tmp_path)

    def attempt() -> str:
        try:
            return str(authorize_iteration_extension(env["run_dir"], 3)["result"])
        except (IterationBudgetError, BlockingIOError):
            return "locked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: attempt(), range(2)))
    assert outcomes.count("authorized") == 1
    assert set(outcomes) <= {"authorized", "idempotent_replay", "locked"}
    assert len(load_iteration_budget(env["run_dir"], 3)["extensions"]) == 1


@pytest.mark.parametrize("lock_name", [".resume.lock", ".delivery.lock"])
def test_active_resume_or_delivery_lock_refuses_authorization(
    tmp_path: Path,
    lock_name: str,
) -> None:
    env = make_exhausted_run(tmp_path)
    lock_path = env["run_dir"] / lock_name
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises((IterationBudgetError, BlockingIOError)):
            authorize_iteration_extension(env["run_dir"], 3)
    finally:
        os.close(fd)
    assert not (env["run_dir"] / ITERATION_BUDGET).exists()


def test_drift_refuses_extension_without_mutation(tmp_path: Path) -> None:
    env = make_exhausted_run(tmp_path)
    (env["worktree"] / "app.txt").write_text("drift\n", encoding="utf-8")
    before = {
        path.name: path.read_bytes()
        for path in env["run_dir"].iterdir()
        if path.is_file() and not path.name.startswith(".")
    }
    with pytest.raises(IterationBudgetError, match="drifted"):
        authorize_iteration_extension(env["run_dir"], 3)
    after = {
        path.name: path.read_bytes()
        for path in env["run_dir"].iterdir()
        if path.is_file() and not path.name.startswith(".")
    }
    assert after == before
    assert not (env["run_dir"] / ITERATION_BUDGET).exists()
    assert read_status(env["run_dir"]) == "BLOCKED"


def test_other_block_reason_is_refused_with_actual_reason(tmp_path: Path) -> None:
    env = make_exhausted_run(tmp_path, reason="validation_failed")
    with pytest.raises(IterationBudgetError, match="validation_failed"):
        authorize_iteration_extension(env["run_dir"], 3)
    assert not (env["run_dir"] / ITERATION_BUDGET).exists()


@pytest.mark.parametrize(
    "status",
    [
        "APPROVED",
        "AWAITING_HUMAN_APPROVAL",
        "HUMAN_APPROVED",
        "DELIVERING",
        "PUSHED",
        "DELIVERY_FAILED",
    ],
)
def test_approval_and_delivery_states_are_never_extendable(
    tmp_path: Path,
    status: str,
) -> None:
    env = make_exhausted_run(tmp_path)
    write_status(env["run_dir"], status)
    with pytest.raises(IterationBudgetError, match=status):
        authorize_iteration_extension(env["run_dir"], 3)
    assert not (env["run_dir"] / ITERATION_BUDGET).exists()


@pytest.mark.parametrize(
    "value",
    [0, -1, MAX_ADDITIONAL_ITERATIONS + 1, True, 1.5, "3"],
)
def test_invalid_values_are_refused_without_mutation(tmp_path: Path, value: object) -> None:
    env = make_exhausted_run(tmp_path)
    status_before = read_status(env["run_dir"])
    with pytest.raises(IterationBudgetError, match="between 1 and"):
        authorize_iteration_extension(env["run_dir"], value)  # type: ignore[arg-type]
    assert read_status(env["run_dir"]) == status_before
    assert not (env["run_dir"] / ITERATION_BUDGET).exists()


def test_legacy_reason_is_migratable_and_legacy_run_without_ledger_still_plans(
    tmp_path: Path,
) -> None:
    env = make_exhausted_run(tmp_path, reason="max_iterations")
    legacy_plan = plan_resume(env["run_dir"], review_only=True)
    assert legacy_plan["resume_phase"] == "reviewer"
    assert legacy_plan["effective_max_iterations"] == 3
    assert not (env["run_dir"] / ITERATION_BUDGET).exists()

    authorized = authorize_iteration_extension(env["run_dir"], 3)
    assert authorized["effective_limit"] == 6
    assert load_iteration_budget(env["run_dir"], 3)["extensions"][0][
        "blocked_reason"
    ] == "max_iterations"


def test_review_only_remains_incompatible_with_budget_authorization(tmp_path: Path) -> None:
    env = make_exhausted_run(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            str(AGENTS / "telegram_bridge.py"),
            "resume-exec",
            "--run-dir",
            str(env["run_dir"]),
            "--review-only",
            "--additional-iterations",
            "3",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert completed.returncode == 2
    assert "cannot be combined" in completed.stderr
    assert not (env["run_dir"] / ITERATION_BUDGET).exists()


@pytest.mark.parametrize("value", ["0", "-1", "abc", str(MAX_ADDITIONAL_ITERATIONS + 1)])
def test_cli_rejects_invalid_budget_before_opening_resume_lock(
    tmp_path: Path,
    value: str,
) -> None:
    env = make_exhausted_run(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            str(AGENTS / "telegram_bridge.py"),
            "resume-exec",
            "--run-dir",
            str(env["run_dir"]),
            "--additional-iterations",
            value,
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert completed.returncode == 2
    assert "no run artifact was changed" in completed.stderr
    assert not (env["run_dir"] / ".resume.lock").exists()
    assert not (env["run_dir"] / ITERATION_BUDGET).exists()


def _write_fake_agents(tmp_path: Path, reviewer_status: str) -> tuple[Path, Path]:
    cursor = tmp_path / "fake-cursor"
    cursor.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            if [[ "${1:-}" == "status" ]]; then
              printf '%s\\n' "Logged in"
              exit 0
            fi
            last="${!#}"
            printf '%s' "$last" > "$AGENT_LOOP_RUN_DIR/captured-executor-prompt.txt"
            printf 'iteration\\n' >> "$AGENT_LOOP_WORKTREE/app.txt"
            printf '%s\\n' '{"summary":"executor continued; 1 passed, 0 failed"}'
            """
        ),
        encoding="utf-8",
    )
    cursor.chmod(0o755)
    codex = tmp_path / "fake-codex"
    codex.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            if [[ "${{1:-}}" == "login" ]]; then
              printf '%s\\n' "Logged in"
              exit 0
            fi
            output=""
            while [[ $# -gt 0 ]]; do
              if [[ "$1" == "--output-last-message" ]]; then
                output="$2"
                shift 2
              else
                shift
              fi
            done
            printf '%s\\n' '{{"status":"{reviewer_status}","summary":"reviewed","findings":[],"tests_required":[]}}' > "$output"
            """
        ),
        encoding="utf-8",
    )
    codex.chmod(0o755)
    return cursor, codex


def _run_extended_loop(env: dict[str, object], tmp_path: Path, reviewer_status: str) -> subprocess.CompletedProcess[str]:
    cursor, codex = _write_fake_agents(tmp_path, reviewer_status)
    environment = dict(os.environ)
    environment.update(
        {
            "CURSOR_AGENT_BIN": str(cursor),
            "CODEX_BIN": str(codex),
            "AGENT_LOOP_TOOL_ROOT": str(REPO_ROOT),
            "AGENT_LOOP_PYTHON": sys.executable,
            "AGENT_HUMAN_APPROVAL_TIMEOUT_SEC": "0",
        }
    )
    return subprocess.run(
        [
            str(REPO_ROOT / "agent-loop"),
            "resume",
            "--run-dir",
            str(env["run_dir"]),
            "--additional-iterations",
            "3",
        ],
        cwd=str(REPO_ROOT),
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )


def test_executor_receives_last_feedback_and_approval_reaches_human_gate(
    tmp_path: Path,
) -> None:
    env = make_exhausted_run(tmp_path)
    completed = _run_extended_loop(env, tmp_path, "APPROVED")
    assert completed.returncode == 2, completed.stdout + completed.stderr
    prompt = (env["run_dir"] / "captured-executor-prompt.txt").read_text(encoding="utf-8")
    assert prompt.count(env["feedback"]) == 1
    assert (env["run_dir"] / "cursor-4.json").is_file()
    assert (env["run_dir"] / "review-4.json").is_file()
    assert (env["run_dir"] / "human_approval_request.json").is_file()
    assert read_status(env["run_dir"]) == "AWAITING_HUMAN_APPROVAL"
    assert not (env["run_dir"] / "human_approval_decision.json").exists()
    assert not (env["run_dir"] / "delivery.json").exists()


def test_changes_requested_blocks_again_at_effective_limit(tmp_path: Path) -> None:
    env = make_exhausted_run(tmp_path)
    completed = _run_extended_loop(env, tmp_path, "CHANGES_REQUESTED")
    assert completed.returncode == 1, completed.stdout + completed.stderr
    failure = json.loads((env["run_dir"] / "failure.json").read_text(encoding="utf-8"))
    assert failure["reason"] == "max_review_iterations"
    assert failure["iteration"] == 6
    assert failure["report"] == "review-6.json"
    assert (env["run_dir"] / "cursor-4.json").is_file()
    assert (env["run_dir"] / "cursor-5.json").is_file()
    assert (env["run_dir"] / "cursor-6.json").is_file()
    assert read_status(env["run_dir"]) == "BLOCKED"
    assert load_iteration_budget(env["run_dir"], 3)["effective_limit"] == 6
    notification = json.loads(
        (env["run_dir"] / "telegram_notify.json").read_text(encoding="utf-8")
    )
    assert "worktree and last reviewer feedback were preserved" in notification["summary"]
    assert "--additional-iterations" in notification["summary"]
    assert notification["offer_approval_button"] is False


def test_budget_artifact_tampering_is_detected(tmp_path: Path) -> None:
    env = make_exhausted_run(tmp_path)
    authorize_iteration_extension(env["run_dir"], 3)
    budget_path = env["run_dir"] / ITERATION_BUDGET
    budget = json.loads(budget_path.read_text(encoding="utf-8"))
    budget["effective_limit"] = 9
    budget_path.write_text(json.dumps(budget), encoding="utf-8")
    with pytest.raises(IterationBudgetError, match="effective limit mismatch"):
        plan_resume(env["run_dir"])
