"""PS-01 — systemd --user scopes and descendant cleanup."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS = REPO_ROOT / "scripts" / "agents"
sys.path.insert(0, str(AGENTS))

from dx import systemd_scope  # noqa: E402
from dx.runtime import TIMEOUT_EXIT, supervise_command  # noqa: E402
from dx.systemd_scope import (  # noqa: E402
    SystemdScopeError,
    cgroup_is_empty,
    cgroup_pids,
    ensure_scope_reaped,
    scope_active_state,
    scope_control_group,
    scope_unit_basename,
    start_scoped_popen,
    stop_user_scope,
    user_systemd_available,
)


def _require_user_systemd() -> None:
    ok, detail = user_systemd_available()
    if not ok:
        pytest.fail(
            f"PS-01 requires a live systemd --user manager; not skipped. detail={detail!r}"
        )


def _wait_gone(pid: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not Path(f"/proc/{pid}").exists():
            return
        time.sleep(0.05)
    assert not Path(f"/proc/{pid}").exists()


def _git_worktree(tmp_path: Path) -> Path:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    subprocess.run(["git", "init", str(worktree)], check=True, capture_output=True)
    return worktree


def _assert_scope_gone(basename: str) -> None:
    assert scope_active_state(basename) in {None, "inactive", "failed"}
    assert cgroup_is_empty(scope_control_group(basename))


def test_scope_unit_basename_is_stable_and_sanitized() -> None:
    name = scope_unit_basename("run:ps-01/../x", "executor", 2)
    assert name.startswith("agent-loop-")
    assert ".." not in name
    assert "/" not in name
    assert " " not in name
    assert name.endswith("-executor-2")


def test_systemd_unit_is_accepted_by_strict_phase_result_contracts(
    tmp_path: Path,
) -> None:
    from dx.runstate import _validate_resume_report_contract
    from dx.snapshot import _validate_validation_result

    result = {
        "schema_version": 1,
        "phase": "validation",
        "iteration": 1,
        "state": "completed",
        "reason": None,
        "exit_code": 0,
        "child_exit_code": 0,
        "elapsed_seconds": 0.1,
        "last_activity_at": "2026-07-26T00:00:00Z",
        "changed_files": 0,
        "finished_at": "2026-07-26T00:00:01Z",
        "systemd_unit": "agent-loop-test-validation-1.scope",
    }

    assert _validate_validation_result(
        tmp_path / "validation-1-result.json", result
    ) == result
    _validate_resume_report_contract(
        tmp_path / "reviewer-1-result.json",
        {**result, "phase": "reviewer"},
        label="reviewer-1-result.json",
    )

    # Results created before PS-01 remain readable.
    legacy = {key: value for key, value in result.items() if key != "systemd_unit"}
    assert _validate_validation_result(
        tmp_path / "validation-legacy-result.json", legacy
    ) == legacy


def test_supervise_refuses_start_without_user_systemd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = _git_worktree(tmp_path)
    run_dir = tmp_path / "run-refuse"
    run_dir.mkdir(mode=0o700)
    monkeypatch.setattr(
        "dx.runtime.start_scoped_popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            SystemdScopeError("refusing to start phase without systemd --user scope (offline)")
        ),
    )
    with pytest.raises(SystemdScopeError, match="refusing to start"):
        supervise_command(
            command=[sys.executable, "-c", "print('should-not-run')"],
            phase="executor",
            iteration=1,
            cwd=worktree,
            run_dir=run_dir,
            environment={"PATH": os.environ["PATH"]},
            secret_values={},
            timeout_seconds=5,
            heartbeat_seconds=1,
            terminate_grace_seconds=1,
        )
    assert not (run_dir / "executor-1-result.json").exists()


def test_normal_phase_keeps_result_logs_and_records_unit(tmp_path: Path) -> None:
    _require_user_systemd()
    worktree = _git_worktree(tmp_path)
    run_dir = tmp_path / "run-ok"
    run_dir.mkdir(mode=0o700)
    result = supervise_command(
        command=[
            sys.executable,
            "-c",
            "import sys; print('hello-out'); print('hello-err', file=sys.stderr)",
        ],
        phase="executor",
        iteration=1,
        cwd=worktree,
        run_dir=run_dir,
        environment={"PATH": os.environ["PATH"]},
        secret_values={},
        timeout_seconds=30,
        heartbeat_seconds=30,
        terminate_grace_seconds=2,
    )
    assert result == 0
    phase_result = json.loads((run_dir / "executor-1-result.json").read_text(encoding="utf-8"))
    assert phase_result["state"] == "completed"
    unit = phase_result["systemd_unit"]
    assert unit.startswith("agent-loop-")
    assert unit.endswith(".scope")
    log = (run_dir / "executor-1.log").read_text(encoding="utf-8")
    assert "hello-out" in log
    assert "hello-err" in log
    heartbeat = json.loads((run_dir / "heartbeat.json").read_text(encoding="utf-8"))
    assert heartbeat["systemd_unit"] == unit
    assert heartbeat["process_group"] is None
    _assert_scope_gone(unit.removesuffix(".scope"))


def test_setsid_descendant_is_killed_on_timeout(tmp_path: Path) -> None:
    _require_user_systemd()
    worktree = _git_worktree(tmp_path)
    run_dir = tmp_path / "run-setsid"
    run_dir.mkdir(mode=0o700)
    child_pid = tmp_path / "setsid.pid"
    script = (
        "import pathlib, subprocess, time; "
        "p = subprocess.Popen(['sleep', '120'], start_new_session=True); "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(p.pid)); "
        "time.sleep(120)"
    )
    result = supervise_command(
        command=[sys.executable, "-c", script],
        phase="executor",
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
    phase_result = json.loads((run_dir / "executor-1-result.json").read_text(encoding="utf-8"))
    assert phase_result["reason"] == "executor_timeout"
    unit = phase_result["systemd_unit"].removesuffix(".scope")
    _assert_scope_gone(unit)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not child_pid.exists():
        time.sleep(0.05)
    assert child_pid.exists()
    _wait_gone(int(child_pid.read_text().strip()))


def test_successful_exit_reaps_surviving_setsid_descendant(tmp_path: Path) -> None:
    """Parent exits 0 while a setsid descendant remains — still stop/empty scope."""
    _require_user_systemd()
    worktree = _git_worktree(tmp_path)
    run_dir = tmp_path / "run-success-orphan"
    run_dir.mkdir(mode=0o700)
    child_pid = tmp_path / "survivor.pid"
    script = f"""
