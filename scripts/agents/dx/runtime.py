"""Process supervision, timeouts, heartbeats, and sanitized project commands."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Mapping, Sequence

from .atomic import atomic_write_bytes, atomic_write_json, atomic_write_text
from .profile import ProjectProfile, sanitize_text


TIMEOUT_EXIT = 124


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


def _copy_stream(source: IO[bytes], destination: IO[bytes], activity: _Activity) -> None:
    try:
        while True:
            chunk = source.read(65536)
            if not chunk:
                break
            destination.write(chunk)
            destination.flush()
            activity.touch()
    finally:
        source.close()


def _terminate_group(process: subprocess.Popen[bytes], grace_seconds: int) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
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
    report_path: Path | None = None,
    sanitize_artifacts: Sequence[Path] = (),
) -> int:
    if not command:
        raise ValueError("empty command")
    run_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{phase}-{iteration}" if iteration else phase
    raw_stdout = run_dir / f".{prefix}.stdout.raw"
    raw_stderr = run_dir / f".{prefix}.stderr.raw"
    phase_log = run_dir / f"{prefix}.log"
    result_path = run_dir / f"{prefix}-result.json"
    heartbeat_path = run_dir / "heartbeat.json"
    started = time.monotonic()
    activity = _Activity(_now(), threading.Lock())
    timed_out = False
    interrupted_signal: int | None = None

    stdout_handle = raw_stdout.open("wb")
    stderr_handle = raw_stderr.open("wb")
    try:
        process = subprocess.Popen(
            list(command),
            cwd=str(cwd),
            env=dict(environment),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdout is not None and process.stderr is not None
        threads = [
            threading.Thread(
                target=_copy_stream,
                args=(process.stdout, stdout_handle, activity),
                daemon=True,
            ),
            threading.Thread(
                target=_copy_stream,
                args=(process.stderr, stderr_handle, activity),
                daemon=True,
            ),
        ]
        for thread in threads:
            thread.start()

        previous_handlers: dict[int, object] = {}

        def forward(signum: int, _frame: object) -> None:
            nonlocal interrupted_signal
            interrupted_signal = signum
            _terminate_group(process, terminate_grace_seconds)

        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous_handlers[signum] = signal.signal(signum, forward)
        try:
            next_heartbeat = started
            previous_changed = _changed_files(cwd)
            while process.poll() is None:
                now = time.monotonic()
                if interrupted_signal is not None:
                    break
                if now - started >= timeout_seconds:
                    timed_out = True
                    _terminate_group(process, terminate_grace_seconds)
                    break
                if now >= next_heartbeat:
                    changed = _changed_files(cwd)
                    if changed != previous_changed:
                        activity.touch()
                        previous_changed = changed
                    try:
                        run_state = (run_dir / "status").read_text(encoding="utf-8").strip()
                    except OSError:
                        run_state = ""
                    payload = {
                        "schema_version": 1,
                        "phase": phase,
                        "iteration": iteration,
                        "elapsed_seconds": int(now - started),
                        "pid": process.pid,
                        "process_group": process.pid,
                        "last_activity_at": activity.get(),
                        "changed_files": changed,
                        "state": run_state or "active",
                        "process_state": "active",
                        "observed_at": _now(),
                    }
                    atomic_write_json(heartbeat_path, payload)
                    print(
                        f"[agent-loop] {phase.capitalize()} iteration={iteration} active "
                        f"elapsed={_elapsed(now - started)} pid={process.pid} pgid={process.pid} "
                        f"last_activity={activity.get()} changed_files={changed}",
                        flush=True,
                    )
                    next_heartbeat = now + heartbeat_seconds
                time.sleep(min(0.2, max(0.05, heartbeat_seconds / 5)))
            if process.poll() is None:
                _terminate_group(process, terminate_grace_seconds)
            return_code = process.wait()
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
        for thread in threads:
            thread.join(timeout=2)
    finally:
        stdout_handle.close()
        stderr_handle.close()

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
    }
    atomic_write_json(result_path, result)
    atomic_write_json(
        heartbeat_path,
        {
            **{key: value for key, value in result.items() if key not in {"reason", "child_exit_code"}},
            "process_state": result["state"],
            "process_group": None,
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
