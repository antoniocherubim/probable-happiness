"""DX-02 profiles, supervision, evidence, and safe-resume tests."""

from __future__ import annotations

import json
import os
import signal
import socket
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS = REPO_ROOT / "scripts" / "agents"
sys.path.insert(0, str(AGENTS))

from dx.approval import (  # noqa: E402
    STATUS_APPROVED,
    STATUS_AWAITING,
    STATUS_HUMAN_APPROVED,
    create_approval_request,
    read_status,
    verify_reviewed_snapshot,
    write_status,
)
from dx.bridge import Bridge  # noqa: E402
from dx.config import BridgeConfig  # noqa: E402
from dx.profile import (  # noqa: E402
    ProfileError,
    ProjectProfile,
    build_authorized_environment,
    load_project_profile,
    sanitize_text,
)
from dx.runstate import (  # noqa: E402
    RunStateError,
    attach_evidence,
    plan_resume,
    write_run_metadata,
)
from dx.runtime import TIMEOUT_EXIT, supervise_command  # noqa: E402
from dx.telegram import FakeTelegramAPI, TelegramClient  # noqa: E402


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def make_repo(tmp_path: Path, *, profile: str | None = None) -> tuple[Path, Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "dx@example.com")
    git(repo, "config", "user.name", "DX Test")
    (repo / ".gitignore").write_text(".venv/\n.validation-env\n", encoding="utf-8")
    task = repo / "docs" / "tasks" / "DX-02.md"
    task.parent.mkdir(parents=True)
    task.write_text("# DX-02\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    if profile is not None:
        profile_path = repo / ".agent-loop" / "project.toml"
        profile_path.parent.mkdir(parents=True)
        profile_path.write_text(textwrap.dedent(profile), encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    worktree = tmp_path / "worktree"
    git(repo, "worktree", "add", "--detach", str(worktree), base)
    return repo.resolve(), worktree.resolve(), base


def make_run(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    repo, worktree, base = make_repo(tmp_path)
    (worktree / "tracked.txt").write_text("changed\n", encoding="utf-8")
    run_dir = tmp_path / "state" / "runs" / "dx02-run"
    run_dir.mkdir(parents=True)
    write_run_metadata(
        run_dir,
        {
            "repo": str(repo),
            "task_file": "docs/tasks/DX-02.md",
            "base_commit": base,
            "worktree": str(worktree),
            "max_iterations": 3,
            "env_file": None,
            "profile": ProjectProfile().public_dict(),
        },
    )
    (run_dir / "iteration").write_text("1\n", encoding="utf-8")
    return repo, worktree, run_dir, base


def test_project_profile_parses_complete_schema(tmp_path: Path) -> None:
    repo, _worktree, _base = make_repo(
        tmp_path,
        profile="""
        schema_version = 1
        [bootstrap]
        command = ["bash", "scripts/agent-loop/bootstrap.sh"]
        timeout_seconds = 12
        [executor]
        timeout_seconds = 20
        heartbeat_seconds = 2
        [reviewer]
        timeout_seconds = 30
        heartbeat_seconds = 3
        [environment]
        required = ["TEST_DATABASE_URL", "POSTGRES_ADMIN_DATABASE_URL"]
        [validation]
        commands = [["python", "-m", "compileall", "-q", "app"], ["git", "diff", "--check"]]
        [instructions]
        executor = [".agent-loop/executor-extra.md"]
        reviewer = [".agent-loop/reviewer-extra.md"]
        [policy]
        missing_profile = "deny"
        terminate_grace_seconds = 4
        """,
    )
    profile = load_project_profile(repo)
    assert profile.bootstrap_command == ("bash", "scripts/agent-loop/bootstrap.sh")
    assert profile.executor_timeout_seconds == 20
    assert profile.reviewer_heartbeat_seconds == 3
    assert profile.required_environment == ("TEST_DATABASE_URL", "POSTGRES_ADMIN_DATABASE_URL")
    assert profile.validation_commands[-1] == ("git", "diff", "--check")
    assert profile.missing_profile == "deny"


@pytest.mark.parametrize(
    "profile",
    [
        "schema_version = 2\n",
        "schema_version = 1\nunknown = true\n",
        "schema_version = 1\n[executor]\ntimeout_seconds = 0\n",
        'schema_version = 1\n[environment]\nrequired = ["BAD-NAME"]\n',
        'schema_version = 1\n[instructions]\nexecutor = ["../secret"]\n',
        'schema_version = 1\n[bootstrap]\ncommand = "bash unsafe"\n',
    ],
)
def test_project_profile_rejects_unknown_or_unsafe_configuration(
    tmp_path: Path, profile: str
) -> None:
    repo, _worktree, _base = make_repo(tmp_path, profile=profile)
    with pytest.raises(ProfileError):
        load_project_profile(repo)


def test_missing_profile_uses_safe_defaults_or_can_be_denied(tmp_path: Path) -> None:
    repo, _worktree, _base = make_repo(tmp_path)
    assert load_project_profile(repo).executor_timeout_seconds == 1800
    with pytest.raises(ProfileError, match="required"):
        load_project_profile(repo, missing_policy="deny")


def test_environment_file_is_allowlisted_and_values_are_sanitized(tmp_path: Path) -> None:
    env_file = tmp_path / "test.env"
    secret_url = "postgresql://admin:password@db.example/test?sslmode=require"
    env_file.write_text(
        f"TEST_DATABASE_URL={secret_url}\nUNLISTED_SECRET=do-not-propagate\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    profile = ProjectProfile(required_environment=("TEST_DATABASE_URL",))
    child, diagnostics = build_authorized_environment(
        profile,
        env_file,
        environ={"PATH": "/bin", "PARENT_SECRET": "hidden"},
    )
    assert child["TEST_DATABASE_URL"] == secret_url
    assert "UNLISTED_SECRET" not in child
    assert "PARENT_SECRET" not in child
    assert diagnostics == {"TEST_DATABASE_URL": "set"}
    sanitized = sanitize_text(f"db={secret_url} other=https://u:p@example.test/x?q=secret", child)
    assert "password" not in sanitized and "secret" not in sanitized
    env_file.chmod(0o644)
    with pytest.raises(ProfileError, match="0600"):
        build_authorized_environment(profile, env_file)


def test_executor_and_validation_receive_only_allowlisted_project_environment(tmp_path: Path) -> None:
    profile = """
    schema_version = 1
    [executor]
    timeout_seconds = 5
    heartbeat_seconds = 1
    [environment]
    required = ["TEST_DATABASE_URL"]
    [validation]
    commands = [["python3", "scripts/agent-loop/check-env.py", ".validation-env"]]
    """
    repo, worktree, base = make_repo(tmp_path, profile=profile)
    checker = repo / "scripts" / "agent-loop" / "check-env.py"
    checker.parent.mkdir(parents=True)
    checker.write_text(
        "import json,os,sys\n"
        "open(sys.argv[1], 'w').write(json.dumps({"
        "'allowed': bool(os.getenv('TEST_DATABASE_URL')), "
        "'extra': bool(os.getenv('UNLISTED_SECRET'))}))\n",
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "checker")
    # Recreate the worktree at the commit that includes the helper.
    git(repo, "worktree", "remove", "--force", str(worktree))
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "worktree", "add", "--detach", str(worktree), base)
    env_file = tmp_path / "test.env"
    env_file.write_text("TEST_DATABASE_URL=postgresql://secret/db\nUNLISTED_SECRET=hidden\n", encoding="utf-8")
    env_file.chmod(0o600)
    common = [
        "--repo", str(repo), "--worktree", str(worktree), "--run-dir", str(tmp_path / "run"),
        "--task-file", "docs/tasks/DX-02.md", "--base-commit", base, "--env-file", str(env_file),
    ]
    validation = subprocess.run(
        ["python3", str(AGENTS / "telegram_bridge.py"), "run-validations", *common],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "UNLISTED_SECRET": "parent-hidden"},
    )
    assert validation.returncode == 0, validation.stdout + validation.stderr
    assert json.loads((worktree / ".validation-env").read_text()) == {"allowed": True, "extra": False}
    assert "postgresql://secret/db" not in validation.stdout + validation.stderr

    (worktree / ".validation-env").unlink()
    executor = subprocess.run(
        [
            "python3", str(AGENTS / "telegram_bridge.py"), "supervise", *common,
            "--phase", "executor", "--iteration", "1", "--",
            "python3", "scripts/agent-loop/check-env.py", ".validation-env",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "UNLISTED_SECRET": "parent-hidden"},
    )
    assert executor.returncode == 0, executor.stdout + executor.stderr
    assert json.loads((worktree / ".validation-env").read_text()) == {"allowed": True, "extra": False}


def test_bootstrap_allows_ignored_files_and_rejects_tracked_changes(tmp_path: Path) -> None:
    profile = """
    schema_version = 1
    [bootstrap]
    command = ["bash", "-c", "mkdir -p .venv && printf ok > .venv/ready"]
    timeout_seconds = 5
    """
    repo, worktree, base = make_repo(tmp_path, profile=profile)
    run_dir = tmp_path / "run"
    command = [
        "python3", str(AGENTS / "telegram_bridge.py"), "run-bootstrap",
        "--repo", str(repo), "--worktree", str(worktree), "--run-dir", str(run_dir),
        "--task-file", "docs/tasks/DX-02.md", "--base-commit", base,
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (worktree / ".venv" / "ready").read_text(encoding="utf-8") == "ok"

    mutating_root = tmp_path / "mutating"
    mutating_root.mkdir()
    repo2, worktree2, base2 = make_repo(
        mutating_root,
        profile='''
        schema_version = 1
        [bootstrap]
        command = ["bash", "-c", "printf changed >> tracked.txt"]
        timeout_seconds = 5
        ''',
    )
    command2 = [
        "python3", str(AGENTS / "telegram_bridge.py"), "run-bootstrap",
        "--repo", str(repo2), "--worktree", str(worktree2), "--run-dir", str(tmp_path / "run2"),
        "--task-file", "docs/tasks/DX-02.md", "--base-commit", base2,
    ]
    completed = subprocess.run(command2, check=False, capture_output=True, text=True)
    assert completed.returncode == 125
    assert "modified tracked" in completed.stderr


def test_supervisor_times_out_entire_process_group_and_keeps_empty_report(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    subprocess.run(["git", "init", str(worktree)], check=True, capture_output=True)
    child_pid = tmp_path / "child.pid"
    report = tmp_path / "run" / "cursor-1.json"
    script = (
        "import pathlib,subprocess,time; "
        "p=subprocess.Popen(['sleep','60']); "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(p.pid)); "
        "time.sleep(60)"
    )
    result = supervise_command(
        command=[sys.executable, "-c", script],
        phase="executor",
        iteration=1,
        cwd=worktree,
        run_dir=report.parent,
        environment={"PATH": os.environ["PATH"]},
        secret_values={},
        timeout_seconds=1,
        heartbeat_seconds=1,
        terminate_grace_seconds=1,
        report_path=report,
    )
    assert result == TIMEOUT_EXIT
    assert report.stat().st_size == 0
    phase_result = json.loads((report.parent / "executor-1-result.json").read_text())
    assert phase_result["reason"] == "executor_timeout"
    pid = int(child_pid.read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and Path(f"/proc/{pid}").exists():
        time.sleep(0.05)
    assert not Path(f"/proc/{pid}").exists()


def test_supervisor_emits_periodic_safe_heartbeat(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    subprocess.run(["git", "init", str(worktree)], check=True, capture_output=True)
    run_dir = tmp_path / "run"
    result = supervise_command(
        command=[sys.executable, "-c", "import time; time.sleep(1.3); print('done')"],
        phase="reviewer",
        iteration=2,
        cwd=worktree,
        run_dir=run_dir,
        environment={"PATH": os.environ["PATH"]},
        secret_values={},
        timeout_seconds=5,
        heartbeat_seconds=1,
        terminate_grace_seconds=1,
    )
    assert result == 0
    output = capsys.readouterr().out
    assert output.count("Reviewer iteration=2 active") >= 2
    assert "pid=" in output and "changed_files=" in output and "last_activity=" in output
    heartbeat = json.loads((run_dir / "heartbeat.json").read_text())
    assert heartbeat["state"] == "completed"


def test_reviewer_timeout_is_structured_and_incomplete_json_is_rejected(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    subprocess.run(["git", "init", str(worktree)], check=True, capture_output=True)
    run_dir = tmp_path / "run"
    result = supervise_command(
        command=[sys.executable, "-c", "import time; time.sleep(10)"],
        phase="reviewer",
        iteration=1,
        cwd=worktree,
        run_dir=run_dir,
        environment={"PATH": os.environ["PATH"]},
        secret_values={},
        timeout_seconds=1,
        heartbeat_seconds=1,
        terminate_grace_seconds=1,
    )
    assert result == TIMEOUT_EXIT
    payload = json.loads((run_dir / "reviewer-1-result.json").read_text())
    assert payload["reason"] == "reviewer_timeout"
    incomplete = run_dir / "review.json"
    incomplete.write_text('{"status":"APPROVED"}\n', encoding="utf-8")
    checked = subprocess.run(
        ["python3", str(AGENTS / "telegram_bridge.py"), "review-status", "--file", str(incomplete)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert checked.returncode == 1


def test_resume_plans_executor_reviewer_changes_and_rejects_review_drift(tmp_path: Path) -> None:
    _repo, worktree, run_dir, base = make_run(tmp_path)
    write_status(run_dir, "EXECUTING")
    (run_dir / "cursor-1.json").write_bytes(b"")
    assert plan_resume(run_dir)["resume_phase"] == "executor"

    (run_dir / "cursor-1.json").write_text('{"summary":"ok"}\n', encoding="utf-8")
    write_status(run_dir, "REVIEWING")
    from dx.approval import compute_diff_hash

    snapshot = compute_diff_hash(worktree, base)
    (run_dir / "review-1-snapshot.json").write_text(
        json.dumps({"schema_version": 1, "iteration": 1, "diff_hash": snapshot}),
        encoding="utf-8",
    )
    (run_dir / "review-1.json").write_bytes(b"")
    assert plan_resume(run_dir)["resume_phase"] == "reviewer"

    write_status(run_dir, "CHANGES_REQUESTED")
    assert plan_resume(run_dir)["iteration"] == 2
    assert plan_resume(run_dir)["resume_phase"] == "executor"

    write_status(run_dir, "REVIEWING")
    (worktree / "tracked.txt").write_text("tampered after snapshot\n", encoding="utf-8")
    with pytest.raises(RunStateError, match="changed"):
        plan_resume(run_dir)


def test_resume_waiting_human_reuses_gate_and_rejects_mutation(tmp_path: Path) -> None:
    _repo, worktree, run_dir, base = make_run(tmp_path)
    write_status(run_dir, STATUS_APPROVED)
    create_approval_request(
        run_dir=run_dir,
        task="docs/tasks/DX-02.md",
        base_commit=base,
        worktree=worktree,
        review_report="review-1.json",
    )
    assert read_status(run_dir) == STATUS_AWAITING
    assert plan_resume(run_dir)["resume_phase"] == "awaiting_human"
    (worktree / "tracked.txt").write_text("drift\n", encoding="utf-8")
    with pytest.raises(RunStateError, match="changed"):
        plan_resume(run_dir)


def test_evidence_accepts_regular_file_and_rejects_unsafe_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo, _worktree, run_dir, _base = make_run(tmp_path)
    evidence = tmp_path / "report.txt"
    evidence.write_text("external report\n", encoding="utf-8")
    accepted = attach_evidence(run_dir, evidence)
    copied = run_dir / "evidence" / accepted["name"]
    assert copied.read_text(encoding="utf-8") == "external report\n"
    manifest = json.loads((run_dir / "evidence.json").read_text())
    assert manifest["items"][0]["trust"] == "untrusted"
    assert read_status(run_dir) == ""

    symlink = tmp_path / "linked"
    symlink.symlink_to(evidence)
    with pytest.raises(RunStateError, match="regular non-symlink"):
        attach_evidence(run_dir, symlink)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(RunStateError, match="regular non-symlink"):
        attach_evidence(run_dir, fifo)
    sock_path = tmp_path / "socket"
    original_lstat = Path.lstat

    def socket_lstat(path: Path) -> os.stat_result:
        if path == sock_path:
            return os.stat_result((stat.S_IFSOCK | 0o600, 0, 0, 1, os.getuid(), os.getgid(), 0, 0, 0, 0))
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", socket_lstat)
    with pytest.raises(RunStateError, match="regular non-symlink"):
        attach_evidence(run_dir, sock_path)
    large = tmp_path / "large"
    large.write_bytes(b"x" * 1025)
    with pytest.raises(RunStateError, match="exceeds"):
        attach_evidence(run_dir, large, max_bytes=1024)


def test_resumed_review_opens_telegram_gate_and_approval_verifies(tmp_path: Path) -> None:
    _repo, worktree, run_dir, _base = make_run(tmp_path)
    write_status(run_dir, "BLOCKED")
    evidence = tmp_path / "standalone-review.txt"
    evidence.write_text("untrusted supporting evidence\n", encoding="utf-8")
    attach_evidence(run_dir, evidence)

    cursor = tmp_path / "agent"
    cursor.write_text(
        "#!/usr/bin/env bash\n[[ \"${1:-}\" == status ]] && { echo authenticated; exit 0; }\nexit 99\n",
        encoding="utf-8",
    )
    cursor.chmod(0o755)
    codex = tmp_path / "codex"
    codex.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            if [[ "${1:-}" == login ]]; then echo 'Logged in'; exit 0; fi
            output=""
            while [[ $# -gt 0 ]]; do
              case "$1" in
                --output-last-message) output="$2"; shift 2 ;;
                *) shift ;;
              esac
            done
            printf '%s\\n' '{"status":"APPROVED","summary":"fresh review","findings":[],"tests_required":[]}' > "$output"
            """
        ),
        encoding="utf-8",
    )
    codex.chmod(0o755)

    completed = subprocess.run(
        [str(REPO_ROOT / "agent-loop"), "resume", "--run-dir", str(run_dir), "--review-only"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "CURSOR_AGENT_BIN": str(cursor),
            "CODEX_BIN": str(codex),
            "AGENT_HUMAN_APPROVAL_TIMEOUT_SEC": "1",
        },
        timeout=15,
    )
    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert read_status(run_dir) == STATUS_AWAITING
    assert (run_dir / "human_approval_request.json").is_file()
    notify = json.loads((run_dir / "telegram_notify.json").read_text())
    assert notify["offer_approval_button"] is True

    token = "123456:DX02-FAKE"
    fake = FakeTelegramAPI(allowed_token=token)
    config = BridgeConfig(bot_token=token, allowed_user_id=42, allowed_chat_id=42, poll_timeout_sec=1)
    bridge = Bridge(
        config,
        TelegramClient(token, api_base="http://telegram.test", transport=fake.as_transport()),
        run_dir.parent,
    )
    assert bridge.process_outbox_once() == 1
    callback_token = notify["callback_token"]
    fake.push_callback(user_id=42, chat_id=42, data=callback_token, callback_query_id="dx02")
    assert bridge.process_updates_once() == 1
    assert read_status(run_dir) == STATUS_HUMAN_APPROVED
    assert verify_reviewed_snapshot(run_dir)["matches"] is True


def test_systemd_unit_creates_missing_state_root(tmp_path: Path) -> None:
    state_root = tmp_path / "missing state"
    unit = tmp_path / "bridge.service"
    completed = subprocess.run(
        [
            str(REPO_ROOT / "agent-loop"), "systemd-unit",
            "--state-root", str(state_root),
            "--credentials-file", str(tmp_path / "telegram.env"),
            "--output", str(unit),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    text = unit.read_text(encoding="utf-8")
    assert f'ExecStartPre=+/usr/bin/mkdir -p "{state_root}"' in text