import pathlib, subprocess
child_path = pathlib.Path({str(child_pid)!r})
p = subprocess.Popen(['sleep', '120'], start_new_session=True)
child_path.write_text(str(p.pid))
print("parent-ok")
"""
    result = supervise_command(
        command=[sys.executable, "-c", script],
        phase="executor",
        iteration=1,
        cwd=worktree,
        run_dir=run_dir,
        environment={"PATH": os.environ["PATH"]},
        secret_values={},
        timeout_seconds=30,
        heartbeat_seconds=30,
        terminate_grace_seconds=2,
    )
    assert result == 0
    phase_result = json.loads((run_dir / "executor-1-result.json").read_text(encoding="utf-8"))
    assert phase_result["state"] == "completed"
    log = (run_dir / "executor-1.log").read_text(encoding="utf-8")
    assert "parent-ok" in log
    unit = phase_result["systemd_unit"].removesuffix(".scope")
    _assert_scope_gone(unit)
    assert child_pid.exists()
    _wait_gone(int(child_pid.read_text().strip()))


def test_orphaned_sandbox_wrapper_descendant_is_killed(tmp_path: Path) -> None:
    _require_user_systemd()
    worktree = _git_worktree(tmp_path)
    run_dir = tmp_path / "run-orphan"
    run_dir.mkdir(mode=0o700)
    orphan_pid = tmp_path / "orphan.pid"
    ready = tmp_path / "ready"
    script = f"""
import os, pathlib, time
ready = pathlib.Path({str(ready)!r})
orphan_path = pathlib.Path({str(orphan_pid)!r})
child = os.fork()
if child == 0:
    grandchild = os.fork()
    if grandchild == 0:
        orphan_path.write_text(str(os.getpid()))
        ready.write_text("1")
        time.sleep(120)
        raise SystemExit(0)
    raise SystemExit(0)
os.waitpid(child, 0)
while not ready.exists():
    time.sleep(0.05)
