"""YOLO approval mode: unbounded task loops, local commits, and final review."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS = REPO_ROOT / "scripts" / "agents"
sys.path.insert(0, str(AGENTS))

from dx.profile import load_project_profile  # noqa: E402
from dx.runstate import UNBOUNDED_ITERATIONS, validate_run  # noqa: E402


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args],
        text=True,
        stderr=subprocess.STDOUT,
    ).strip()


def executable(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content).lstrip("\\\n"), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_profile_accepts_yolo_without_remote_configuration(tmp_path: Path) -> None:
    profile = tmp_path / ".agent-loop" / "project.toml"
    profile.parent.mkdir()
    profile.write_text(
        'schema_version = 1\n[approval]\nmode = "yolo"\n',
        encoding="utf-8",
    )

    parsed = load_project_profile(tmp_path)

    assert parsed.approval_mode == "yolo"
    assert parsed.approval_remote is None
    assert parsed.approval_base_branch is None


def test_token_exhaustion_detector_is_terminal_and_specific(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "executor-7.log").write_text(
        "Request failed: context window limit exceeded\n",
        encoding="utf-8",
    )
    harness = textwrap.dedent(
        f"""\
        set -euo pipefail
        RUN_DIR={str(run_dir)!r}
        source {str(AGENTS / 'run_task.sh')!r}
        agent_tokens_exhausted executor 7
        """
    )

    completed = subprocess.run(
        ["bash", "-c", harness],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_yolo_runs_ordered_tasks_as_local_commits_then_reviews_once(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "target"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "yolo@example.com")
    git(repo, "config", "user.name", "YOLO Test")
    profile = repo / ".agent-loop" / "project.toml"
    profile.parent.mkdir()
    profile.write_text(
        textwrap.dedent(
            """\
            schema_version = 1
            [approval]
            mode = "yolo"
            [executor]
            timeout_seconds = 20
            heartbeat_seconds = 1
            [reviewer]
            timeout_seconds = 20
            heartbeat_seconds = 1
            [validation]
            commands = [["git", "diff", "--check"]]
            [limits]
            memory_bytes = 268435456
            tasks = 64
            [policy]
            missing_profile = "deny"
            terminate_grace_seconds = 1
            """
        ),
        encoding="utf-8",
    )
    tasks = []
    for task_id in ("YOLO-A", "YOLO-B"):
        task = repo / "docs" / "tasks" / f"{task_id}.md"
        task.parent.mkdir(parents=True, exist_ok=True)
        task.write_text(f"# {task_id}\n\nUpdate app.txt.\n", encoding="utf-8")
        tasks.append(task.relative_to(repo).as_posix())
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "prepare ordered tasks")
    start = git(repo, "rev-parse", "HEAD")

    cursor = tmp_path / "fake-cursor"
    executable(
        cursor,
        r"""
        #!/usr/bin/env bash
        set -euo pipefail
        if [[ "${1:-}" == "status" ]]; then
          printf '%s\n' "Logged in"
          exit 0
        fi
        iteration="$(<"$AGENT_LOOP_RUN_DIR/iteration")"
        printf '%s iteration=%s\n' "$AGENT_LOOP_TASK_FILE" "$iteration" >> "$AGENT_LOOP_WORKTREE/app.txt"
        printf '%s\n' '{"summary":"implemented task locally"}'
        """,
    )
    codex = tmp_path / "fake-codex"
    executable(
        codex,
        r"""
        #!/usr/bin/env bash
        set -euo pipefail
        if [[ "${1:-}" == "login" ]]; then
          printf '%s\n' "Logged in"
          exit 0
        fi
        if [[ "${1:-}" == "--version" ]]; then
          printf '%s\n' "codex-cli test"
          exit 0
        fi
        output=""
        structured=0
        while [[ $# -gt 0 ]]; do
          case "$1" in
            --output-last-message) output="$2"; shift 2 ;;
            --output-schema) structured=1; shift 2 ;;
            *) shift ;;
          esac
        done
        [[ -n "$output" ]]
        if [[ "$structured" -eq 0 ]]; then
          printf '%s\n' '# Final technical review' '' 'No findings.' > "$output"
        elif [[ "$AGENT_LOOP_TASK_FILE" == *YOLO-A.md ]] && [[ "$(<"$AGENT_LOOP_RUN_DIR/iteration")" == 1 ]]; then
          printf '%s\n' '{"status":"CHANGES_REQUESTED","summary":"one correction","findings":[{"severity":"medium","title":"iterate","details":"run once more","files":["app.txt"]}],"tests_required":[]}' > "$output"
        else
          printf '%s\n' '{"status":"APPROVED","summary":"approved","findings":[],"tests_required":[]}' > "$output"
        fi
        """,
    )
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
            "yolo",
            "--repo",
            str(repo),
            "--state-root",
            str(state_root),
            *tasks,
        ],
        cwd=REPO_ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=90,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "iteration 2/unlimited: Cursor executing" in completed.stdout
    assert "local commits=2 push=none branch_created=none" in completed.stdout
    assert git(repo, "rev-list", "--count", f"{start}..HEAD") == "2"
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "branch", "--format=%(refname:short)").splitlines() == ["main"]
    assert git(repo, "log", "-2", "--format=%s").splitlines() == [
        "YOLO-B: integrate approved snapshot",
        "YOLO-A: integrate approved snapshot",
    ]
    assert (repo / "app.txt").read_text(encoding="utf-8").splitlines() == [
        "base",
        "docs/tasks/YOLO-A.md iteration=1",
        "docs/tasks/YOLO-A.md iteration=2",
        "docs/tasks/YOLO-B.md iteration=1",
    ]
    chain_dirs = list(state_root.glob("projects/*/chains/yolo-*"))
    assert len(chain_dirs) == 1
    chain = chain_dirs[0]
    assert (chain / "status").read_text(encoding="utf-8").strip() == "COMPLETE"
    assert (chain / "final-review.md").read_text(encoding="utf-8") == (
        "# Final technical review\n\nNo findings.\n"
    )
    runs = [Path(line) for line in (chain / "runs").read_text(encoding="utf-8").splitlines()]
    assert len(runs) == 2
    for run in runs:
        metadata = validate_run(run)
        assert metadata["max_iterations"] == UNBOUNDED_ITERATIONS
        assert (run / "integration.json").is_file()
