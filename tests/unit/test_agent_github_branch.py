"""GitHub branch publication mode without Telegram communication."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Sequence

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS = REPO_ROOT / "scripts" / "agents"
sys.path.insert(0, str(AGENTS))

import dx.github_branch as github_branch_mod  # noqa: E402
from dx.approval import (  # noqa: E402
    ApprovalError,
    enqueue_notification,
    list_pending_notifications,
)
from dx.atomic import atomic_write_json  # noqa: E402
from dx.github_branch import (  # noqa: E402
    BRANCH_PUBLICATION_FILENAME,
    GitHubBranchError,
    LEGACY_PULL_REQUEST_FILENAME,
    publish_reviewed_branch,
)
from dx.integration import IntegrationError, integrate_reviewed_snapshot  # noqa: E402
from dx.profile import ProfileError, load_project_profile  # noqa: E402
from dx.runstate import write_run_metadata  # noqa: E402
from dx.snapshot import build_snapshot_manifest  # noqa: E402
from dx.state_machine import RunEvent, transition_run  # noqa: E402


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args],
        text=True,
        stderr=subprocess.STDOUT,
    ).strip()


def approved_github_run(tmp_path: Path) -> dict[str, object]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "github-branch@example.test")
    git(repo, "config", "user.name", "GitHub Branch Test")
    profile_path = repo / ".agent-loop" / "project.toml"
    profile_path.parent.mkdir()
    profile_path.write_text(
        textwrap.dedent(
            """\
            schema_version = 1
            [approval]
            mode = "github_branch"
            remote = "origin"
            base_branch = "main"
            """
        ),
        encoding="utf-8",
    )
    task = repo / "docs" / "tasks" / "PR-01.md"
    task.parent.mkdir(parents=True)
    task.write_text("# PR-01\n", encoding="utf-8")
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    git(
        repo,
        "remote",
        "add",
        "origin",
        "https://github.com/example/project.git",
    )

    worktree = tmp_path / "worktree"
    git(repo, "worktree", "add", "--detach", str(worktree), base)
    (worktree / "app.txt").write_text("base\nreviewed\n", encoding="utf-8")

    run_dir = tmp_path / "state" / "runs" / "pr-01-20260820T000000Z"
    run_dir.mkdir(parents=True)
    write_run_metadata(
        run_dir,
        {
            "repo": str(repo.resolve()),
            "task_file": "docs/tasks/PR-01.md",
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
        "worktree": worktree,
        "run_dir": run_dir,
        "base": base,
    }


class FakeGitRemote:
    def __init__(self, env: dict[str, object]) -> None:
        self.env = env
        self.pushed = False
        self.commands: list[list[str]] = []

    def _record(self) -> dict[str, object]:
        run_dir = self.env["run_dir"]
        assert isinstance(run_dir, Path)
        current = run_dir / BRANCH_PUBLICATION_FILENAME
        record_path = (
            current if current.exists() else run_dir / LEGACY_PULL_REQUEST_FILENAME
        )
        return json.loads(
            record_path.read_text(encoding="utf-8")
        )

    def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        input_bytes: bytes | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        del cwd, check
        argv = list(command)
        self.commands.append(argv)
        if argv[0] == "git" and "ls-remote" in argv:
            ref = argv[-1]
            if ref == "refs/heads/main":
                output = f"{self.env['base']}\t{ref}\n".encode()
            elif self.pushed:
                output = f"{self._record()['commit']}\t{ref}\n".encode()
            else:
                output = b""
            return subprocess.CompletedProcess(argv, 0, output, b"")
        if argv[0] == "git" and "push" in argv:
            self.pushed = True
            return subprocess.CompletedProcess(argv, 0, b"ok\n", b"")
        raise AssertionError(f"unexpected external command: {argv}")


def test_profile_requires_explicit_safe_github_branch_destination(tmp_path: Path) -> None:
    profile_path = tmp_path / ".agent-loop" / "project.toml"
    profile_path.parent.mkdir()
    profile_path.write_text(
        'schema_version = 1\n[approval]\nmode = "github_branch"\n'
        'remote = "origin"\nbase_branch = "main"\n',
        encoding="utf-8",
    )
    profile = load_project_profile(tmp_path)
    assert profile.approval_mode == "github_branch"
    assert profile.approval_remote == "origin"
    assert profile.approval_base_branch == "main"

    profile_path.write_text(
        'schema_version = 1\n[approval]\nmode = "github_pr"\n'
        'remote = "origin"\nbase_branch = "main"\n',
        encoding="utf-8",
    )
    assert load_project_profile(tmp_path).approval_mode == "github_pr"

    for invalid in (
        'mode = "github_branch"\n',
        'mode = "none"\nremote = "origin"\nbase_branch = "main"\n',
        'mode = "github_branch"\nremote = "../origin"\nbase_branch = "main"\n',
        'mode = "github_branch"\nremote = "origin"\nbase_branch = "../main"\n',
        'mode = "github_branch"\nremote = "origin\\tbad"\nbase_branch = "main"\n',
        'mode = "github_branch"\nremote = "origin"\nbase_branch = "topic.lock"\n',
        'mode = "github_branch"\nremote = "origin"\nbase_branch = "main"\n'
        '[environment]\nrequired = ["AGENT_TELEGRAM_BOT_TOKEN"]\n',
        'mode = "github_branch"\nremote = "origin"\nbase_branch = "main"\n'
        '[environment]\nrequired = ["GH_TOKEN"]\n',
    ):
        profile_path.write_text(
            "schema_version = 1\n[approval]\n" + invalid,
            encoding="utf-8",
        )
        with pytest.raises(ProfileError):
            load_project_profile(tmp_path)


def test_github_mode_pushes_bound_branch_without_gh_or_telegram(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = approved_github_run(tmp_path)
    repo = env["repo"]
    run_dir = env["run_dir"]
    base = env["base"]
    assert isinstance(repo, Path)
    assert isinstance(run_dir, Path)
    assert isinstance(base, str)
    fake = FakeGitRemote(env)
    monkeypatch.setattr(github_branch_mod, "_run_external", fake)
    monkeypatch.setenv("AGENT_TELEGRAM_BOT_TOKEN", "must-not-propagate")
    monkeypatch.setenv("AGENT_TELEGRAM_CREDENTIALS_FILE", "/secret/telegram.env")

    result = publish_reviewed_branch(run_dir)

    assert result["result"] == "branch_published"
    assert result["telegram_operations"] is False
    assert result["remote_operations"] is True
    assert "pull_request" not in result
    assert git(repo, "rev-parse", "HEAD") == base
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "rev-parse", f"refs/heads/{result['head_branch']}") == result["commit"]
    assert not (run_dir / "telegram_notify.json").exists()
    assert all("telegram" not in " ".join(command).lower() for command in fake.commands)
    assert not any(
        key.startswith("AGENT_TELEGRAM_")
        for key in github_branch_mod._external_environment()
    )
    assert all(command[0] == "git" for command in fake.commands)

    replay = publish_reviewed_branch(run_dir)
    assert replay["result"] == "already_published"

    current_record = run_dir / BRANCH_PUBLICATION_FILENAME
    legacy_record = run_dir / LEGACY_PULL_REQUEST_FILENAME
    current_record.rename(legacy_record)
    legacy_payload = json.loads(legacy_record.read_text(encoding="utf-8"))
    legacy_payload["result"] = "branch_pushed"
    atomic_write_json(legacy_record, legacy_payload)
    assert publish_reviewed_branch(run_dir)["result"] == "already_published"
    assert not current_record.exists()

    with pytest.raises(IntegrationError, match="remote branch approval mode"):
        integrate_reviewed_snapshot(run_dir)


def test_github_mode_refuses_base_drift_before_any_push(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = approved_github_run(tmp_path)
    fake = FakeGitRemote(env)

    def drifted(
        command: Sequence[str],
        *,
        cwd: Path,
        input_bytes: bytes | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        argv = list(command)
        if argv[0] == "git" and "ls-remote" in argv and argv[-1] == "refs/heads/main":
            return subprocess.CompletedProcess(
                argv, 0, ("f" * 40 + "\trefs/heads/main\n").encode(), b""
            )
        return fake(command, cwd=cwd, input_bytes=input_bytes, check=check)

    monkeypatch.setattr(github_branch_mod, "_run_external", drifted)
    with pytest.raises(GitHubBranchError, match="base branch"):
        publish_reviewed_branch(env["run_dir"])
    assert fake.pushed is False
    assert not (env["run_dir"] / BRANCH_PUBLICATION_FILENAME).exists()


def test_github_mode_rejects_non_github_or_credentialed_remote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = approved_github_run(tmp_path)
    repo = env["repo"]
    assert isinstance(repo, Path)
    git(
        repo,
        "remote",
        "set-url",
        "origin",
        "https://token@github.com/example/project.git",
    )
    fake = FakeGitRemote(env)
    monkeypatch.setattr(github_branch_mod, "_run_external", fake)

    with pytest.raises(GitHubBranchError, match="uncredentialed github.com"):
        publish_reviewed_branch(env["run_dir"])
    assert fake.commands == []


def test_bridge_ignores_planted_outbox_for_github_mode(tmp_path: Path) -> None:
    env = approved_github_run(tmp_path)
    run_dir = env["run_dir"]
    assert isinstance(run_dir, Path)
    atomic_write_json(
        run_dir / "telegram_notify.json",
        {
            "schema_version": 1,
            "kind": "approved",
            "run_id": run_dir.name,
            "notification_id": "planted",
            "summary": "must never leave host",
            "sent_at": None,
        },
    )

    assert list_pending_notifications(run_dir.parent) == []
    with pytest.raises(ApprovalError, match="forbidden"):
        enqueue_notification(
            run_dir=run_dir,
            kind="approved",
            summary="must never leave host",
        )
