"""Process supervision, timeouts, heartbeats, and sanitized project commands."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Mapping, Sequence

from .atomic import atomic_write_bytes, atomic_write_json, atomic_write_text
from .profile import ProjectProfile, sanitize_text
from .systemd_scope import (
    scope_unit_basename,
    start_scoped_popen,
    stop_user_scope,
)


TIMEOUT_EXIT = 124
RESOURCE_LIMIT_EXIT = 125


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _changed_files(worktree: Path) -> int:
    try:
        output = subprocess.check_output(
            ["git", "-C", str(worktree), "status", "--porcelain=v1", "-z"],
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return 0
    return len([item for item in output.split(b"\0") if item])


def _elapsed(value: float) -> str:
    seconds = max(0, int(value))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


@dataclass
class _Activity:
    timestamp: str
    lock: threading.Lock

    def touch(self) -> None:
        with self.lock:
            self.timestamp = _now()

    def get(self) -> str:
        with self.lock:
            return self.timestamp


@dataclass
class _OutputBudget:
    limit: int
    used: int
    exceeded: bool
    lock: threading.Lock

    def accept(self, chunk: bytes) -> bytes:
        with self.lock:
            remaining = max(0, self.limit - self.used)
            accepted = chunk[:remaining]
            self.used += len(accepted)
            if len(accepted) != len(chunk):
                self.exceeded = True
            return accepted


def _copy_stream(
    source: IO[bytes],
    destination: IO[bytes],
    activity: _Activity,
    budget: _OutputBudget,
) -> None:
    try:
        while True:
            chunk = source.read(65536)
            if not chunk:
                break
            accepted = budget.accept(chunk)
            if accepted:
                destination.write(accepted)
                destination.flush()
                activity.touch()
            if len(accepted) != len(chunk):
                break
    finally:
        source.close()


def _artifact_limit_reason(
    run_dir: Path,
    artifacts: Sequence[Path],
    *,
    max_files: int,
    max_file_bytes: int,
) -> str | None:
    count = 0
    try:
        for current, directories, files in os.walk(
            run_dir,
            topdown=True,
            followlinks=False,
        ):
            current_path = Path(current)
            for name in directories:
                if (current_path / name).is_symlink():
                    return "run_artifact_symlink"
            for name in files:
                path = current_path / name
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    return "run_artifact_special_file"
                count += 1
                if count > max_files:
                    return "run_artifact_file_count"
                if info.st_size > max_file_bytes:
                    return "run_artifact_file_size"
        for artifact in artifacts:
            try:
                info = artifact.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                return "phase_artifact_special_file"
            if info.st_size > max_file_bytes:
                return "phase_artifact_file_size"
    except OSError:
        return "run_artifact_inspection_failed"
    return None


def _reap_scope(
    basename: str,
    process: subprocess.Popen[bytes],
    grace_seconds: int,
) -> None:
    """Stop the full cgroup, then reap the systemd-run wrapper."""
    stop_user_scope(basename, grace_seconds=grace_seconds)
    if process.poll() is not None:
        return
    try:
        process.wait(timeout=max(1, grace_seconds))
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
    except OSError:
        return
    try:
        process.wait(timeout=max(1, grace_seconds))
    except subprocess.TimeoutExpired:
        pass


def supervise_command(
    *,
    command: Sequence[str],
    phase: str,
    iteration: int,
    cwd: Path,
    run_dir: Path,
    environment: Mapping[str, str],
    secret_values: Mapping[str, str],
    timeout_seconds: int,
    heartbeat_seconds: int,
    terminate_grace_seconds: int,
    max_output_bytes: int = 16 * 1024 * 1024,
    max_file_bytes: int = 64 * 1024 * 1024,
    max_run_files: int = 512,
    memory_limit_bytes: int = 4 * 1024 * 1024 * 1024,
    task_limit: int = 512,
    report_path: Path | None = None,
    sanitize_artifacts: Sequence[Path] = (),
) -> int:
    if not command:
        raise ValueError("empty command")
    if min(
        max_output_bytes,
        max_file_bytes,
        max_run_files,
        memory_limit_bytes,
        task_limit,
    ) < 1:
        raise ValueError("resource limits must be positive")
    run_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{phase}-{iteration}" if iteration else phase
    raw_stdout = run_dir / f".{prefix}.stdout.raw"
    raw_stderr = run_dir / f".{prefix}.stderr.raw"
    phase_log = run_dir / f"{prefix}.log"
    result_path = run_dir / f"{prefix}-result.json"
    heartbeat_path = run_dir / "heartbeat.json"
    started = time.monotonic()
    activity = _Activity(_now(), threading.Lock())
    output_budget = _OutputBudget(
        max_output_bytes,
        0,
        False,
        threading.Lock(),
    )
    timed_out = False
    interrupted_signal: int | None = None
    scope_basename = scope_unit_basename(run_dir.name, phase, iteration)
    unit_name = f"{scope_basename}.scope"

    stdout_handle = raw_stdout.open("wb")
    stderr_handle = raw_stderr.open("wb")
    process: subprocess.Popen[bytes] | None = None
    threads: list[threading.Thread] = []
    previous_handlers: dict[int, object] = {}
    try:
        process = start_scoped_popen(
            command,
            basename=scope_basename,
            cwd=cwd,
            environment=environment,
            file_limit_bytes=max_file_bytes,
            memory_limit_bytes=memory_limit_bytes,
            task_limit=task_limit,
        )
        assert process.stdout is not None and process.stderr is not None
        threads = [
            threading.Thread(
                target=_copy_stream,
                args=(process.stdout, stdout_handle, activity, output_budget),
                daemon=True,
            ),
            threading.Thread(
                target=_copy_stream,
                args=(process.stderr, stderr_handle, activity, output_budget),
                daemon=True,
            ),
        ]
        for thread in threads:
            thread.start()

        def forward(signum: int, _frame: object) -> None:
            nonlocal interrupted_signal
            interrupted_signal = signum

        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous_handlers[signum] = signal.signal(signum, forward)
        try:
            next_heartbeat = started
            previous_changed = _changed_files(cwd)
            while process.poll() is None:
                now = time.monotonic()
                if interrupted_signal is not None:
                    break
                if output_budget.exceeded:
                    break
                if now - started >= timeout_seconds:
                    timed_out = True
                    _reap_scope(scope_basename, process, terminate_grace_seconds)
                    break
                if now >= next_heartbeat:
                    changed = _changed_files(cwd)
                    if changed != previous_changed:
                        activity.touch()
                        previous_changed = changed
                    from .state_machine import read_status

                    run_state = read_status(run_dir)
                    payload = {
                        "schema_version": 1,
                        "phase": phase,
                        "iteration": iteration,
                        "elapsed_seconds": int(now - started),
                        "pid": process.pid,
                        "process_group": None,
                        "systemd_unit": unit_name,
                        "last_activity_at": activity.get(),
                        "changed_files": changed,
                        "state": run_state or "active",
                        "process_state": "active",
                        "observed_at": _now(),
                    }
                    atomic_write_json(heartbeat_path, payload)
                    print(
                        f"[agent-loop] {phase.capitalize()} iteration={iteration} active "
                        f"elapsed={_elapsed(now - started)} pid={process.pid} "
                        f"unit={unit_name} last_activity={activity.get()} "
                        f"changed_files={changed}",
                        flush=True,
                    )
                    next_heartbeat = now + heartbeat_seconds
                time.sleep(min(0.2, max(0.05, heartbeat_seconds / 5)))
            if process.poll() is None:
                _reap_scope(scope_basename, process, terminate_grace_seconds)
            return_code = process.wait()
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
    finally:
        cleanup_error: BaseException | None = None
        if process is not None:
            try:
                _reap_scope(scope_basename, process, terminate_grace_seconds)
            except BaseException as exc:
                cleanup_error = exc
        for thread in threads:
            thread.join(timeout=2)
        stdout_handle.close()
        stderr_handle.close()
        if process is None:
            raw_stdout.unlink(missing_ok=True)
            raw_stderr.unlink(missing_ok=True)
        if cleanup_error is not None:
            raise cleanup_error

    stdout_text = raw_stdout.read_text(encoding="utf-8", errors="replace")
    stderr_text = raw_stderr.read_text(encoding="utf-8", errors="replace")
    sanitized_stdout = sanitize_text(stdout_text, secret_values)
    sanitized_stderr = sanitize_text(stderr_text, secret_values)
    if report_path is not None:
        atomic_write_bytes(report_path, sanitized_stdout.encode("utf-8"))
    combined = sanitized_stderr
    if report_path is None:
        combined = sanitized_stdout + sanitized_stderr
    atomic_write_text(phase_log, combined)
    artifact_limit = _artifact_limit_reason(
        run_dir,
        sanitize_artifacts,
        max_files=max_run_files,
        max_file_bytes=max_file_bytes,
    )
    if artifact_limit is None:
        for artifact in sanitize_artifacts:
            if artifact.is_file():
                text = artifact.read_text(encoding="utf-8", errors="replace")
                atomic_write_bytes(
                    artifact,
                    sanitize_text(text, secret_values).encode("utf-8"),
                )
    raw_stdout.unlink(missing_ok=True)
    raw_stderr.unlink(missing_ok=True)

    reason = None
    effective_exit = return_code
    state = "completed" if return_code == 0 else "failed"
    if timed_out:
        reason = f"{phase}_timeout"
        effective_exit = TIMEOUT_EXIT
        state = "timeout"
    elif interrupted_signal is not None:
        reason = f"{phase}_interrupted"
        effective_exit = 128 + interrupted_signal
        state = "interrupted"
    elif output_budget.exceeded:
        reason = f"{phase}_output_limit"
        effective_exit = RESOURCE_LIMIT_EXIT
        state = "resource_limit"
    elif artifact_limit is not None:
        reason = artifact_limit
        effective_exit = RESOURCE_LIMIT_EXIT
        state = "resource_limit"
    finished = time.monotonic()
    result = {
        "schema_version": 1,
        "phase": phase,
        "iteration": iteration,
        "state": state,
        "reason": reason,
        "exit_code": effective_exit,
        "child_exit_code": return_code,
        "elapsed_seconds": round(finished - started, 3),
        "last_activity_at": activity.get(),
        "changed_files": _changed_files(cwd),
        "finished_at": _now(),
        "systemd_unit": unit_name,
        "captured_output_bytes": output_budget.used,
    }
    atomic_write_json(result_path, result)
    atomic_write_json(
        heartbeat_path,
        {
            **{key: value for key, value in result.items() if key not in {"reason", "child_exit_code"}},
            "process_state": result["state"],
            "process_group": None,
            "systemd_unit": unit_name,
            "pid": None,
        },
    )
    return effective_exit


def phase_settings(profile: ProjectProfile, phase: str) -> tuple[int, int]:
    if phase == "bootstrap":
        return profile.bootstrap_timeout_seconds, profile.executor_heartbeat_seconds
    if phase in {"executor", "validation"}:
        return profile.executor_timeout_seconds, profile.executor_heartbeat_seconds
    if phase == "reviewer":
        return profile.reviewer_timeout_seconds, profile.reviewer_heartbeat_seconds
    raise ValueError(f"unsupported phase: {phase}")


def tracked_worktree_clean(worktree: Path, expected_head: str | None = None) -> bool:
    if expected_head is not None:
        try:
            head = subprocess.check_output(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            return False
        if head != expected_head:
            return False
    for command in (
        ["git", "-C", str(worktree), "diff", "--quiet", "HEAD", "--"],
        ["git", "-C", str(worktree), "diff", "--cached", "--quiet", "HEAD", "--"],
    ):
        if subprocess.run(command, check=False).returncode != 0:
            return False
    return True
