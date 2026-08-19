"""SELF-00P frozen control adapter vs authorized candidate profile."""

from __future__ import annotations

import json
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

from dx.approval import verify_reviewed_snapshot  # noqa: E402
from dx.atomic import atomic_write_json  # noqa: E402
from dx.control_adapter import (  # noqa: E402
    CANDIDATE_PROFILE_FLAG,
    MISSING_BASE_ENTRYPOINT,
    MISSING_FROZEN_ENTRYPOINT,
    UNAUTHORIZED_PROFILE_MESSAGE,
    ControlAdapterError,
    UnauthorizedProfileChange,
    accept_candidate_profile,
    capture_control_adapter,
    command_entrypoint,
    load_control_profile,
    load_frozen_instruction_text,
    materialize_control_adapter,
    rewrite_frozen_entrypoint,
)
from dx.integration import IntegrationError, integrate_reviewed_snapshot  # noqa: E402
from dx.profile import load_instruction_text, load_project_profile  # noqa: E402
from dx.runstate import RunStateError, load_run_metadata, plan_resume  # noqa: E402
from dx.snapshot import build_snapshot_manifest  # noqa: E402
from dx.state_machine import RunEvent, transition_run  # noqa: E402


DX = ["python3", str(AGENTS / "telegram_bridge.py")]


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args],
        text=True,
        stderr=subprocess.STDOUT,
    ).strip()


def make_repo(
    tmp_path: Path,
    *,
    profile: str,
    extra_files: dict[str, str] | None = None,
    mode: dict[str, int] | None = None,
) -> tuple[Path, Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "self00p@example.test")
    git(repo, "config", "user.name", "SELF-00P")
    (repo / ".agent-loop").mkdir()
    (repo / ".agent-loop" / "project.toml").write_text(
        textwrap.dedent(profile), encoding="utf-8"
    )
    task = repo / "docs" / "tasks" / "SELF-00P.md"
    task.parent.mkdir(parents=True)
    task.write_text("# SELF-00P\n\nChange app.txt.\n", encoding="utf-8")
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    for relative, content in (extra_files or {}).items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if mode and relative in mode:
            path.chmod(mode[relative])
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    worktree = tmp_path / "worktree"
    git(repo, "worktree", "add", "--detach", str(worktree), base)
    return repo.resolve(), worktree.resolve(), base


def init_run(
    tmp_path: Path,
    repo: Path,
    worktree: Path,
    base: str,
    *,
    allow: bool = False,
    task_file: str = "docs/tasks/SELF-00P.md",
) -> Path:
    run_dir = tmp_path / "state" / "runs" / "self-00p-run"
    run_dir.mkdir(parents=True)
    command = [
        *DX,
        "init-run",
        "--run-dir",
        str(run_dir),
        "--repo",
        str(repo),
        "--worktree",
        str(worktree),
        "--task-file",
        task_file,
        "--base-commit",
        base,
        "--max-iterations",
        "1",
    ]
    if allow:
        command.append("--allow-candidate-profile")
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    (run_dir / "iteration").write_text("1\n", encoding="utf-8")
    return run_dir