time.sleep(120)
"""
    result = supervise_command(
        command=[sys.executable, "-c", script],
        phase="executor",
        iteration=1,
        cwd=worktree,
        run_dir=run_dir,
        environment={"PATH": os.environ["PATH"]},
        secret_values={},
        timeout_seconds=2,
        heartbeat_seconds=1,
        terminate_grace_seconds=1,
    )
    assert result == TIMEOUT_EXIT
    unit = scope_unit_basename(run_dir.name, "executor", 1)
    _assert_scope_gone(unit)
    assert orphan_pid.exists()
    _wait_gone(int(orphan_pid.read_text().strip()))


def test_injected_error_after_spawn_leaves_unit_inactive_and_empty(tmp_path: Path) -> None:
    _require_user_systemd()
    worktree = _git_worktree(tmp_path)
    run_dir = tmp_path / "run-inject"
    run_dir.mkdir(mode=0o700)
    child_pid = tmp_path / "inject.pid"
    script = (
        "import os, pathlib, time; "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    status = run_dir / "status"
    status.write_text("EXECUTING\n", encoding="utf-8")
    os.chmod(status, 0o644)

    from dx.state_machine import StateTransitionError

    with pytest.raises(StateTransitionError, match="cannot be read safely|insecure"):
        supervise_command(
            command=[sys.executable, "-c", script],
            phase="executor",
            iteration=1,
            cwd=worktree,
            run_dir=run_dir,
            environment={"PATH": os.environ["PATH"]},
            secret_values={},
            timeout_seconds=30,
            heartbeat_seconds=1,
            terminate_grace_seconds=1,
        )
    unit = scope_unit_basename(run_dir.name, "executor", 1)
    _assert_scope_gone(unit)
    if child_pid.exists():
        _wait_gone(int(child_pid.read_text().strip()))


def test_exception_before_signal_handlers_still_reaps_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Post-spawn setup failure must not leak the unit."""
    _require_user_systemd()
    worktree = _git_worktree(tmp_path)
    run_dir = tmp_path / "run-presignal"
    run_dir.mkdir(mode=0o700)
    child_pid = tmp_path / "presignal.pid"
    script = (
        "import os, pathlib, time; "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    real_signal = signal.signal

    def boom_on_sigterm(signum: int, handler: object) -> object:
        if signum == signal.SIGTERM:
            raise RuntimeError("injected before signal-handler setup")
        return real_signal(signum, handler)  # type: ignore[arg-type]

    monkeypatch.setattr(signal, "signal", boom_on_sigterm)
    with pytest.raises(RuntimeError, match="injected before signal-handler setup"):
        supervise_command(
            command=[sys.executable, "-c", script],
            phase="executor",
            iteration=1,
            cwd=worktree,
            run_dir=run_dir,
            environment={"PATH": os.environ["PATH"]},
            secret_values={},
            timeout_seconds=30,
            heartbeat_seconds=30,
            terminate_grace_seconds=2,
        )
    unit = scope_unit_basename(run_dir.name, "executor", 1)
    _assert_scope_gone(unit)
    if child_pid.exists():
        _wait_gone(int(child_pid.read_text().strip()))


def test_systemctl_query_and_stop_failures_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom_show(*args: str, timeout: float = 30):
        raise subprocess.TimeoutExpired(cmd=["systemctl", *args], timeout=timeout)

    monkeypatch.setattr(systemd_scope, "_systemctl_user", boom_show)
    with pytest.raises(SystemdScopeError, match="failed to query"):
        scope_active_state("agent-loop-missing-query")

    def fail_stop(*args: str, timeout: float = 30):
        if args and args[0] == "stop":
            raise OSError("bus down")
        if args[:1] == ("show",):
            return SimpleNamespace(
                returncode=0,
                stdout="ActiveState=active\nLoadState=loaded\nControlGroup=/user.slice/x.scope\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(systemd_scope, "_systemctl_user", fail_stop)
    with pytest.raises(SystemdScopeError, match="failed to stop"):
        stop_user_scope("agent-loop-stop-fail", grace_seconds=1)


def test_incomplete_systemctl_show_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        systemd_scope,
        "_show_unit_properties",
        lambda basename, *properties: {"LoadState": "loaded"},
    )
    with pytest.raises(SystemdScopeError, match="missing ActiveState"):
        scope_active_state("agent-loop-incomplete-active")
    with pytest.raises(SystemdScopeError, match="missing ControlGroup"):
        scope_control_group("agent-loop-incomplete-cgroup")


def test_cgroup_read_errors_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cg = "/user.slice/agent-loop-fake.scope"
    fake_root = tmp_path / "sys" / "fs" / "cgroup" / "user.slice" / "agent-loop-fake.scope"
    fake_root.mkdir(parents=True)
    events = fake_root / "cgroup.events"
    events.write_text("populated 1\nfrozen 0\n", encoding="utf-8")
    (fake_root / "cgroup.procs").write_text("1\n", encoding="utf-8")

    monkeypatch.setattr(
        systemd_scope,
        "_cgroup_dir",
        lambda control_group: fake_root if control_group == cg else None,
    )
    assert cgroup_is_empty(cg) is False
    assert cgroup_pids(cg) == [1]

    events.chmod(0o000)
    try:
        with pytest.raises(SystemdScopeError, match="cannot read"):
            cgroup_is_empty(cg)
    finally:
        events.chmod(0o644)


def test_nested_child_cgroup_is_reaped(tmp_path: Path) -> None:
    """Recursive population via cgroup.events must see nested descendants."""
    _require_user_systemd()
    worktree = _git_worktree(tmp_path)
    run_dir = tmp_path / "run-nested"
    run_dir.mkdir(mode=0o700)
    nested_pid = tmp_path / "nested.pid"
    ready = tmp_path / "nested-ready"
    script = f"""
import os, pathlib, time
ready = pathlib.Path({str(ready)!r})
nested_path = pathlib.Path({str(nested_pid)!r})
cg = pathlib.Path("/proc/self/cgroup").read_text().strip().split(":")[-1]
root = pathlib.Path("/sys/fs/cgroup") / cg.lstrip("/")
child = root / "nested-ps01"
child.mkdir(exist_ok=True)
pid = os.fork()
if pid == 0:
    pathlib.Path(child / "cgroup.procs").write_text(str(os.getpid()))
    nested_path.write_text(str(os.getpid()))
    ready.write_text("1")
    time.sleep(120)
    raise SystemExit(0)
deadline = time.time() + 5
while time.time() < deadline and not ready.exists():
    time.sleep(0.05)
print("parent-done")
"""
    result = supervise_command(
        command=[sys.executable, "-c", script],
        phase="executor",
        iteration=1,
        cwd=worktree,
        run_dir=run_dir,
        environment={"PATH": os.environ["PATH"]},
        secret_values={},
        timeout_seconds=30,
        heartbeat_seconds=30,
        terminate_grace_seconds=2,
    )
    assert result == 0
    phase_result = json.loads((run_dir / "executor-1-result.json").read_text(encoding="utf-8"))
    assert phase_result["state"] == "completed"
    assert "parent-done" in (run_dir / "executor-1.log").read_text(encoding="utf-8")
    unit = phase_result["systemd_unit"].removesuffix(".scope")
    _assert_scope_gone(unit)
    assert nested_pid.exists()
    _wait_gone(int(nested_pid.read_text().strip()))


def test_transient_unit_creation_failure_is_scope_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """systemd-run rejection after preflight must raise SystemdScopeError."""
    _require_user_systemd()
    worktree = _git_worktree(tmp_path)
    basename = scope_unit_basename(tmp_path.name, "executor", 9)
    real_popen = subprocess.Popen

    def fake_popen(cmd, *args, **kwargs):
        if cmd and cmd[0] == "systemd-run":
            return real_popen(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stderr.write('Failed to start transient scope unit\\n'); sys.exit(1)",
                ],
                *args,
                **kwargs,
            )
        return real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    with pytest.raises(SystemdScopeError, match="refusing to start phase without systemd scope"):
        start_scoped_popen(
            [sys.executable, "-c", "print('nope')"],
            basename=basename,
            cwd=worktree,
            environment={"PATH": os.environ["PATH"]},
        )
    assert scope_active_state(basename) in {None, "inactive", "failed"}


