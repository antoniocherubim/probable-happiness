"""Safe, local-only integration of reviewed snapshots."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS = REPO_ROOT / "scripts" / "agents"
sys.path.insert(0, str(AGENTS))

from dx.approval import verify_reviewed_snapshot  # noqa: E402
from dx.atomic import atomic_write_json  # noqa: E402
from dx.integration import (  # noqa: E402
    INTEGRATION_FILENAME,
    IntegrationError,
    integrate_reviewed_snapshot,
)
from dx.profile import load_project_profile  # noqa: E402
from dx.runstate import write_run_metadata  # noqa: E402
from dx.snapshot import build_snapshot_manifest  # noqa: E402
from dx.state_machine import RunEvent, transition_run  # noqa: E402


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args],
        text=True,
        stderr=subprocess.STDOUT,
    ).strip()


def approved_run(tmp_path: Path) -> dict[str, object]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "integration@example.test")
    git(repo, "config", "user.name", "Integration Test")

    profile = repo / ".agent-loop" / "project.toml"
    profile.parent.mkdir()
    profile.write_text(
        'schema_version = 1\n[approval]\nmode = "none"\n',
        encoding="utf-8",
    )
    task = repo / "docs" / "tasks" / "IT-01.md"
    task.parent.mkdir(parents=True)
    task.write_text("# IT-01\n", encoding="utf-8")
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    (repo / "delete.txt").write_text("remove me\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")

    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--quiet", "--bare", str(repo), str(remote)],
        check=True,
    )
    git(repo, "remote", "add", "origin", str(remote))

    worktree = tmp_path / "worktree"
    git(repo, "worktree", "add", "--detach", str(worktree), base)
    (worktree / "app.txt").write_text("base\napproved\n", encoding="utf-8")
    (worktree / "delete.txt").unlink()
    executable = worktree / "run.sh"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    (worktree / "app-link").symlink_to("app.txt")

    run_dir = tmp_path / "state" / "runs" / "it-01-run"
    run_dir.mkdir(parents=True)
    write_run_metadata(
        run_dir,
        {
            "repo": str(repo.resolve()),
            "task_file": "docs/tasks/IT-01.md",
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
    return {
        "repo": repo,
        "remote": remote,
        "worktree": worktree,
        "run_dir": run_dir,
        "base": base,
    }


def test_cli_integrates_exact_snapshot_without_remote_or_hooks(
    tmp_path: Path,
) -> None:
    env = approved_run(tmp_path)
    repo = env["repo"]
    remote = env["remote"]
    worktree = env["worktree"]
    run_dir = env["run_dir"]
    base = env["base"]
    assert isinstance(repo, Path)
    assert isinstance(remote, Path)
    assert isinstance(worktree, Path)
    assert isinstance(run_dir, Path)
    assert isinstance(base, str)

    hook_marker = tmp_path / "post-merge-ran"
    hook = repo / ".git" / "hooks" / "post-merge"
    hook.write_text(
        f"#!/bin/sh\nprintf ran > {str(hook_marker)!r}\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)

    completed = subprocess.run(
        [
            str(REPO_ROOT / "agent-loop"),
            "integrate",
            "--run-dir",
            str(run_dir),
            "--message",
            "IT-01: approved local integration",
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "AGENT_LOOP_PYTHON": sys.executable},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    commit = result["commit"]

    assert result["result"] == "integrated"
    assert result["remote_operations"] is False
    assert git(repo, "rev-parse", "HEAD") == commit
    assert git(repo, "rev-parse", f"{commit}^") == base
    assert git(repo, "status", "--porcelain") == ""
    assert (repo / "app.txt").read_text(encoding="utf-8") == "base\napproved\n"
    assert not (repo / "delete.txt").exists()
    assert os.access(repo / "run.sh", os.X_OK)
    assert (repo / "app-link").is_symlink()
    assert os.readlink(repo / "app-link") == "app.txt"
    assert git(remote, "rev-parse", "refs/heads/main") == base
    assert not hook_marker.exists()

    # The forensic worktree remains detached at base and still verifies.
    assert git(worktree, "rev-parse", "HEAD") == base
    assert verify_reviewed_snapshot(run_dir)["matches"] is True

    replay = integrate_reviewed_snapshot(run_dir)
    assert replay["result"] == "already_integrated"
    assert replay["commit"] == commit
    assert json.loads(
        (run_dir / INTEGRATION_FILENAME).read_text(encoding="utf-8")
    )["commit"] == commit


def test_integration_rejects_dirty_target_checkout(tmp_path: Path) -> None:
    env = approved_run(tmp_path)
    repo = env["repo"]
    assert isinstance(repo, Path)
    (repo / "unreviewed.txt").write_text("local\n", encoding="utf-8")

    with pytest.raises(IntegrationError, match="not clean"):
        integrate_reviewed_snapshot(env["run_dir"])
    assert git(repo, "rev-parse", "HEAD") == env["base"]


def test_integration_rejects_target_branch_diverged_from_base(
    tmp_path: Path,
) -> None:
    env = approved_run(tmp_path)
    repo = env["repo"]
    assert isinstance(repo, Path)
    (repo / "local.txt").write_text("local\n", encoding="utf-8")
    git(repo, "add", "local.txt")
    git(repo, "commit", "-m", "local divergence")
    divergent = git(repo, "rev-parse", "HEAD")

    with pytest.raises(IntegrationError, match="approved base"):
        integrate_reviewed_snapshot(env["run_dir"])
    assert git(repo, "rev-parse", "HEAD") == divergent


def test_integration_rejects_reviewed_worktree_drift(tmp_path: Path) -> None:
    env = approved_run(tmp_path)
    worktree = env["worktree"]
    repo = env["repo"]
    assert isinstance(worktree, Path)
    assert isinstance(repo, Path)
    (worktree / "app.txt").write_text("drift\n", encoding="utf-8")

    with pytest.raises(IntegrationError, match="no longer matches"):
        integrate_reviewed_snapshot(env["run_dir"])
    assert git(repo, "rev-parse", "HEAD") == env["base"]


def test_integration_rejects_tampered_manifest(tmp_path: Path) -> None:
    env = approved_run(tmp_path)
    run_dir = env["run_dir"]
    repo = env["repo"]
    assert isinstance(run_dir, Path)
    assert isinstance(repo, Path)
    manifest_path = run_dir / "reviewed_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entries"][0]["path"] = "../escape"
    atomic_write_json(manifest_path, manifest)

    with pytest.raises(
        IntegrationError,
        match="manifest does not match",
    ):
        integrate_reviewed_snapshot(run_dir)
    assert git(repo, "rev-parse", "HEAD") == env["base"]


def test_integration_rejects_unsafe_commit_message(tmp_path: Path) -> None:
    env = approved_run(tmp_path)
    repo = env["repo"]
    assert isinstance(repo, Path)

    with pytest.raises(IntegrationError, match="one non-empty line"):
        integrate_reviewed_snapshot(
            env["run_dir"],
            message="approved\npush origin main",
        )
    assert git(repo, "rev-parse", "HEAD") == env["base"]


def test_integration_rejects_checkout_filter_without_executing_it(
    tmp_path: Path,
) -> None:
    env = approved_run(tmp_path)
    repo = env["repo"]
    worktree = env["worktree"]
    run_dir = env["run_dir"]
    assert isinstance(repo, Path)
    assert isinstance(worktree, Path)
    assert isinstance(run_dir, Path)

    marker = tmp_path / "filter-ran"
    git(
        repo,
        "config",
        "filter.danger.smudge",
        f"sh -c 'printf ran > {marker}'",
    )
    (worktree / ".gitattributes").write_text(
        "app.txt filter=danger\n",
        encoding="utf-8",
    )
    atomic_write_json(
        run_dir / "reviewed_manifest.json",
        build_snapshot_manifest(worktree, str(env["base"])),
    )

    with pytest.raises(IntegrationError, match="checkout filter"):
        integrate_reviewed_snapshot(run_dir)
    assert not marker.exists()
    assert git(repo, "rev-parse", "HEAD") == env["base"]


def test_integration_checks_whitespace_in_reviewed_untracked_files(
    tmp_path: Path,
) -> None:
    env = approved_run(tmp_path)
    repo = env["repo"]
    worktree = env["worktree"]
    run_dir = env["run_dir"]
    assert isinstance(repo, Path)
    assert isinstance(worktree, Path)
    assert isinstance(run_dir, Path)

    (worktree / "bad-whitespace.txt").write_text(
        "content\n\n",
        encoding="utf-8",
    )
    atomic_write_json(
        run_dir / "reviewed_manifest.json",
        build_snapshot_manifest(worktree, str(env["base"])),
    )

    with pytest.raises(IntegrationError, match="local Git command failed: diff"):
        integrate_reviewed_snapshot(run_dir)
    assert git(repo, "rev-parse", "HEAD") == env["base"]
