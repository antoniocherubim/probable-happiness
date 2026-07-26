"""Personal Core supervisor regressions."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS = REPO_ROOT / "scripts" / "agents"
sys.path.insert(0, str(AGENTS))

import dx.runtime as runtime  # noqa: E402
from dx.systemd_scope import SystemdScopeError, scope_unit_basename  # noqa: E402


def test_scope_name_is_deterministic_safe_and_bounded() -> None:
    first = scope_unit_basename("../../run com espaços", "executor", 4)
    second = scope_unit_basename("../../run com espaços", "executor", 4)

    assert first == second
    assert first.startswith("agent-loop-")
    assert len(first) < 180
    assert "/" not in first
    assert " " not in first
    assert first != scope_unit_basename("../../run com espaços", "reviewer", 4)


def test_supervisor_fails_closed_and_removes_raw_files_without_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_dir = tmp_path / "run"

    def unavailable(*_args: object, **_kwargs: object) -> object:
        raise SystemdScopeError("manager unavailable")

    monkeypatch.setattr(runtime, "start_scoped_popen", unavailable)

    with pytest.raises(SystemdScopeError, match="manager unavailable"):
        runtime.supervise_command(
            command=["true"],
            phase="executor",
            iteration=1,
            cwd=worktree,
            run_dir=run_dir,
            environment={},
            secret_values={},
            timeout_seconds=5,
            heartbeat_seconds=1,
            terminate_grace_seconds=1,
        )

    assert not list(run_dir.glob(".*.raw"))
    assert not (run_dir / "executor-1-result.json").exists()