MINIMAL_PROFILE = """
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


def test_command_entrypoint_identifies_gate_scripts_not_test_objects() -> None:
    assert command_entrypoint(("bash", "scripts/agent-loop/test.sh")) == "scripts/agent-loop/test.sh"
    assert command_entrypoint(("bash", "-e", "scripts/gate.sh")) == "scripts/gate.sh"
    assert command_entrypoint(("/bin/bash", "-e", "scripts/gate.sh")) == "scripts/gate.sh"
    assert command_entrypoint(("sh", "-e", "./scripts/gate.sh")) == "scripts/gate.sh"
    assert command_entrypoint(("bash", "--norc", "-e", "scripts/gate.sh")) == "scripts/gate.sh"
    assert (
        command_entrypoint(("bash", "-euo", "pipefail", "scripts/gate.sh"))
        == "scripts/gate.sh"
    )
    assert command_entrypoint(("python3", "-B", "scripts/gate.py")) == "scripts/gate.py"
    assert command_entrypoint(("python3", "-m", "compileall", "-q", "scripts/agents/dx")) is None
    assert command_entrypoint(("python3", "-m", "pytest", "tests/unit/test_foo.py")) is None
    assert command_entrypoint(("bash", "-c", "scripts/gate.sh")) is None
    assert command_entrypoint(("git", "diff", "--check")) is None
    isolated = rewrite_frozen_entrypoint(
        ("python3", "-m", "compileall", "-q", "scripts/agents/dx"),
        Path("/nonexistent-run-dir"),
    )
    assert isolated == ["python3", "-P", "-m", "compileall", "-q", "scripts/agents/dx"]
    assert rewrite_frozen_entrypoint(
        ("python3", "-P", "-m", "compileall", "-q", "pkg"),
        Path("/nonexistent-run-dir"),
    ) == ["python3", "-P", "-m", "compileall", "-q", "pkg"]
    assert rewrite_frozen_entrypoint(
        ("python3", "-Im", "compileall", "-q", "pkg"),
        Path("/nonexistent-run-dir"),
    ) == ["python3", "-Im", "compileall", "-q", "pkg"]
    assert rewrite_frozen_entrypoint(
        ("python3", "-Bm", "compileall", "-q", "pkg"),
        Path("/nonexistent-run-dir"),
    ) == ["python3", "-P", "-Bm", "compileall", "-q", "pkg"]


def test_unauthorized_profile_change_keeps_stable_message(tmp_path: Path) -> None:
    repo, worktree, base = make_repo(tmp_path, profile=MINIMAL_PROFILE)
    run_dir = init_run(tmp_path, repo, worktree, base, allow=False)
    (worktree / ".agent-loop" / "project.toml").write_text(
        'schema_version = 1\n[approval]\nmode = "none"\n[executor]\ntimeout_seconds = 11\n',
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            *DX,
            "accept-candidate-profile",
            "--run-dir",
            str(run_dir),
            "--worktree",
            str(worktree),
            "--base-commit",
            base,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert UNAUTHORIZED_PROFILE_MESSAGE in completed.stderr
    with pytest.raises(RunStateError, match="project profile changed after run creation"):
        plan_resume(run_dir)


def test_task_name_does_not_authorize_profile_change(tmp_path: Path) -> None:
    repo, worktree, base = make_repo(tmp_path, profile=MINIMAL_PROFILE)
    run_dir = init_run(tmp_path, repo, worktree, base, allow=False)
    metadata = load_run_metadata(run_dir)
    assert metadata["candidate_profile_authorization"]["allowed"] is False
    assert metadata["candidate_profile_authorization"]["flag"] is None
    (worktree / ".agent-loop" / "project.toml").write_text(
        MINIMAL_PROFILE.replace("timeout_seconds = 10", "timeout_seconds = 12"),
        encoding="utf-8",
    )
    with pytest.raises(UnauthorizedProfileChange, match="project.toml"):
        accept_candidate_profile(run_dir, worktree, base)


def test_authorized_candidate_is_validated_but_not_activated(tmp_path: Path) -> None:
    gate = textwrap.dedent(
        """\
        #!/usr/bin/env bash
        set -euo pipefail
        printf 'frozen\\n' > "$AGENT_LOOP_RUN_DIR/gate-marker.txt"
        """
    )
    profile = MINIMAL_PROFILE.replace(
        'commands = [["git", "diff", "--check"]]',
        'commands = [["bash", "scripts/gate.sh"]]',
    )
    repo, worktree, base = make_repo(
        tmp_path,
        profile=profile,
        extra_files={"scripts/gate.sh": gate},
        mode={"scripts/gate.sh": 0o755},
    )
    run_dir = init_run(tmp_path, repo, worktree, base, allow=True)
    (worktree / "scripts" / "gate.sh").write_text(
        "#!/usr/bin/env bash\nprintf 'candidate\\n' > \"$AGENT_LOOP_RUN_DIR/gate-marker.txt\"\nexit 1\n",
        encoding="utf-8",
    )
    (worktree / "scripts" / "gate.sh").chmod(0o755)
    (worktree / ".agent-loop" / "project.toml").write_text(
        profile.replace('commands = [["bash", "scripts/gate.sh"]]', "commands = []"),
        encoding="utf-8",
    )
    accepted = accept_candidate_profile(run_dir, worktree, base)
    assert accepted["differs_from_frozen"] is True
    frozen = load_control_profile(run_dir, worktree)
    live = load_project_profile(worktree)
    assert frozen.validation_commands == (("bash", "scripts/gate.sh"),)
    assert live.validation_commands == ()
    rewritten = rewrite_frozen_entrypoint(("bash", "scripts/gate.sh"), run_dir)
    executed = subprocess.run(
        rewritten,
        cwd=str(worktree),
        env={**os.environ, "AGENT_LOOP_RUN_DIR": str(run_dir)},
        check=False,
        capture_output=True,
        text=True,
    )
    assert executed.returncode == 0, executed.stdout + executed.stderr
    assert (run_dir / "gate-marker.txt").read_text(encoding="utf-8") == "frozen\n"


def test_candidate_reviewer_instructions_do_not_affect_frozen_run(tmp_path: Path) -> None:
    repo, worktree, base = make_repo(
        tmp_path,
        profile=MINIMAL_PROFILE
        + '\n[instructions]\nreviewer = [".agent-loop/reviewer.md"]\n',
        extra_files={".agent-loop/reviewer.md": "FROZEN_REVIEWER_INSTRUCTION\n"},
    )
    run_dir = init_run(tmp_path, repo, worktree, base, allow=True)
    injected = worktree / ".agent-loop" / "reviewer.md"
    injected.write_text("INJECTED_REVIEWER_INSTRUCTION\n", encoding="utf-8")
    (worktree / ".agent-loop" / "executor.md").write_text(
        "INJECTED_EXECUTOR_INSTRUCTION\n", encoding="utf-8"
    )
    frozen_text = load_frozen_instruction_text(run_dir, "reviewer")
    live_text = load_instruction_text(load_project_profile(worktree), worktree, "reviewer")
    assert "FROZEN_REVIEWER_INSTRUCTION" in frozen_text
    assert "INJECTED_REVIEWER_INSTRUCTION" not in frozen_text
    assert "INJECTED_EXECUTOR_INSTRUCTION" not in load_frozen_instruction_text(
        run_dir, "executor"
    )
    assert "INJECTED_REVIEWER_INSTRUCTION" in live_text
    cli = subprocess.run(
        [*DX, "instructions", "--run-dir", str(run_dir), "--phase", "reviewer"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert cli.returncode == 0, cli.stderr
    assert "FROZEN_REVIEWER_INSTRUCTION" in cli.stdout
    assert "INJECTED_REVIEWER_INSTRUCTION" not in cli.stdout


def test_invalid_candidate_profile_is_rejected(tmp_path: Path) -> None:
    repo, worktree, base = make_repo(tmp_path, profile=MINIMAL_PROFILE)
    run_dir = init_run(tmp_path, repo, worktree, base, allow=True)
    (worktree / ".agent-loop" / "project.toml").write_text(
        "schema_version = 2\n", encoding="utf-8"
    )
    completed = subprocess.run(
        [
            *DX,
            "accept-candidate-profile",
            "--run-dir",
            str(run_dir),
            "--worktree",
            str(worktree),
            "--base-commit",
            base,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1
    assert "candidate project profile is invalid" in completed.stderr


def test_tampered_authorization_blocks_resume(tmp_path: Path) -> None:
    repo, worktree, base = make_repo(tmp_path, profile=MINIMAL_PROFILE)
    run_dir = init_run(tmp_path, repo, worktree, base, allow=False)
    from dx.state_machine import read_state_document
    from dx.atomic import atomic_write_json

    state = read_state_document(run_dir)
    assert state is not None
    state["metadata"]["candidate_profile_authorization"]["allowed"] = True
    atomic_write_json(run_dir / "state.json", state)
    with pytest.raises(RunStateError, match="tampered|diverges"):
        plan_resume(run_dir)


def _approve_with_manifest(run_dir: Path, worktree: Path, base: str) -> None:
    transition_run(run_dir, RunEvent.RUN_STARTED)
    transition_run(run_dir, RunEvent.REVIEW_STARTED)
    from dx.atomic import atomic_write_json

    atomic_write_json(run_dir / "reviewed_manifest.json", build_snapshot_manifest(worktree, base))
    transition_run(run_dir, RunEvent.REVIEW_APPROVED)


def test_unauthorized_snapshot_cannot_integrate_profile_change(tmp_path: Path) -> None:
    repo, worktree, base = make_repo(tmp_path, profile=MINIMAL_PROFILE)
    run_dir = init_run(tmp_path, repo, worktree, base, allow=False)
    (worktree / "app.txt").write_text("changed\n", encoding="utf-8")
    (worktree / ".agent-loop" / "project.toml").write_text(
        MINIMAL_PROFILE.replace("timeout_seconds = 10", "timeout_seconds = 20"),
        encoding="utf-8",
    )
    _approve_with_manifest(run_dir, worktree, base)
    with pytest.raises((IntegrationError, RunStateError), match="changed after run|tampered"):
        integrate_reviewed_snapshot(run_dir)


def test_metadata_snapshot_divergence_blocks_verify(tmp_path: Path) -> None:
    repo, worktree, base = make_repo(tmp_path, profile=MINIMAL_PROFILE)
    run_dir = init_run(tmp_path, repo, worktree, base, allow=True)
    (worktree / "app.txt").write_text("changed\n", encoding="utf-8")
    (worktree / ".agent-loop" / "project.toml").write_text(
        MINIMAL_PROFILE.replace("timeout_seconds = 10", "timeout_seconds = 20"),
        encoding="utf-8",
    )
    accept_candidate_profile(run_dir, worktree, base)
    recorded = json.loads((run_dir / "candidate-profile.json").read_text(encoding="utf-8"))
    recorded["sha256"] = "0" * 64
    (run_dir / "candidate-profile.json").write_text(
        json.dumps(recorded, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _approve_with_manifest(run_dir, worktree, base)
    with pytest.raises(Exception, match="diverges"):
        verify_reviewed_snapshot(run_dir)


def test_candidate_cannot_create_configured_gate_absent_from_base(tmp_path: Path) -> None:
    profile = MINIMAL_PROFILE.replace(
        'commands = [["git", "diff", "--check"]]',
        'commands = [["bash", "-e", "scripts/gate.sh"]]',
    )
    repo, worktree, base = make_repo(tmp_path, profile=profile)
    with pytest.raises(ControlAdapterError, match=MISSING_BASE_ENTRYPOINT):
        capture_control_adapter(repo, base)
    init = subprocess.run(
        [
            *DX,
            "init-run",
            "--run-dir",
            str(tmp_path / "state" / "runs" / "missing-gate"),
            "--repo",
            str(repo),
            "--worktree",
            str(worktree),
            "--task-file",
            "docs/tasks/SELF-00P.md",
            "--base-commit",
            base,
            "--max-iterations",
            "1",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert init.returncode == 1
    assert MISSING_BASE_ENTRYPOINT in init.stderr
    (worktree / "scripts").mkdir(parents=True, exist_ok=True)
    planted = worktree / "scripts" / "gate.sh"
    planted.write_text(
        "#!/usr/bin/env bash\nprintf 'candidate\\n'\nexit 0\n",
        encoding="utf-8",
    )
    planted.chmod(0o755)
    run_dir = tmp_path / "run-without-frozen-gate"
    run_dir.mkdir()
    for argv in (("bash", "scripts/gate.sh"), ("bash", "-e", "scripts/gate.sh")):
        with pytest.raises(ControlAdapterError, match=MISSING_FROZEN_ENTRYPOINT):
            rewrite_frozen_entrypoint(argv, run_dir)


def test_interpreter_option_gate_executes_frozen_snapshot_not_candidate(
    tmp_path: Path,
) -> None:
    gate = textwrap.dedent(
        """\
        #!/usr/bin/env bash
        set -euo pipefail
        printf 'frozen\\n' > "$AGENT_LOOP_RUN_DIR/gate-marker.txt"
        """
    )
    profile = MINIMAL_PROFILE.replace(
        'commands = [["git", "diff", "--check"]]',
        'commands = [["bash", "-e", "scripts/gate.sh"]]',
    )
    repo, worktree, base = make_repo(
        tmp_path,
        profile=profile,
        extra_files={"scripts/gate.sh": gate},
        mode={"scripts/gate.sh": 0o755},
    )
    capture = capture_control_adapter(repo, base)
    assert any(item["path"] == "scripts/gate.sh" for item in capture["entrypoints"])
    run_dir = init_run(tmp_path, repo, worktree, base, allow=True)
    (worktree / "scripts" / "gate.sh").write_text(
        "#!/usr/bin/env bash\nprintf 'candidate\\n' > \"$AGENT_LOOP_RUN_DIR/gate-marker.txt\"\nexit 1\n",
        encoding="utf-8",
    )
    (worktree / "scripts" / "gate.sh").chmod(0o755)
    rewritten = rewrite_frozen_entrypoint(("bash", "-e", "scripts/gate.sh"), run_dir)
    assert rewritten[0] == "bash"
    assert rewritten[1] == "-e"
    frozen = Path(rewritten[2])
    assert frozen.is_file()
    assert frozen.is_absolute()
    assert "control-adapter" in frozen.parts
    assert frozen.resolve() != (worktree / "scripts" / "gate.sh").resolve()
    assert "scripts/gate.sh" not in rewritten
    executed = subprocess.run(
        rewritten,
        cwd=str(worktree),
        env={**os.environ, "AGENT_LOOP_RUN_DIR": str(run_dir)},
        check=False,
        capture_output=True,
        text=True,
    )
    assert executed.returncode == 0, executed.stdout + executed.stderr
    assert (run_dir / "gate-marker.txt").read_text(encoding="utf-8") == "frozen\n"
    frozen.write_text(
        (worktree / "scripts" / "gate.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    with pytest.raises(ControlAdapterError, match=MISSING_FROZEN_ENTRYPOINT):
        rewrite_frozen_entrypoint(("bash", "-e", "scripts/gate.sh"), run_dir)


def test_candidate_compileall_module_is_not_executed_as_validation_gate(
    tmp_path: Path,
) -> None:
    """Planted compileall.py must not replace the frozen python -m compileall gate."""
    profile = MINIMAL_PROFILE.replace(
        'commands = [["git", "diff", "--check"]]',
        'commands = [["python3", "-m", "compileall", "-q", "pkg"]]',
    )
    repo, worktree, base = make_repo(
        tmp_path,
        profile=profile,
        extra_files={"pkg/__init__.py": "VALUE = 1\n"},
    )
    run_dir = init_run(tmp_path, repo, worktree, base, allow=True)
    planted = worktree / "compileall.py"
    planted.write_text(
        textwrap.dedent(
            """\
            import os
            from pathlib import Path
            marker = Path(os.environ["AGENT_LOOP_RUN_DIR"]) / "candidate-module.txt"
            marker.write_text("candidate-compileall-executed\\n", encoding="utf-8")
            raise SystemExit("candidate compileall executed")
            """
        ),
        encoding="utf-8",
    )
    command = ("python3", "-m", "compileall", "-q", "pkg")
    rewritten = rewrite_frozen_entrypoint(command, run_dir)
    assert rewritten[0] == "python3"
    assert rewritten[1] == "-P"
    assert rewritten[2:] == ["-m", "compileall", "-q", "pkg"]
    assert str(worktree / "compileall.py") not in rewritten
    unisolated = subprocess.run(
        list(command),
        cwd=str(worktree),
        env={**os.environ, "AGENT_LOOP_RUN_DIR": str(run_dir)},
        check=False,
        capture_output=True,
        text=True,
    )
    assert unisolated.returncode != 0
    assert "candidate compileall executed" in (unisolated.stdout + unisolated.stderr)
    candidate_marker = run_dir / "candidate-module.txt"
    assert candidate_marker.read_text(encoding="utf-8") == "candidate-compileall-executed\n"
    candidate_marker.unlink()
    executed = subprocess.run(
        rewritten,
        cwd=str(worktree),
        env={**os.environ, "AGENT_LOOP_RUN_DIR": str(run_dir)},
        check=False,
        capture_output=True,
        text=True,
    )
    assert executed.returncode == 0, executed.stdout + executed.stderr
    assert "candidate compileall executed" not in (executed.stdout + executed.stderr)
    assert not candidate_marker.exists()
    pycache = worktree / "pkg" / "__pycache__"
    assert pycache.is_dir()
    assert any(path.suffix == ".pyc" for path in pycache.iterdir())


def test_rewrite_uses_frozen_entrypoint_copy(tmp_path: Path) -> None:
    repo, worktree, base = make_repo(
        tmp_path,
        profile=MINIMAL_PROFILE.replace(
            'commands = [["git", "diff", "--check"]]',
            'commands = [["bash", "scripts/gate.sh"]]',
        ),
        extra_files={"scripts/gate.sh": "#!/bin/sh\nexit 0\n"},
        mode={"scripts/gate.sh": 0o755},
    )
    capture = capture_control_adapter(repo, base)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    materialize_control_adapter(run_dir, capture, {
        "schema_version": 1,
        "allowed": False,
        "origin": "cli",
        "flag": None,
        "base_commit": base,
    })
    rewritten = rewrite_frozen_entrypoint(("bash", "scripts/gate.sh"), run_dir)
    assert rewritten[0] == "bash"
    frozen = Path(rewritten[1])
    assert frozen.is_file()
    assert "control-adapter" in frozen.parts
    (worktree / "scripts" / "gate.sh").write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
    assert frozen.read_text(encoding="utf-8").endswith("exit 0\n")


def _write_agent(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_authorized_end_to_end_run_uses_frozen_control_and_integrates(
    tmp_path: Path,
) -> None:
    repo, _worktree, _base = make_repo(tmp_path / "target", profile=MINIMAL_PROFILE)
    cursor = tmp_path / "fake-cursor"
    _write_agent(
        cursor,
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            if [[ "${1:-}" == "status" ]]; then
              printf '%s\\n' "Logged in"
              exit 0
            fi
            printf '%s\\n' "changed" >> "$AGENT_LOOP_WORKTREE/app.txt"
            python3 - <<'PY'
            from pathlib import Path
            import os
            root = Path(os.environ["AGENT_LOOP_WORKTREE"])
            path = root / ".agent-loop" / "project.toml"
            text = path.read_text()
            path.write_text(text.replace("timeout_seconds = 10", "timeout_seconds = 20"))
            (root / ".agent-loop" / "reviewer.md").write_text("INJECTED_REVIEWER_INSTRUCTION\\n")
            PY
            printf '%s\\n' '{"summary":"updated app and candidate profile"}'
            """
        ),
    )
    prompt_log = tmp_path / "reviewer-prompt.txt"
    codex = tmp_path / "fake-codex"
    _write_agent(
        codex,
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            if [[ "${{1:-}}" == "login" ]]; then
              printf '%s\\n' "Logged in"
              exit 0
            fi
            output=""
            prompt=""
            while [[ $# -gt 0 ]]; do
              if [[ "$1" == "--output-last-message" ]]; then
                output="$2"
                shift 2
              else
                prompt="$1"
                shift
              fi
            done
            printf '%s\\n' "$prompt" > {str(prompt_log)!r}
            printf '%s\\n' '{{"status":"APPROVED","summary":"ok","findings":[],"tests_required":[]}}' > "$output"
            """
        ),
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
            "run",
            "--repo",
            str(repo),
            "--state-root",
            str(state_root),
            "--allow-candidate-profile",
            "docs/tasks/SELF-00P.md",
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
    metadata = load_run_metadata(run_dir)
    assert metadata["candidate_profile_authorization"]["allowed"] is True
    assert metadata["candidate_profile_authorization"]["flag"] == CANDIDATE_PROFILE_FLAG
    assert metadata["profile"]["executor"]["timeout_seconds"] == 10
    prompt = prompt_log.read_text(encoding="utf-8")
    assert "INJECTED_REVIEWER_INSTRUCTION" not in prompt
    worktree = Path(str(metadata["worktree"]))
    assert "timeout_seconds = 20" in (
        worktree / ".agent-loop" / "project.toml"
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

    integrated = subprocess.run(
        [
            str(REPO_ROOT / "agent-loop"),
            "integrate",
            "--run-dir",
            str(run_dir),
            "--message",
            "SELF-00P: authorized candidate profile",
        ],
        cwd=REPO_ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert integrated.returncode == 0, integrated.stdout + integrated.stderr
    committed = git(repo, "show", "HEAD:.agent-loop/project.toml")
    assert "timeout_seconds = 20" in committed
    assert git(repo, "rev-parse", "HEAD^") == metadata["base_commit"]


def test_unauthorized_end_to_end_run_still_refuses_profile_mutation(
    tmp_path: Path,
) -> None:
    repo, _worktree, _base = make_repo(tmp_path / "target", profile=MINIMAL_PROFILE)
    cursor = tmp_path / "fake-cursor"
    _write_agent(
        cursor,
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            if [[ "${1:-}" == "status" ]]; then
              printf '%s\\n' "Logged in"
              exit 0
            fi
            printf '%s\\n' "changed" >> "$AGENT_LOOP_WORKTREE/app.txt"
            printf '%s\\n' 'schema_version = 1' > "$AGENT_LOOP_WORKTREE/.agent-loop/project.toml"
            printf '%s\\n' '{"summary":"mutated profile"}'
            """
        ),
    )
    codex = tmp_path / "fake-codex"
    _write_agent(
        codex,
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            if [[ "${1:-}" == "login" ]]; then echo 'Logged in'; exit 0; fi
            exit 99
            """
        ),
    )
    completed = subprocess.run(
        [
            str(REPO_ROOT / "agent-loop"),
            "run",
            "--repo",
            str(repo),
            "--state-root",
            str(tmp_path / "state"),
            "docs/tasks/SELF-00P.md",
            "1",
            "main",
        ],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "CURSOR_AGENT_BIN": str(cursor),
            "CODEX_BIN": str(codex),
            "AGENT_LOOP_PYTHON": sys.executable,
        },
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    assert completed.returncode != 0
    combined = completed.stdout + completed.stderr
    assert UNAUTHORIZED_PROFILE_MESSAGE in combined
    assert "INJECTED" not in combined


def test_public_run_without_flag_still_accepts_current_consumer_profile(
    tmp_path: Path,
) -> None:
    repo, _worktree, _base = make_repo(tmp_path / "target", profile=MINIMAL_PROFILE)
    cursor = tmp_path / "fake-cursor"
    _write_agent(
        cursor,
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            if [[ "${1:-}" == "status" ]]; then printf '%s\\n' "Logged in"; exit 0; fi
            printf '%s\\n' "changed" >> "$AGENT_LOOP_WORKTREE/app.txt"
            printf '%s\\n' '{"summary":"changed app"}'
            """
        ),
    )
    codex = tmp_path / "fake-codex"
    _write_agent(
        codex,
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            if [[ "${1:-}" == "login" ]]; then printf '%s\\n' "Logged in"; exit 0; fi
            output=""
            while [[ $# -gt 0 ]]; do
              if [[ "$1" == "--output-last-message" ]]; then output="$2"; shift 2; else shift; fi
            done
            printf '%s\\n' '{"status":"APPROVED","summary":"ok","findings":[],"tests_required":[]}' > "$output"
            """
        ),
    )
    completed = subprocess.run(
        [
            str(REPO_ROOT / "agent-loop"),
            "run",
            "--repo",
            str(repo),
            "--state-root",
            str(tmp_path / "state"),
            "--require-profile",
            "docs/tasks/SELF-00P.md",
            "1",
            "main",
        ],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "CURSOR_AGENT_BIN": str(cursor),
            "CODEX_BIN": str(codex),
            "AGENT_LOOP_PYTHON": sys.executable,
        },
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    usage = subprocess.run(
        [str(REPO_ROOT / "agent-loop")],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert "--allow-candidate-profile" in usage.stdout + usage.stderr
    assert "--require-profile" in usage.stdout + usage.stderr
