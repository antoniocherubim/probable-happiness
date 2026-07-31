"""Local-only approval and documentation regressions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS = REPO_ROOT / "scripts" / "agents"
sys.path.insert(0, str(AGENTS))

from dx.approval import (  # noqa: E402
    STATUS_APPROVED,
    STATUS_HUMAN_APPROVED,
    apply_human_approval,
    create_approval_request,
    enqueue_notification,
    read_status,
)
from dx.bridge import Bridge  # noqa: E402
from dx.config import BridgeConfig  # noqa: E402
from dx.profile import ProfileError, load_project_profile  # noqa: E402
from dx.runstate import plan_resume, write_run_metadata  # noqa: E402
from dx.snapshot import (  # noqa: E402
    SnapshotError,
    _test_summary,
    build_snapshot_manifest,
    format_technical_summary,
    split_telegram_message,
    validate_documentation,
)
from dx.state_machine import RunEvent, transition_run  # noqa: E402
from dx.telegram import FakeTelegramAPI, TelegramClient  # noqa: E402


PROFILE = """
schema_version = 1

[validation]
commands = [["python3", "-m", "pytest", "-q"]]

[documentation]
required = true
required_paths = ["docs/release/{task_id}.md"]

"""


def mark_review_approved(run_dir: Path) -> None:
    transition_run(run_dir, RunEvent.RUN_STARTED)
    transition_run(run_dir, RunEvent.REVIEW_STARTED)
    transition_run(run_dir, RunEvent.REVIEW_APPROVED)


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args],
        text=True,
        stderr=subprocess.STDOUT,
    ).strip()


def make_local_run(tmp_path: Path, *, approve: bool = False) -> dict[str, object]:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "local-only@example.test")
    git(repo, "config", "user.name", "Local Only Test")
    (repo / ".gitignore").write_text(".agent-op/\n", encoding="utf-8")
    profile_path = repo / ".agent-loop" / "project.toml"
    profile_path.parent.mkdir()
    profile_path.write_text(textwrap.dedent(PROFILE), encoding="utf-8")
    task = repo / "docs" / "tasks" / "CP-00.md"
    task.parent.mkdir(parents=True)
    task.write_text("# CP-00 — Local only\n", encoding="utf-8")
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")

    worktree = tmp_path / "worktree"
    git(repo, "worktree", "add", "--detach", str(worktree), base)
    (worktree / "app.txt").write_text("base\nfeature\n", encoding="utf-8")
    documentation = worktree / "docs" / "release" / "CP-00.md"
    documentation.parent.mkdir(parents=True)
    documentation.write_text(
        "Behavior: local approval\nTests: pytest\nResidual risks: manual integration\n",
        encoding="utf-8",
    )

    run_dir = tmp_path / "state" / "runs" / "cp-00-run"
    run_dir.mkdir(parents=True)
    profile = load_project_profile(worktree)
    write_run_metadata(
        run_dir,
        {
            "repo": str(repo.resolve()),
            "task_file": "docs/tasks/CP-00.md",
            "base_commit": base,
            "worktree": str(worktree.resolve()),
            "max_iterations": 3,
            "env_file": None,
            "profile": profile.public_dict(),
        },
    )
    (run_dir / "iteration").write_text("1\n", encoding="utf-8")
    mark_review_approved(run_dir)
    reviewed_hash = build_snapshot_manifest(worktree, base)["snapshot_hash"]
    request = create_approval_request(
        run_dir=run_dir,
        task="docs/tasks/CP-00.md",
        task_id="CP-00",
        base_commit=base,
        worktree=worktree,
        review_report="review-1.json",
        diff_hash=reviewed_hash,
    )
    if approve:
        result, _decision = apply_human_approval(
            run_dir=run_dir,
            callback_token=request["callback_token"],
            telegram_user_id=7,
            telegram_chat_id=7,
            allowed_user_id=7,
            allowed_chat_id=7,
        )
        assert result == "accepted"
    return {
        "repo": repo,
        "worktree": worktree,
        "run_dir": run_dir,
        "base": base,
        "profile": profile,
        "request": request,
    }


def test_profile_rejects_every_delivery_section(tmp_path: Path) -> None:
    env = make_local_run(tmp_path / "valid")
    profile = env["profile"]
    assert "delivery" not in profile.public_dict()

    for obsolete in (
        '[delivery]\nmode = "push_branch"\n',
        '[delivery]\nmode = "none"\n',
        '[delivery]\nmode = "none"\nremote = "origin"\n',
    ):
        repo = tmp_path / f"bad-{abs(hash(obsolete))}"
        profile_path = repo / ".agent-loop" / "project.toml"
        profile_path.parent.mkdir(parents=True)
        profile_path.write_text(
            "schema_version = 1\n" + obsolete,
            encoding="utf-8",
        )
        with pytest.raises(ProfileError):
            load_project_profile(repo)


def test_remote_git_surface_is_absent() -> None:
    for path in (
        REPO_ROOT / "agent-loop",
        AGENTS / "run_task.sh",
        AGENTS / "dx" / "cli.py",
        AGENTS / "dx" / "bridge.py",
        AGENTS / "dx" / "approval.py",
    ):
        source = path.read_text(encoding="utf-8")
        assert ".delivery.lock" not in source
        assert "DELIVERY_FAILED" not in source
        assert "PUSHED" not in source
    run_task = (AGENTS / "run_task.sh").read_text(encoding="utf-8")
    assert "Do not commit, push, merge, deploy, or access secrets." in run_task
    assert "git push" not in run_task


def test_approval_is_local_terminal(
    tmp_path: Path,
) -> None:
    env = make_local_run(tmp_path, approve=True)
    run_dir = env["run_dir"]
    assert read_status(run_dir) == STATUS_HUMAN_APPROVED
    assert plan_resume(run_dir)["resume_phase"] == "complete"


def test_required_documentation_and_unsafe_paths(tmp_path: Path) -> None:
    env = make_local_run(tmp_path / "docs")
    manifest = build_snapshot_manifest(env["worktree"], env["base"])
    assert validate_documentation(
        env["profile"], manifest, task_id="CP-00", task_slug="cp-00"
    ) == ["docs/release/CP-00.md"]

    os.unlink(env["worktree"] / "docs" / "release" / "CP-00.md")
    with pytest.raises(SnapshotError, match="required documentation"):
        validate_documentation(
            env["profile"],
            build_snapshot_manifest(env["worktree"], env["base"]),
            task_id="CP-00",
            task_slug="cp-00",
        )

    for value in ("../ROADMAP.md", "/tmp/report.md"):
        repo = tmp_path / f"unsafe-{abs(hash(value))}"
        profile_path = repo / ".agent-loop" / "project.toml"
        profile_path.parent.mkdir(parents=True)
        profile_path.write_text(
            "schema_version = 1\n[documentation]\n"
            f'required = true\nrequired_paths = ["{value}"]\n',
            encoding="utf-8",
        )
        with pytest.raises(ProfileError):
            load_project_profile(repo)


def test_ignored_symlink_and_special_file_handling(tmp_path: Path) -> None:
    env = make_local_run(tmp_path)
    ignored = env["worktree"] / ".agent-op"
    ignored.mkdir()
    (ignored / "current").symlink_to("/tmp/not-followed")
    manifest = build_snapshot_manifest(env["worktree"], env["base"])
    assert ".agent-op/current" not in {entry["path"] for entry in manifest["entries"]}

    fifo = env["worktree"] / "unsafe.fifo"
    os.mkfifo(fifo)
    try:
        with pytest.raises(SnapshotError, match="special file"):
            build_snapshot_manifest(env["worktree"], env["base"])
    finally:
        fifo.unlink()


def test_summary_is_sanitized_and_chunked() -> None:
    summary = {
        "task_id": "CP-00",
        "task_title": "<b>Unsafe</b>",
        "repository": "repo",
        "base_commit": "a" * 40,
        "reviewer_status": "APPROVED",
        "iteration": 2,
        "max_iterations": 3,
        "file_count": 2,
        "additions": 20,
        "deletions": 3,
        "test_counts": {"passed": 47, "skipped": 1, "failed": 0, "errors": 0},
        "validation_status": "passed",
        "reviewed_diff_hash": "b" * 64,
        "files": ["app.py", "docs/CP-00.md"],
        "executor_summary": "token=abc password:secret https://user:pw@example.test/x",
        "test_commands": ["pytest -q"],
        "reviewer_summary": "\n".join(["reviewed safely"] * 400),
        "findings": [],
        "residual_risks": ["manual integration"],
        "documentation": ["docs/CP-00.md"],
    }
    chunks = split_telegram_message(format_technical_summary(summary), limit=600)
    rendered = "\n".join(chunks)
    assert len(chunks) > 1
    assert "token=[REDACTED]" in rendered
    assert "password=[REDACTED]" in rendered
    assert "https://user:pw@" not in rendered


def test_test_summary_uses_only_latest_authoritative_validation(
    tmp_path: Path,
) -> None:
    env = make_local_run(tmp_path)
    run_dir = env["run_dir"]
    (run_dir / "cursor-3.json").write_text(
        "documentation says 999 passed, 600 errors\n",
        encoding="utf-8",
    )
    logs = {
        1: "",
        2: "1 passed in 0.45s\n348 passed, 379 deselected in 19.29s\n",
        3: (
            "31 passed, 1 warning in 56.29s\n"
            "qa03b_security_matrix_ok passed=31 failed=0 skipped=0\n"
            "SELF-TEST OK: expected negative cases follow\n"
            "600 errors expected by fixture documentation\n"
        ),
        4: "",
    }
    for index, text in logs.items():
        (run_dir / f"validation-{index}.log").write_text(text, encoding="utf-8")
        (run_dir / f"validation-{index}-result.json").write_text(
            json.dumps({"state": "completed", "exit_code": 0}),
            encoding="utf-8",
        )

    counts, commands, source = _test_summary(run_dir)

    assert counts == {"passed": 31, "failed": 0, "skipped": 0, "errors": 0}
    assert source == "validation-3.log"
    assert commands == ["python3 -m pytest -q"]


def test_test_summary_falls_back_to_last_single_line_summary(
    tmp_path: Path,
) -> None:
    env = make_local_run(tmp_path)
    run_dir = env["run_dir"]
    (run_dir / "validation-1.log").write_text(
        "1 passed in 0.10s\n47 passed, 1 skipped in 2.0s\n",
        encoding="utf-8",
    )
    (run_dir / "validation-1-result.json").write_text(
        json.dumps({"state": "completed", "exit_code": 0}),
        encoding="utf-8",
    )

    counts, _commands, source = _test_summary(run_dir)

    assert counts == {"passed": 47, "failed": 0, "skipped": 1, "errors": 0}
    assert source == "validation-1.log"


def test_multipart_terminal_notification_has_no_actions(tmp_path: Path) -> None:
    env = make_local_run(tmp_path)
    run_dir = env["run_dir"]
    messages = ["(1/2)\nfirst", "(2/2)\nlast"]
    enqueue_notification(
        run_dir=run_dir,
        kind="approved",
        summary="first",
        messages=messages,
    )
    fake = FakeTelegramAPI(allowed_token="123:fake")
    bridge = Bridge(
        BridgeConfig(
            bot_token="123:fake",
            allowed_chat_id=7,
        ),
        TelegramClient(
            "123:fake",
            api_base="http://telegram.test",
            transport=fake.as_transport(),
        ),
        run_dir.parent,
    )
    assert bridge.process_outbox_once() == 1
    assert "reply_markup" not in fake.sent_messages[0]
    assert "reply_markup" not in fake.sent_messages[1]
