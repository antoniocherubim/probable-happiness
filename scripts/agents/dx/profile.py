"""Strict, tracked per-project configuration for the external agent loop."""

from __future__ import annotations

import os
import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


PROFILE_RELATIVE_PATH = Path(".agent-loop/project.toml")
PROFILE_SCHEMA_VERSION = 1
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_SECTIONS = {
    "bootstrap": {"command", "timeout_seconds"},
    "executor": {"timeout_seconds", "heartbeat_seconds"},
    "reviewer": {"timeout_seconds", "heartbeat_seconds"},
    "environment": {"required"},
    "validation": {"commands"},
    "instructions": {"executor", "reviewer"},
    "policy": {"missing_profile", "terminate_grace_seconds"},
}
_BASE_ENV = {
    "HOME",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "NO_COLOR",
    "PATH",
    "SHELL",
    "TERM",
    "TMPDIR",
    "TZ",
    "USER",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR",
    "XDG_STATE_HOME",
}


class ProfileError(ValueError):
    """Unsafe or unsupported project integration configuration."""


@dataclass(frozen=True)
class ProjectProfile:
    path: Path | None = None
    bootstrap_command: tuple[str, ...] | None = None
    bootstrap_timeout_seconds: int = 300
    executor_timeout_seconds: int = 1800
    executor_heartbeat_seconds: int = 30
    reviewer_timeout_seconds: int = 1800
    reviewer_heartbeat_seconds: int = 30
    validation_commands: tuple[tuple[str, ...], ...] = ()
    required_environment: tuple[str, ...] = ()
    executor_instructions: tuple[str, ...] = ()
    reviewer_instructions: tuple[str, ...] = ()
    missing_profile: str = "allow"
    terminate_grace_seconds: int = 5

    def public_dict(self) -> dict[str, Any]:
        """Serializable configuration; contains names and commands, never values."""
        return {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "profile_path": str(self.path) if self.path else None,
            "bootstrap": {
                "command": list(self.bootstrap_command) if self.bootstrap_command else None,
                "timeout_seconds": self.bootstrap_timeout_seconds,
            },
            "executor": {
                "timeout_seconds": self.executor_timeout_seconds,
                "heartbeat_seconds": self.executor_heartbeat_seconds,
            },
            "reviewer": {
                "timeout_seconds": self.reviewer_timeout_seconds,
                "heartbeat_seconds": self.reviewer_heartbeat_seconds,
            },
            "environment": {"required": list(self.required_environment)},
            "validation": {"commands": [list(command) for command in self.validation_commands]},
            "instructions": {
                "executor": list(self.executor_instructions),
                "reviewer": list(self.reviewer_instructions),
            },
            "policy": {
                "missing_profile": self.missing_profile,
                "terminate_grace_seconds": self.terminate_grace_seconds,
            },
        }


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ProfileError(f"[{name}] must be a table")
    unknown = set(value) - _SAFE_SECTIONS[name]
    if unknown:
        raise ProfileError(f"unknown [{name}] key(s): {', '.join(sorted(unknown))}")
    return value


def _bounded_int(value: Any, field: str, default: int, *, low: int = 1, high: int = 86400) -> int:
    if value is None:
        return default
    if type(value) is not int or not low <= value <= high:
        raise ProfileError(f"{field} must be an integer between {low} and {high}")
    return value


def _command(value: Any, field: str, *, optional: bool = False) -> tuple[str, ...] | None:
    if value is None and optional:
        return None
    if not isinstance(value, list) or not value or len(value) > 128:
        raise ProfileError(f"{field} must be a non-empty argv array")
    if any(not isinstance(item, str) or not item or "\x00" in item for item in value):
        raise ProfileError(f"{field} contains an invalid argv item")
    return tuple(value)


