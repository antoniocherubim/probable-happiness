"""Portable tool, target-repository, and external state paths."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path


class PathConfigError(ValueError):
    """Invalid repository or state path."""


def canonical_repo(path: Path | str) -> Path:
    candidate = Path(path).expanduser()
    try:
        output = subprocess.check_output(
            ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
            stderr=subprocess.STDOUT,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PathConfigError(f"not a Git repository: {candidate}") from exc
    return Path(output).resolve()


def repository_id(path: Path | str) -> str:
    repo = canonical_repo(path)
    slug = re.sub(r"[^a-z0-9._-]+", "-", repo.name.lower()).strip("-") or "repo"
    digest = hashlib.sha256(os.fsencode(repo)).hexdigest()[:12]
    return f"{slug}-{digest}"


def default_state_root(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    xdg_state = (env.get("XDG_STATE_HOME") or "").strip()
    base = Path(xdg_state).expanduser() if xdg_state else Path.home() / ".local" / "state"
    return base / "codex-cursor-agent-loop"


def project_state_dir(
    repo: Path | str,
    state_root: Path | str | None = None,
) -> Path:
    root = Path(state_root).expanduser() if state_root is not None else default_state_root()
    return root.resolve() / "projects" / repository_id(repo)


def _systemd_path(path: Path | str) -> str:
    return str(Path(path).expanduser().resolve()).replace("%", "%%")


def _systemd_quoted(path: Path | str) -> str:
    value = _systemd_path(path)
    return _systemd_quote_value(value)


def _systemd_quote_value(value: str) -> str:
    value = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{value}"'


def _systemd_word(path: Path | str) -> str:
    """Escape one unquoted systemd word (needed after EnvironmentFile's '-' prefix)."""
    value = _systemd_path(path)
    escaped: list[str] = []
    for char in value:
        if char.isspace() or char in {'\\', '"', "'"}:
            escaped.extend(f"\\x{byte:02x}" for byte in char.encode("utf-8"))
        else:
            escaped.append(char)
    return "".join(escaped)


def render_systemd_unit(
    *,
    template: Path,
    tool_root: Path | str,
    state_root: Path | str,
    credentials_file: Path | str,
) -> str:
    tool = Path(tool_root).expanduser().resolve()
    values = {
        "@TOOL_ROOT@": _systemd_path(tool),
        "@BRIDGE_SCRIPT@": _systemd_quoted(tool / "scripts" / "agents" / "telegram_bridge.py"),
        "@STATE_ROOT@": _systemd_quoted(state_root),
        "@CREDENTIALS_FILE@": f"-{_systemd_word(credentials_file)}",
    }
    text = Path(template).read_text(encoding="utf-8")
    for marker, value in values.items():
        text = text.replace(marker, value)
    if re.search(r"@[A-Z_]+@", text):
        raise PathConfigError("unresolved marker in systemd template")
    return text
