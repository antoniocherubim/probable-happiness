"""PC-03 optional Telegram and local reviewed-snapshot completion."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS = REPO_ROOT / "scripts" / "agents"
sys.path.insert(0, str(AGENTS))

import dx.cli as cli_mod  # noqa: E402
from dx.approval import verify_reviewed_snapshot  # noqa: E402
from dx.atomic import atomic_write_json  # noqa: E402
from dx.profile import ProfileError, load_project_profile  # noqa: E402
from dx.runstate import RunStateError, plan_resume, write_run_metadata  # noqa: E402
from dx.snapshot import build_snapshot_manifest  # noqa: E402
from dx.state_machine import RunEvent, transition_run  # noqa: E402


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args],
        text=True,
        stderr=subprocess.STDOUT,
    ).strip()


def make_local_approved_run(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "pc03@example.com")
    git(repo, "config", "user.name", "PC-03 Test")
    profile = repo / ".agent-loop" / "project.toml"
    profile.parent.mkdir()
    profile.write_text(
        'schema_version = 1\n[approval]\nmode = "none"\n',
        encoding="utf-8",
    )
    task = repo / "docs" / "tasks" / "PC-03.md"
    task.parent.mkdir(parents=True)
    task.write_text("# PC-03\n", encoding="utf-8")
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    worktree = tmp_path / "worktree"
    git(repo, "worktree", "add", "--detach", str(worktree), base)
    (worktree / "app.txt").write_text("base\nchanged\n", encoding="utf-8")

    run_dir = tmp_path / "state" / "runs" / "pc-03-run"
    run_dir.mkdir(parents=True)
    write_run_metadata(
        run_dir,
        {
            "repo": str(repo.resolve()),
            "task_file": "docs/tasks/PC-03.md",
            "base_commit": base,
            "worktree": str(worktree.resolve()),
            "max_iterations": 1,
            "env_file": None,
            "profile": load_project_profile(worktree).public_dict(),
        },
    )
    (run_dir / "iteration").write_text("1\n", encoding="utf-8")
    atomic_write_json(
        run_dir / "reviewed_manifest.json",
        build_snapshot_manifest(worktree, base),
    )
    transition_run(run_dir, RunEvent.RUN_STARTED)
    transition_run(run_dir, RunEvent.REVIEW_STARTED)
    transition_run(run_dir, RunEvent.REVIEW_APPROVED)
    return repo, worktree, run_dir, base


def test_profile_supports_explicit_optional_telegram(tmp_path: Path) -> None:
    profile_path = tmp_path / ".agent-loop" / "project.toml"
    profile_path.parent.mkdir()
    profile_path.write_text(
        'schema_version = 1\n[approval]\nmode = "none"\n',
        encoding="utf-8",
    )
    assert load_project_profile(tmp_path).approval_mode == "none"

    profile_path.write_text(
        'schema_version = 1\n[approval]\nmode = "automatic"\n',
        encoding="utf-8",
    )
    with pytest.raises(ProfileError, match="approval.mode"):
        load_project_profile(tmp_path)


def test_local_technical_approval_is_terminal_and_resumable(tmp_path: Path) -> None:
    _repo, _worktree, run_dir, _base = make_local_approved_run(tmp_path)

    verified = verify_reviewed_snapshot(run_dir)
    plan = plan_resume(run_dir)

    assert verified["matches"] is True
    assert verified["approval_mode"] == "none"
    assert verified["status"] == "APPROVED"
    assert plan["resume_phase"] == "complete"


def test_local_terminal_approval_detects_worktree_drift(tmp_path: Path) -> None:
    _repo, worktree, run_dir, _base = make_local_approved_run(tmp_path)
    (worktree / "app.txt").write_text("drift\n", encoding="utf-8")

    assert verify_reviewed_snapshot(run_dir)["matches"] is False
    with pytest.raises(RunStateError, match="changed after terminal"):
        plan_resume(run_dir)


def test_supervised_phases_disable_git_network_protocols(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_path = tmp_path / ".agent-loop" / "project.toml"
    profile_path.parent.mkdir()
    profile_path.write_text("schema_version = 1\n", encoding="utf-8")
    captured: dict[str, str] = {}

    def fake_supervise_command(**kwargs: object) -> int:
        captured.update(kwargs["environment"])  # type: ignore[arg-type]
        return 0

    monkeypatch.setattr(cli_mod, "supervise_command", fake_supervise_command)
    args = SimpleNamespace(
        repo=str(tmp_path),
        worktree=str(tmp_path),
        run_dir=str(tmp_path / "run"),
        task_file="docs/tasks/PC-03.md",
        base_commit="a" * 40,
        env_file=None,
    )

    assert cli_mod._run_profile_command(
        args,
        [sys.executable, "-c", "pass"],
        phase="executor",
        iteration=1,
    ) == 0
    assert captured["GIT_ALLOW_PROTOCOL"] == "file"
    assert captured["GIT_PROTOCOL_FROM_USER"] == "0"

    blocked = subprocess.run(
        ["git", "ls-remote", "https://example.invalid/repo.git"],
        env={"PATH": captured["PATH"], "GIT_ALLOW_PROTOCOL": captured["GIT_ALLOW_PROTOCOL"]},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert blocked.returncode != 0
    assert "transport 'https' not allowed" in blocked.stderr


def test_real_end_to_end_without_telegram_is_verifiable_and_resumable(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "target"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "pc03-e2e@example.com")
    git(repo, "config", "user.name", "PC-03 E2E")
    profile = repo / ".agent-loop" / "project.toml"
    profile.parent.mkdir()
    profile.write_text(
        textwrap.dedent(
            """\
            schema_version = 1
            [approval]
            mode = "none"
            [executor]
            timeout_seconds = 10
            heartbeat_seconds = 1
            [reviewer]
            timeout_seconds = 10
            heartbeat_seconds = 1
            [validation]
            commands = [["git", "diff", "--check"]]
            [limits]
            memory_bytes = 268435456
            tasks = 64
            [policy]
            terminate_grace_seconds = 1
            """
        ),
        encoding="utf-8",
    )
    task = repo / "docs" / "tasks" / "PC-03.md"
    task.parent.mkdir(parents=True)
    task.write_text("# PC-03\n\nChange app.txt.\n", encoding="utf-8")
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")

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
            if git ls-remote https://example.invalid/repo.git >"$AGENT_LOOP_RUN_DIR/executor-git-network.stdout" 2>"$AGENT_LOOP_RUN_DIR/executor-git-network.stderr"; then
              printf '%s\\n' "remote Git unexpectedly succeeded" >&2
              exit 91
            fi
            grep -q "transport 'https' not allowed" "$AGENT_LOOP_RUN_DIR/executor-git-network.stderr"
            printf '%s\\n' "changed" >> "$AGENT_LOOP_WORKTREE/app.txt"
            printf '%s\\n' '{"summary":"changed app; validation delegated to runner"}'
            """
        ),
        encoding="utf-8",
    )
    cursor.chmod(0o755)
    codex = tmp_path / "fake-codex"
    codex.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            if [[ "${1:-}" == "login" ]]; then
              printf '%s\\n' "Logged in"
              exit 0
            fi
            if git ls-remote ssh://example.invalid/repo.git >"$AGENT_LOOP_RUN_DIR/reviewer-git-network.stdout" 2>"$AGENT_LOOP_RUN_DIR/reviewer-git-network.stderr"; then
              exit 92
            fi
            grep -q "transport 'ssh' not allowed" "$AGENT_LOOP_RUN_DIR/reviewer-git-network.stderr"
            output=""
            while [[ $# -gt 0 ]]; do
              if [[ "$1" == "--output-last-message" ]]; then
                output="$2"
                shift 2
              else
                shift
              fi
            done
            printf '%s\\n' '{"status":"APPROVED","summary":"change and validation are correct","findings":[],"tests_required":[]}' > "$output"
            """
        ),
        encoding="utf-8",
    )
    codex.chmod(0o755)
    state_root = tmp_path / "state"
    environment = {
        **os.environ,
        "CURSOR_AGENT_BIN": str(cursor),
        "CODEX_BIN": str(codex),
        "AGENT_LOOP_PYTHON": sys.executable,
    }
    completed = subprocess.run(
        [
            str(REPO_ROOT / "agent-loop"),
            "run",
            "--repo",
            str(repo),
            "--state-root",
            str(state_root),
            "--require-profile",
            "docs/tasks/PC-03.md",
            "1",
            "main",
        ],
        cwd=REPO_ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    run_dirs = list(state_root.glob("projects/*/runs/*"))
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]
    assert "technical APPROVED finalized locally" in completed.stdout
    assert not (run_dir / "human_approval_request.json").exists()
    assert not (run_dir / "telegram_notify.json").exists()
    assert (run_dir / "reviewed_manifest.json").is_file()
    assert "transport 'https' not allowed" in (
        run_dir / "executor-git-network.stderr"
    ).read_text(encoding="utf-8")
    assert "transport 'ssh' not allowed" in (
        run_dir / "reviewer-git-network.stderr"
    ).read_text(encoding="utf-8")

    verify = subprocess.run(
        [str(REPO_ROOT / "agent-loop"), "verify", "--run-dir", str(run_dir)],
        cwd=REPO_ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert verify.returncode == 0, verify.stdout + verify.stderr
    assert '"approval_mode": "none"' in verify.stdout

    resumed = subprocess.run(
        [str(REPO_ROOT / "agent-loop"), "resume", "--run-dir", str(run_dir)],
        cwd=REPO_ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=10,
    )
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert "snapshot still matches" in resumed.stdout