def _commands(value: Any, field: str) -> tuple[tuple[str, ...], ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 32:
        raise ProfileError(f"{field} must be an array of at most 32 commands")
    return tuple(_command(item, f"{field}[{index}]") or () for index, item in enumerate(value))


def _string_list(value: Any, field: str, *, environment: bool = False) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 128:
        raise ProfileError(f"{field} must be an array of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or "\x00" in item:
            raise ProfileError(f"{field} contains an invalid value")
        if environment:
            if not _ENV_NAME.fullmatch(item):
                raise ProfileError(f"invalid environment variable name: {item!r}")
        elif Path(item).is_absolute() or ".." in Path(item).parts:
            raise ProfileError(f"{field} path must be repository-relative: {item!r}")
        if item in result:
            raise ProfileError(f"duplicate value in {field}: {item!r}")
        result.append(item)
    return tuple(result)


def load_project_profile(repo: Path | str, *, missing_policy: str = "allow") -> ProjectProfile:
    repo_path = Path(repo).resolve()
    path = repo_path / PROFILE_RELATIVE_PATH
    if not path.exists():
        if missing_policy == "deny":
            raise ProfileError(f"project profile is required: {path}")
        if missing_policy != "allow":
            raise ProfileError("missing profile policy must be 'allow' or 'deny'")
        return ProjectProfile(missing_profile=missing_policy)
    if path.is_symlink() or not path.is_file():
        raise ProfileError(f"project profile must be a regular non-symlink file: {path}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ProfileError(f"cannot parse project profile: {exc}") from exc
    if not isinstance(data, dict):
        raise ProfileError("project profile must be a TOML table")
    allowed_top = {"schema_version", *_SAFE_SECTIONS}
    unknown = set(data) - allowed_top
    if unknown:
        raise ProfileError(f"unknown top-level key(s): {', '.join(sorted(unknown))}")
    if data.get("schema_version") != PROFILE_SCHEMA_VERSION:
        raise ProfileError(f"schema_version must be {PROFILE_SCHEMA_VERSION}")

    bootstrap = _table(data, "bootstrap")
    executor = _table(data, "executor")
    reviewer = _table(data, "reviewer")
    environment = _table(data, "environment")
    validation = _table(data, "validation")
    instructions = _table(data, "instructions")
    policy = _table(data, "policy")
    configured_missing = policy.get("missing_profile", "allow")
    if configured_missing not in {"allow", "deny"}:
        raise ProfileError("policy.missing_profile must be 'allow' or 'deny'")

    return ProjectProfile(
        path=path,
        bootstrap_command=_command(bootstrap.get("command"), "bootstrap.command", optional=True),
        bootstrap_timeout_seconds=_bounded_int(
            bootstrap.get("timeout_seconds"), "bootstrap.timeout_seconds", 300
        ),
        executor_timeout_seconds=_bounded_int(
            executor.get("timeout_seconds"), "executor.timeout_seconds", 1800
        ),
        executor_heartbeat_seconds=_bounded_int(
            executor.get("heartbeat_seconds"), "executor.heartbeat_seconds", 30, high=3600
        ),
        reviewer_timeout_seconds=_bounded_int(
            reviewer.get("timeout_seconds"), "reviewer.timeout_seconds", 1800
        ),
        reviewer_heartbeat_seconds=_bounded_int(
            reviewer.get("heartbeat_seconds"), "reviewer.heartbeat_seconds", 30, high=3600
        ),
        validation_commands=_commands(validation.get("commands"), "validation.commands"),
        required_environment=_string_list(
            environment.get("required"), "environment.required", environment=True
        ),
        executor_instructions=_string_list(instructions.get("executor"), "instructions.executor"),
        reviewer_instructions=_string_list(instructions.get("reviewer"), "instructions.reviewer"),
        missing_profile=configured_missing,
        terminate_grace_seconds=_bounded_int(
            policy.get("terminate_grace_seconds"),
            "policy.terminate_grace_seconds",
            5,
            high=300,
        ),
    )


def load_environment_file(path: Path | str | None) -> dict[str, str]:
    if path is None:
        return {}
    candidate = Path(path).expanduser()
    try:
        info = candidate.lstat()
    except OSError as exc:
        raise ProfileError(f"environment file not found: {candidate}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ProfileError("environment file must be a regular non-symlink file")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ProfileError("environment file permissions must be 0600 or stricter")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ProfileError("environment file must be owned by the current user")
    values: dict[str, str] = {}
    try:
        fd = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise ProfileError("environment file changed while opening")
            if opened.st_size > 1024 * 1024:
                raise ProfileError("environment file exceeds 1 MiB")
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                lines = handle.read().splitlines()
        finally:
            if fd >= 0:
                os.close(fd)
    except (OSError, UnicodeError) as exc:
        raise ProfileError(f"cannot read environment file: {candidate}") from exc
    for line_number, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export ") or "=" not in stripped:
            raise ProfileError(f"invalid environment line {line_number}")
        name, value = stripped.split("=", 1)
        name = name.strip()
        if not _ENV_NAME.fullmatch(name) or name in values:
            raise ProfileError(f"invalid or duplicate environment name on line {line_number}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if "\x00" in value:
            raise ProfileError(f"NUL in environment value on line {line_number}")
        values[name] = value
    return values


def build_authorized_environment(
    profile: ProjectProfile,
    env_file: Path | str | None,
    environ: Mapping[str, str] | None = None,
    context: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return child env and name-only set/unset diagnostics."""
    source = dict(os.environ if environ is None else environ)
    file_values = load_environment_file(env_file)
    child = {name: source[name] for name in _BASE_ENV if name in source}
    diagnostics: dict[str, str] = {}
    secret_values: dict[str, str] = {}
    for name in profile.required_environment:
        value = file_values.get(name, source.get(name))
        if value is None or value == "":
            diagnostics[name] = "unset"
            continue
        child[name] = value
        secret_values[name] = value
        diagnostics[name] = "set"
    missing = [name for name, state in diagnostics.items() if state == "unset"]
    if missing:
        raise ProfileError(f"required environment variable(s) unset: {', '.join(missing)}")
    if context:
        for name, value in context.items():
            if not name.startswith("AGENT_LOOP_"):
                raise ProfileError(f"unsafe runtime context name: {name}")
            child[name] = value
    return child, diagnostics


_URL = re.compile(r"(?i)\b(?:postgres(?:ql)?|https?|redis|mysql)://[^\s'\"]+")


def sanitize_text(text: str, secrets: Mapping[str, str] | None = None) -> str:
    result = text.replace("\x00", "")
    for value in sorted(set((secrets or {}).values()), key=len, reverse=True):
        if value:
            result = result.replace(value, "[REDACTED]")
    return _URL.sub("[REDACTED_URL]", result)


def load_instruction_text(profile: ProjectProfile, repo: Path, phase: str) -> str:
    configured = (
        profile.executor_instructions if phase == "executor" else profile.reviewer_instructions
    )
    conventional = Path(".agent-loop") / f"{phase}.md"
    paths = list(configured)
    if (repo / conventional).is_file() and str(conventional) not in paths:
        paths.append(str(conventional))
    sections: list[str] = []
    root = repo.resolve()
    for relative in paths:
        path = root / relative
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise ProfileError(f"unsafe or missing {phase} instruction file: {relative}")
        text = path.read_text(encoding="utf-8")
        if len(text.encode("utf-8")) > 256 * 1024:
            raise ProfileError(f"instruction file too large: {relative}")
        sections.append(f"Additional tracked project instructions from {relative}:\n{text}")
    return "\n\n".join(sections)
