"""Small fail-closed wrapper around transient ``systemd --user`` scopes."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Mapping, Sequence


class SystemdScopeError(RuntimeError):
    """A phase could not be started or completely reaped in its user scope."""


_SAFE_UNIT = re.compile(r"[^A-Za-z0-9_.@-]+")
_ACTIVE_STATES = {"activating", "active", "deactivating", "reloading"}
_BUS_ENVIRONMENT = ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")


def scope_unit_basename(run_id: str, phase: str, iteration: int) -> str:
    """Return one deterministic, bounded unit basename for a run phase."""
    raw = f"{run_id}:{phase}:{iteration}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    readable = _SAFE_UNIT.sub("-", f"{run_id}-{phase}-{iteration}").strip("-._")
    readable = readable[:120] or "phase"
    return f"agent-loop-{readable}-{digest}"


def _systemctl(
    *args: str,
    timeout: float = 10,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["systemctl", "--user", *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemdScopeError(f"systemctl --user failed: {exc}") from exc


def user_systemd_available() -> tuple[bool, str]:
    result = _systemctl("show-environment", timeout=5)
    if result.returncode == 0:
        return True, "available"
    detail = result.stderr.strip() or result.stdout.strip() or f"exit={result.returncode}"
    return False, detail[-300:]


def _unit_name(basename: str) -> str:
    return f"{basename}.scope"


def _unit_properties(basename: str) -> dict[str, str]:
    result = _systemctl(
        "show",
        _unit_name(basename),
        "--property=LoadState",
        "--property=ActiveState",
        "--property=ControlGroup",
    )
    properties: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key] = value
    if result.returncode != 0 and not properties:
        return {}
    return properties


def scope_active_state(basename: str) -> str | None:
    properties = _unit_properties(basename)
    if properties.get("LoadState") in {None, "", "not-found"}:
        return None
    return properties.get("ActiveState") or None


def scope_control_group(basename: str) -> str | None:
    properties = _unit_properties(basename)
    if properties.get("LoadState") in {None, "", "not-found"}:
        return None
    value = properties.get("ControlGroup", "")
    return value if value.startswith("/") else None


def _cgroup_path(control_group: str | None) -> Path | None:
    if not control_group:
        return None
    candidate = Path("/sys/fs/cgroup") / control_group.lstrip("/")
    try:
        candidate.resolve().relative_to(Path("/sys/fs/cgroup"))
    except (OSError, ValueError):
        return None
    return candidate


def cgroup_is_empty(control_group: str | None) -> bool:
    path = _cgroup_path(control_group)
    if path is None or not path.exists():
        return True
    events = path / "cgroup.events"
    try:
        values = dict(
            line.split(maxsplit=1)
            for line in events.read_text(encoding="utf-8").splitlines()
            if len(line.split(maxsplit=1)) == 2
        )
    except OSError:
        values = {}
    if "populated" in values:
        return values["populated"] == "0"
    try:
        return not any(
            item.strip()
            for procs in path.rglob("cgroup.procs")
            for item in procs.read_text(encoding="utf-8").splitlines()
        )
    except OSError:
        return False


def ensure_scope_reaped(
    basename: str,
    *,
    control_group: str | None = None,
    timeout_seconds: float = 5,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    known_group = control_group or scope_control_group(basename)
    while True:
        state = scope_active_state(basename)
        if state not in _ACTIVE_STATES and cgroup_is_empty(known_group):
            return
        if time.monotonic() >= deadline:
            raise SystemdScopeError(
                f"scope {_unit_name(basename)} did not become empty "
                f"(state={state!r}, cgroup={known_group!r})"
            )
        time.sleep(0.05)


def _kill_scope(basename: str, signal_name: str) -> None:
    result = _systemctl(
        "kill",
        "--kill-whom=all",
        f"--signal={signal_name}",
        _unit_name(basename),
    )
    if result.returncode != 0 and scope_active_state(basename) in _ACTIVE_STATES:
        detail = result.stderr.strip() or f"exit={result.returncode}"
        raise SystemdScopeError(
            f"cannot send {signal_name} to {_unit_name(basename)}: {detail}"
        )


def stop_user_scope(basename: str, *, grace_seconds: int) -> None:
    """Terminate every process in the scope and prove its cgroup is empty."""
    control_group = scope_control_group(basename)
    state = scope_active_state(basename)
    if state not in _ACTIVE_STATES and cgroup_is_empty(control_group):
        return

    _kill_scope(basename, "SIGTERM")
    deadline = time.monotonic() + max(0, grace_seconds)
    while not cgroup_is_empty(control_group) and time.monotonic() < deadline:
        time.sleep(0.05)
    if not cgroup_is_empty(control_group):
        _kill_scope(basename, "SIGKILL")

    result = _systemctl("stop", _unit_name(basename))
    if result.returncode != 0 and scope_active_state(basename) in _ACTIVE_STATES:
        detail = result.stderr.strip() or f"exit={result.returncode}"
        raise SystemdScopeError(
            f"cannot stop {_unit_name(basename)}: {detail}"
        )
    ensure_scope_reaped(
        basename,
        control_group=control_group,
        timeout_seconds=float(max(2, grace_seconds)),
    )


def _scoped_environment(environment: Mapping[str, str]) -> dict[str, str]:
    result = dict(environment)
    for name in _BUS_ENVIRONMENT:
        if name not in result and name in os.environ:
            result[name] = os.environ[name]
    return result


def start_scoped_popen(
    command: Sequence[str],
    *,
    basename: str,
    cwd: Path,
    environment: Mapping[str, str],
) -> subprocess.Popen[bytes]:
    """Start a command in a required user scope or fail before returning."""
    if not command:
        raise ValueError("empty command")
    available, detail = user_systemd_available()
    if not available:
        raise SystemdScopeError(
            f"refusing to run phase without systemd --user ({detail})"
        )
    if scope_active_state(basename) in _ACTIVE_STATES:
        raise SystemdScopeError(f"scope {_unit_name(basename)} is already active")
    _systemctl("reset-failed", _unit_name(basename), timeout=5)
    stale_deadline = time.monotonic() + 2
    while scope_active_state(basename) is not None:
        if time.monotonic() >= stale_deadline:
            raise SystemdScopeError(
                f"stale scope {_unit_name(basename)} was not collected"
            )
        time.sleep(0.05)

    argv = [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        f"--unit={basename}",
        "--property=KillMode=control-group",
        f"--working-directory={cwd}",
        "--",
        *command,
    ]
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=_scoped_environment(environment),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise SystemdScopeError(
            f"cannot start {_unit_name(basename)}: {exc}"
        ) from exc

    deadline = time.monotonic() + 5
    try:
        while process.poll() is None:
            if scope_active_state(basename) == "active":
                return process
            if time.monotonic() >= deadline:
                raise SystemdScopeError(
                    f"scope {_unit_name(basename)} never became active"
                )
            time.sleep(0.01)
        # A short-lived command may be collected before it is observed. A
        # successful systemd-run exit still proves the scope was created.
        return process
    except BaseException:
        try:
            stop_user_scope(basename, grace_seconds=1)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
        raise
