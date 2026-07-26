"""Mandatory real-systemd gate for Personal Core process cleanup."""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS = REPO_ROOT / "scripts" / "agents"
sys.path.insert(0, str(AGENTS))

from dx.runtime import TIMEOUT_EXIT, supervise_command  # noqa: E402
from dx.systemd_scope import scope_active_state, scope_unit_basename  # noqa: E402


def _escaped_command(
    pid_path: Path,
    *,
    parent_sleep: float,
    exit_code: int,
) -> list[str]:
    source = (
        "import pathlib,subprocess,time; "
        "child=subprocess.Popen("
        "['sleep','60'], start_new_session=True, "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL, close_fds=True); "
        f"pathlib.Path({str(pid_path)!r}).write_text(str(child.pid)); "
        f"time.sleep({parent_sleep}); "
        f"raise SystemExit({exit_code})"
    )
    return [sys.executable, "-c", source]


def _wait_for_file(path: Path, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not path.is_file():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {path}")
        time.sleep(0.05)


def _assert_pid_gone(path: Path) -> None:
    _wait_for_file(path)
    pid = int(path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while Path(f"/proc/{pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not Path(f"/proc/{pid}").exists(), f"descendant {pid} survived"


def _assert_scope_collected(run_dir: Path, phase: str, iteration: int) -> None:
    basename = scope_unit_basename(run_dir.name, phase, iteration)
    deadline = time.monotonic() + 5
    while scope_active_state(basename) is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert scope_active_state(basename) is None


def _unit_resource_properties(
    run_dir: Path,
    phase: str,
    iteration: int,
) -> dict[str, str]:
    unit = f"{scope_unit_basename(run_dir.name, phase, iteration)}.scope"
    output = subprocess.check_output(
        [
            "systemctl",
            "--user",
            "show",
            unit,
            "--property=MemoryMax",
            "--property=TasksMax",
        ],
        text=True,
    )
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


@pytest.mark.parametrize(
    ("label", "exit_code", "timeout_seconds", "expected"),
    (
        ("success", 0, 5, 0),
        ("error", 7, 5, 7),
        ("timeout", 0, 1, TIMEOUT_EXIT),
    ),
)
def test_real_scope_reaps_setsid_descendant(
    tmp_path: Path,
    label: str,
    exit_code: int,
    timeout_seconds: int,
    expected: int,
) -> None:
    worktree = tmp_path / f"worktree-{label}"
    worktree.mkdir()
    run_dir = tmp_path / f"run-{label}"
    pid_path = tmp_path / f"{label}.pid"
    parent_sleep = 60 if label == "timeout" else 0

    result = supervise_command(
        command=_escaped_command(
            pid_path,
            parent_sleep=parent_sleep,
            exit_code=exit_code,
        ),
        phase="executor",
        iteration=1,
        cwd=worktree,
        run_dir=run_dir,
        environment={"PATH": os.environ["PATH"]},
        secret_values={},
        timeout_seconds=timeout_seconds,
        heartbeat_seconds=1,
        terminate_grace_seconds=1,
        memory_limit_bytes=256 * 1024 * 1024,
        task_limit=32,
    )

    assert result == expected
    _assert_pid_gone(pid_path)
    _assert_scope_collected(run_dir, "executor", 1)


def _signal_worker(worktree: str, run_dir: str, pid_path: str) -> None:
    result = supervise_command(
        command=_escaped_command(
            Path(pid_path),
            parent_sleep=60,
            exit_code=0,
        ),
        phase="reviewer",
        iteration=2,
        cwd=Path(worktree),
        run_dir=Path(run_dir),
        environment={"PATH": os.environ["PATH"]},
        secret_values={},
        timeout_seconds=30,
        heartbeat_seconds=1,
        terminate_grace_seconds=1,
        memory_limit_bytes=256 * 1024 * 1024,
        task_limit=32,
    )
    (Path(run_dir) / "signal-worker-result.json").write_text(
        json.dumps({"result": result}),
        encoding="utf-8",
    )


def test_real_scope_reaps_setsid_descendant_after_signal(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree-signal"
    worktree.mkdir()
    run_dir = tmp_path / "run-signal"
    pid_path = tmp_path / "signal.pid"
    context = multiprocessing.get_context("fork")
    worker = context.Process(
        target=_signal_worker,
        args=(str(worktree), str(run_dir), str(pid_path)),
    )
    worker.start()
    _wait_for_file(pid_path)
    properties = _unit_resource_properties(run_dir, "reviewer", 2)
    assert properties["MemoryMax"] == str(256 * 1024 * 1024)
    assert properties["TasksMax"] == "32"
    os.kill(worker.pid, signal.SIGTERM)
    worker.join(timeout=15)

    assert not worker.is_alive()
    assert worker.exitcode == 0
    result = json.loads(
        (run_dir / "signal-worker-result.json").read_text(encoding="utf-8")
    )
    assert result["result"] == 128 + signal.SIGTERM
    _assert_pid_gone(pid_path)
    _assert_scope_collected(run_dir, "reviewer", 2)