def test_show_unit_properties_failure_after_spawn_reaps_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Post-Popen systemctl query faults must not leak the scoped wrapper."""
    _require_user_systemd()
    worktree = _git_worktree(tmp_path)
    basename = scope_unit_basename(tmp_path.name, "executor", 77)
    child_pid = tmp_path / "query-fail.pid"
    script = (
        "import os, pathlib, time; "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid())); "
        "time.sleep(120)"
    )
    real_show = systemd_scope._show_unit_properties
    real_popen = subprocess.Popen
    spawned = {"done": False}
    armed = {"fail": True}

    def tracking_popen(*args: object, **kwargs: object):
        proc = real_popen(*args, **kwargs)  # type: ignore[arg-type]
        spawned["done"] = True
        return proc

    def boom_after_popen(unit_basename: str, *properties: str) -> dict[str, str]:
        if unit_basename == basename and spawned["done"] and armed["fail"]:
            armed["fail"] = False
            raise SystemdScopeError("injected systemctl query failure")
        return real_show(unit_basename, *properties)

    monkeypatch.setattr(subprocess, "Popen", tracking_popen)
    monkeypatch.setattr(systemd_scope, "_show_unit_properties", boom_after_popen)
    with pytest.raises(SystemdScopeError, match="injected systemctl query failure"):
        start_scoped_popen(
            [sys.executable, "-c", script],
            basename=basename,
            cwd=worktree,
            environment={"PATH": os.environ["PATH"]},
        )
    assert spawned["done"] is True
    # Restore real queries for emptiness verification (cleanup already ran).
    monkeypatch.setattr(systemd_scope, "_show_unit_properties", real_show)
    _assert_scope_gone(basename)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not child_pid.exists():
        time.sleep(0.05)
    if child_pid.exists():
        _wait_gone(int(child_pid.read_text().strip()))
    assert cgroup_is_empty(scope_control_group(basename))


def test_short_command_nonzero_exit_is_not_scope_start_failure(tmp_path: Path) -> None:
    _require_user_systemd()
    worktree = _git_worktree(tmp_path)
    run_dir = tmp_path / "run-short-fail"
    run_dir.mkdir(mode=0o700)
    result = supervise_command(
        command=[sys.executable, "-c", "import sys; sys.exit(3)"],
        phase="executor",
        iteration=1,
        cwd=worktree,
        run_dir=run_dir,
        environment={"PATH": os.environ["PATH"]},
        secret_values={},
        timeout_seconds=30,
        heartbeat_seconds=30,
        terminate_grace_seconds=2,
    )
    assert result == 3
    phase_result = json.loads((run_dir / "executor-1-result.json").read_text(encoding="utf-8"))
    assert phase_result["state"] == "failed"
    assert phase_result["exit_code"] == 3
    _assert_scope_gone(phase_result["systemd_unit"].removesuffix(".scope"))


def test_bin_false_repeated_is_command_failure_not_scope_error(tmp_path: Path) -> None:
    """Genuinely fast /bin/false must stay a command failure across collect races."""
    _require_user_systemd()
    assert Path("/bin/false").is_file()
    worktree = _git_worktree(tmp_path)
    for i in range(25):
        run_dir = tmp_path / f"run-false-{i}"
        run_dir.mkdir(mode=0o700)
        result = supervise_command(
            command=["/bin/false"],
            phase="executor",
            iteration=1,
            cwd=worktree,
            run_dir=run_dir,
            environment={"PATH": os.environ["PATH"]},
            secret_values={},
            timeout_seconds=30,
            heartbeat_seconds=30,
            terminate_grace_seconds=2,
        )
        assert result == 1, f"iteration {i}: expected command exit 1, got {result!r}"
        phase_result = json.loads((run_dir / "executor-1-result.json").read_text(encoding="utf-8"))
        assert phase_result["state"] == "failed"
        assert phase_result["exit_code"] == 1
        _assert_scope_gone(phase_result["systemd_unit"].removesuffix(".scope"))


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_signal_cleanup_paths(signum: int, tmp_path: Path) -> None:
    _require_user_systemd()
    worktree = _git_worktree(tmp_path)
    run_dir = tmp_path / f"run-sig-{signum}"
    run_dir.mkdir(mode=0o700)
    ready = tmp_path / f"ready-{signum}"
    child_pid = tmp_path / f"child-{signum}.pid"
    script = (
        "import os, pathlib, time; "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid())); "
        f"pathlib.Path({str(ready)!r}).write_text('1'); "
        "time.sleep(60)"
    )
    handlers: dict[int, object] = {}
    original_signal = signal.signal
    error: list[BaseException] = []
    fired = threading.Event()

    def tracking_signal(sig: int, handler: object) -> object:
        previous = original_signal(sig, handler)
        handlers[sig] = handler
        return previous

    def fire_when_ready() -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if ready.exists() and signum in handlers:
                handler = handlers[signum]
                assert callable(handler)
                handler(signum, None)
                fired.set()
                return
            time.sleep(0.05)
        error.append(RuntimeError("handler was not installed in time"))

    signal.signal = tracking_signal  # type: ignore[assignment]
    try:
        side = threading.Thread(target=fire_when_ready, daemon=True)
        side.start()
        code = supervise_command(
            command=[sys.executable, "-c", script],
            phase="executor",
            iteration=1,
            cwd=worktree,
            run_dir=run_dir,
            environment={"PATH": os.environ["PATH"]},
            secret_values={},
            timeout_seconds=30,
            heartbeat_seconds=30,
            terminate_grace_seconds=2,
        )
    finally:
        signal.signal = original_signal  # type: ignore[assignment]

    assert not error, error
    assert fired.is_set()
    assert code == 128 + signum
    phase_result = json.loads((run_dir / "executor-1-result.json").read_text(encoding="utf-8"))
    assert phase_result["state"] == "interrupted"
    assert phase_result["reason"] == "executor_interrupted"
    unit = phase_result["systemd_unit"].removesuffix(".scope")
    _assert_scope_gone(unit)
    if child_pid.exists():
        _wait_gone(int(child_pid.read_text().strip()))


def test_systemd_user_integration_scope_lifecycle(tmp_path: Path) -> None:
    """Must execute against real systemd --user (never skip)."""
    _require_user_systemd()
    worktree = _git_worktree(tmp_path)
    run_dir = tmp_path / "run-integration"
    run_dir.mkdir(mode=0o700)
    unit_holder = tmp_path / "unit-during.txt"
    script = f"""
