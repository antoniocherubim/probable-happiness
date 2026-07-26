"""User systemd scopes for agent phase isolation (PS-01).

Backend is exclusively ``systemd --user``. There is no silent process-group
fallback: if a scope cannot be created, start fails closed.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Mapping, Sequence

__all__ = [
    "SystemdScopeError",
    "cgroup_is_empty",
    "cgroup_pids",
    "ensure_scope_reaped",
    "scope_active_state",
    "scope_control_group",
    "scope_unit_basename",
    "start_scoped_popen",
    "stop_user_scope",
    "user_systemd_available",
    "wait_scope_inactive",
]


class SystemdScopeError(RuntimeError):
    """Raised when a user scope cannot be created, stopped, or verified empty."""


_UNSAFE_UNIT_RE = re.compile(r"[^A-Za-z0-9:_.@-]+")
_UNIT_MAX = 200  # leave room for ".scope" under systemd's 255 limit
# Positive evidence that systemd-run never created the transient scope.
# Absence of a unit after --collect is NOT enough: /bin/false (and similar
# ultrashort nonzero commands) race with collection the same way.
_CREATION_FAILURE_RE = re.compile(
    r"Failed to start transient|"
    r"Failed to create transient|"
    r"Failed to allocate|"
    r"Failed connecting to bus|"
    r"Failed to connect to bus|"
    r"Failed to get|"
    r"Unit name .* is not valid|"
    r"Interactive authentication required",
    re.IGNORECASE,
)


def _user_bus_environment() -> dict[str, str]:
    """Minimal env so ``systemd-run --user`` can reach the session bus."""
    env: dict[str, str] = {}
    for key in (
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "XDG_SESSION_ID",
        "XDG_SESSION_TYPE",
        "XDG_SESSION_CLASS",
        "XDG_SESSION_DESKTOP",
    ):
        value = os.environ.get(key)
        if value:
            env[key] = value
    runtime_dir = env.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        candidate = f"/run/user/{os.getuid()}"
        if Path(candidate).exists():
            env["XDG_RUNTIME_DIR"] = candidate
            runtime_dir = candidate
    if runtime_dir and "DBUS_SESSION_BUS_ADDRESS" not in env:
        bus = Path(runtime_dir) / "bus"
        if bus.exists():
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    return env


def scope_unit_basename(run_id: str, phase: str, iteration: int) -> str:
    """Return a sensitive-free unit basename (without ``.scope``)."""
    raw = f"agent-loop-{run_id}-{phase}-{int(iteration)}"
    cleaned = _UNSAFE_UNIT_RE.sub("-", raw)
    cleaned = cleaned.replace("..", ".")
    while ".." in cleaned:
        cleaned = cleaned.replace("..", ".")
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-._")
    if not cleaned:
        cleaned = "agent-loop-phase"
    if len(cleaned) > _UNIT_MAX:
        digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:12]
        cleaned = f"{cleaned[: _UNIT_MAX - 13]}-{digest}"
    return cleaned


def _unit_name(basename: str) -> str:
    return basename if basename.endswith(".scope") else f"{basename}.scope"


def user_systemd_available() -> tuple[bool, str]:
    """Return ``(ok, detail)`` for the caller’s user systemd manager."""
    env = {**os.environ, **_user_bus_environment()}
    try:
        completed = subprocess.run(
            ["systemctl", "--user", "is-system-running"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    detail = (completed.stdout or completed.stderr or "").strip() or f"exit={completed.returncode}"
    # "running" and "degraded" still accept transient scopes on a personal host.
    if detail in {"running", "degraded"} or completed.returncode == 0:
        return True, detail if detail in {"running", "degraded"} else (
            detail if detail else "running"
        )
    return False, detail


def _systemctl_user(*args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env={**os.environ, **_user_bus_environment()},
    )


def _parse_systemctl_show(stdout: str) -> dict[str, str]:
    props: dict[str, str] = {}
    for line in (stdout or "").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        props[key] = value
    return props


def _show_unit_properties(basename: str, *properties: str) -> dict[str, str]:
    """Query unit properties. Raises on bus/tool failure; missing unit is ok."""
    unit = _unit_name(basename)
    args: list[str] = ["show", unit]
    for prop in properties:
        args.extend(["-p", prop])
    try:
        completed = _systemctl_user(*args, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemdScopeError(f"failed to query scope {unit}: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip() or f"exit={completed.returncode}"
        raise SystemdScopeError(f"failed to query scope {unit}: {detail}")
    return _parse_systemctl_show(completed.stdout)


def scope_active_state(basename: str) -> str | None:
    """Return ActiveState, or ``None`` when the unit is not loaded.

    Query failures (bus loss, timeout, non-zero systemctl) raise
    ``SystemdScopeError`` instead of looking like a missing unit.
    """
    props = _show_unit_properties(basename, "ActiveState", "LoadState")
    if "LoadState" not in props:
        raise SystemdScopeError(
            f"incomplete systemctl show for {_unit_name(basename)}: missing LoadState"
        )
    load_state = props["LoadState"].strip()
    if load_state == "not-found":
        return None
    if "ActiveState" not in props:
        raise SystemdScopeError(
            f"incomplete systemctl show for {_unit_name(basename)}: missing ActiveState"
        )
    active_state = props["ActiveState"].strip()
    if not active_state or active_state == "n/a":
        raise SystemdScopeError(
            f"incomplete systemctl show for {_unit_name(basename)}: invalid ActiveState"
        )
    return active_state


def scope_control_group(basename: str) -> str | None:
    """Return the unit ControlGroup path, or ``None`` if missing/unloaded.

    Query failures raise ``SystemdScopeError``.
    """
    props = _show_unit_properties(basename, "ControlGroup", "LoadState")
    if "LoadState" not in props:
        raise SystemdScopeError(
            f"incomplete systemctl show for {_unit_name(basename)}: missing LoadState"
        )
    if props["LoadState"].strip() == "not-found":
        return None
    if "ControlGroup" not in props:
        raise SystemdScopeError(
            f"incomplete systemctl show for {_unit_name(basename)}: missing ControlGroup"
        )
    value = props["ControlGroup"].strip()
    if not value:
        raise SystemdScopeError(
            f"incomplete systemctl show for {_unit_name(basename)}: empty ControlGroup"
        )
    return value


def _cgroup_dir(control_group: str) -> Path | None:
    if not control_group or control_group == "/":
        return None
    relative = control_group.lstrip("/")
    for root in (Path("/sys/fs/cgroup"), Path("/sys/fs/cgroup/unified")):
        candidate = root / relative
        if candidate.is_dir():
            return candidate
    return None


def _read_cgroup_populated(cgroup_dir: Path) -> bool:
    """Return True when the cgroup tree still has processes (recursive)."""
    events = cgroup_dir / "cgroup.events"
    try:
        text = events.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemdScopeError(f"cannot read {events}: {exc}") from exc
    for line in text.splitlines():
        if line.startswith("populated "):
            return line.split()[1] != "0"
    raise SystemdScopeError(f"missing populated field in {events}")


def cgroup_pids(control_group: str) -> list[int]:
    """Collect PIDs from ``control_group`` and nested child cgroups.

    A fully absent cgroup directory yields ``[]``. An existing but unreadable
    ``cgroup.procs`` raises ``SystemdScopeError``.
    """
    root = _cgroup_dir(control_group)
    if root is None:
        return []
    pids: list[int] = []
    try:
        dirs = [root, *sorted(path for path in root.rglob("*") if path.is_dir())]
    except OSError as exc:
        raise SystemdScopeError(f"cannot walk cgroup {control_group}: {exc}") from exc
    for directory in dirs:
        procs = directory / "cgroup.procs"
        if not procs.exists():
            continue
        try:
            text = procs.read_text(encoding="utf-8")
        except OSError as exc:
            raise SystemdScopeError(f"cannot read {procs}: {exc}") from exc
        for line in text.splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
    return pids


def cgroup_is_empty(control_group: str | None) -> bool:
    """Return True when the unit cgroup is gone or recursively unpopulated.

    Uses ``cgroup.events`` ``populated`` so nested child cgroups count.
    Unreadable cgroup state raises ``SystemdScopeError`` (fail closed).
    """
    if not control_group:
        return True
    cgroup_dir = _cgroup_dir(control_group)
    if cgroup_dir is None:
        # Path already torn down after the unit left the manager.
        return True
    return not _read_cgroup_populated(cgroup_dir)


def wait_scope_inactive(basename: str, *, timeout_seconds: float) -> None:
    unit = _unit_name(basename)
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    while time.monotonic() < deadline:
        state = scope_active_state(basename)
        if state is None or state in {"inactive", "failed"}:
            return
        time.sleep(0.05)
    state = scope_active_state(basename)
    raise SystemdScopeError(f"scope {unit} still {state!r} after stop wait")


def ensure_scope_reaped(basename: str, *, timeout_seconds: float = 5.0) -> None:
    """Wait until inactive/missing and confirm the unit cgroup has no PIDs."""
    wait_scope_inactive(basename, timeout_seconds=timeout_seconds)
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    last_cg: str | None = None
    while time.monotonic() < deadline:
        last_cg = scope_control_group(basename)
        if cgroup_is_empty(last_cg):
            return
        time.sleep(0.05)
    remaining = cgroup_pids(last_cg) if last_cg else []
    raise SystemdScopeError(
        f"scope {_unit_name(basename)} cgroup not empty after stop: pids={remaining}"
    )


def stop_user_scope(basename: str, *, grace_seconds: int) -> None:
    """Stop the scope, wait until inactive, and require an empty cgroup."""
    unit = _unit_name(basename)
    state = scope_active_state(basename)
    if state is None:
        # Unit already gone — still confirm any reported cgroup is empty.
        ensure_scope_reaped(basename, timeout_seconds=float(max(1, grace_seconds)))
        return
    try:
        completed = _systemctl_user("stop", unit, timeout=max(5, grace_seconds + 5))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemdScopeError(f"failed to stop scope {unit}: {exc}") from exc
    if completed.returncode != 0:
        # Race: unit disappeared between show and stop — verify emptiness.
        after = scope_active_state(basename)
        if after is not None and after not in {"inactive", "failed"}:
            detail = (completed.stderr or completed.stdout or "").strip() or f"exit={completed.returncode}"
            raise SystemdScopeError(f"failed to stop scope {unit}: {detail}")
    # If stop did not clear quickly, escalate with SIGKILL on remaining cgroup PIDs.
    try:
        wait_scope_inactive(basename, timeout_seconds=float(grace_seconds))
    except SystemdScopeError:
        cg = scope_control_group(basename)
        for pid in cgroup_pids(cg or ""):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                continue
        try:
            _systemctl_user("stop", unit, timeout=max(5, grace_seconds + 5))
        except (OSError, subprocess.TimeoutExpired):
            pass
        wait_scope_inactive(basename, timeout_seconds=float(max(1, grace_seconds)))
    ensure_scope_reaped(basename, timeout_seconds=float(max(1, grace_seconds)))


def _read_stderr_bytes(process: subprocess.Popen[bytes]) -> bytes:
    if process.stderr is None:
        return b""
    try:
        return process.stderr.read() or b""
    except OSError:
        return b""


def _close_process_pipes(process: subprocess.Popen[bytes]) -> None:
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _cleanup_failed_start(basename: str, process: subprocess.Popen[bytes]) -> None:
    """Reap wrapper + scope after a post-Popen failure before returning to caller.

    Must not depend solely on ``_show_unit_properties``: that helper may be the
    fault that triggered cleanup. Prefer a direct ``systemctl stop``, then kill
    the wrapper session and any remaining cgroup PIDs.
    """
    unit = _unit_name(basename)
    try:
        _systemctl_user("stop", unit, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        stop_user_scope(basename, grace_seconds=2)
    except SystemdScopeError:
        pass
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            pass
    try:
        cg = scope_control_group(basename)
    except SystemdScopeError:
        cg = None
    if cg:
        try:
            for pid in cgroup_pids(cg):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    continue
        except SystemdScopeError:
            pass
    try:
        ensure_scope_reaped(basename, timeout_seconds=2.0)
    except SystemdScopeError:
        pass
    _close_process_pipes(process)


def _scope_creation_failed(
    basename: str,
    process: subprocess.Popen[bytes],
    *,
    stderr_hint: str,
) -> bool:
    """True only with positive evidence the transient scope never started.

    A fast legitimate nonzero command (e.g. ``/bin/false``) may exit and have
    ``--collect`` remove the unit before ActiveState is observed. That race must
    remain an ordinary command failure, not ``SystemdScopeError``.
    """
    if process.returncode == 0:
        return False
    state = scope_active_state(basename)
    if state is not None:
        return False
    props = _show_unit_properties(basename, "LoadState", "Result", "ActiveState")
    if props.get("LoadState") not in {"", "not-found"}:
        return False
    if stderr_hint and _CREATION_FAILURE_RE.search(stderr_hint):
        return True
    return False


def start_scoped_popen(
    command: Sequence[str],
    *,
    basename: str,
    cwd: Path,
    environment: Mapping[str, str],
) -> subprocess.Popen[bytes]:
    """Start ``command`` inside a new user scope. Refuse if the scope cannot start."""
    if not command:
        raise ValueError("empty command")
    ok, detail = user_systemd_available()
    if not ok:
        raise SystemdScopeError(
            f"refusing to start phase without systemd --user scope ({detail})"
        )
    unit = _unit_name(basename)
    # Clear a leftover failed unit with the same name from a prior crash.
    try:
        _systemctl_user("reset-failed", unit, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass
    if scope_active_state(basename) == "active":
        raise SystemdScopeError(f"scope {unit} already active")

    systemd_run = [
        "systemd-run",
        "--user",
        "--scope",
        f"--unit={basename}",
        "--collect",
        "--property=Delegate=yes",
        "--quiet",
        f"--working-directory={cwd}",
        "--",
        *list(command),
    ]
    # Preserve session-bus endpoints for systemd-run while keeping the caller's
    # authorized environment for the scoped command (no secret expansion).
    child_env = {**dict(environment), **_user_bus_environment()}
    if "PATH" not in child_env and "PATH" in os.environ:
        child_env["PATH"] = os.environ["PATH"]
    try:
        process = subprocess.Popen(
            systemd_run,
            cwd=str(cwd),
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise SystemdScopeError(f"failed to invoke systemd-run for {unit}: {exc}") from exc

    # Own cleanup until a live Popen is returned: supervise_command only reaps
    # after start_scoped_popen succeeds. Any post-spawn exception must stop the
    # scope and kill the wrapper here.
    try:
        deadline = time.monotonic() + 5.0
        saw_unit = False
        while True:
            props = _show_unit_properties(basename, "ActiveState", "LoadState")
            load_state = props.get("LoadState", "")
            state = (props.get("ActiveState") or "").strip()
            if load_state and load_state != "not-found":
                saw_unit = True
            if state == "active":
                return process
            if process.poll() is not None:
                if saw_unit:
                    return process
                # Brief settle for --collect races after a real short-lived command.
                settle_deadline = time.monotonic() + 0.2
                while time.monotonic() < settle_deadline:
                    settle = _show_unit_properties(basename, "ActiveState", "LoadState")
                    if settle.get("LoadState") not in {"", "not-found"}:
                        return process
                    if (settle.get("ActiveState") or "").strip() in {
                        "active",
                        "inactive",
                        "failed",
                    }:
                        if settle.get("LoadState") != "not-found":
                            return process
                    time.sleep(0.01)
                # Only consume stderr when discriminating creation failure from a
                # collected ultrashort command; restore bytes for the caller.
                err_bytes = _read_stderr_bytes(process)
                stderr_hint = err_bytes.decode("utf-8", errors="replace").strip()
                if process.returncode != 0 and _scope_creation_failed(
                    basename, process, stderr_hint=stderr_hint
                ):
                    _close_process_pipes(process)
                    detail = stderr_hint or f"exit={process.returncode}"
                    raise SystemdScopeError(
                        f"refusing to start phase without systemd scope {unit} ({detail})"
                    )
                if process.stderr is not None:
                    try:
                        process.stderr.close()
                    except OSError:
                        pass
                    # process already exited — no further pipe traffic.
                    process.stderr = io.BytesIO(err_bytes)  # type: ignore[assignment]
                return process
            if time.monotonic() >= deadline:
                break
            time.sleep(0.005)

        # Child still running but the scope never became active — fail closed.
        raise SystemdScopeError(
            f"refusing to start phase without systemd scope {unit} "
            f"(never became active, exit={process.poll()})"
        )
    except BaseException:
        _cleanup_failed_start(basename, process)
        raise