import pathlib, time
unit = pathlib.Path({str(unit_holder)!r})
cg = pathlib.Path('/proc/self/cgroup').read_text()
unit.write_text(cg)
time.sleep(1.2)
print('integration-ok')
"""
    result = supervise_command(
        command=[sys.executable, "-c", script],
        phase="reviewer",
        iteration=3,
        cwd=worktree,
        run_dir=run_dir,
        environment={"PATH": os.environ["PATH"]},
        secret_values={},
        timeout_seconds=30,
        heartbeat_seconds=1,
        terminate_grace_seconds=2,
    )
    assert result == 0
    expected = scope_unit_basename(run_dir.name, "reviewer", 3)
    cg_text = unit_holder.read_text(encoding="utf-8")
    assert expected in cg_text or f"{expected}.scope" in cg_text
    phase_result = json.loads((run_dir / "reviewer-3-result.json").read_text(encoding="utf-8"))
    assert phase_result["systemd_unit"] == f"{expected}.scope"
    log = (run_dir / "reviewer-3.log").read_text(encoding="utf-8")
    assert "integration-ok" in log
    _assert_scope_gone(expected)


def test_ensure_scope_reaped_rejects_populated_cgroup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    basename = "agent-loop-reap-populated"
    fake_root = tmp_path / "cg"
    fake_root.mkdir()
    (fake_root / "cgroup.events").write_text("populated 1\nfrozen 0\n", encoding="utf-8")
    (fake_root / "cgroup.procs").write_text("4242\n", encoding="utf-8")
    nested = fake_root / "child"
    nested.mkdir()
    (nested / "cgroup.procs").write_text("4243\n", encoding="utf-8")
    (nested / "cgroup.events").write_text("populated 1\nfrozen 0\n", encoding="utf-8")

    monkeypatch.setattr(systemd_scope, "scope_active_state", lambda _basename: "inactive")
    monkeypatch.setattr(
        systemd_scope,
        "scope_control_group",
        lambda _basename: "/user.slice/fake.scope",
    )
    monkeypatch.setattr(systemd_scope, "_cgroup_dir", lambda _cg: fake_root)
    with pytest.raises(SystemdScopeError, match="cgroup not empty"):
        ensure_scope_reaped(basename, timeout_seconds=0.2)
    assert cgroup_pids("/user.slice/fake.scope") == [4242, 4243]
