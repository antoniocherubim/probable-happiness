"""Configuration for the local Telegram bridge (env / external credential file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    """Invalid or incomplete bridge configuration."""


def _parse_positive_int(raw: str, field: str) -> int:
    raw = raw.strip()
    if not raw.isdigit():
        raise ConfigError(f"{field} must be a numeric id, got {raw!r}")
    value = int(raw)
    if value <= 0:
        raise ConfigError(f"{field} must be a positive integer")
    return value


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    text = path.read_text(encoding="utf-8")
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise ConfigError(f"invalid credential line {line_no} in {path}")
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if not key:
            raise ConfigError(f"empty key on line {line_no} in {path}")
        values[key] = value
    return values


@dataclass(frozen=True)
class BridgeConfig:
    bot_token: str
    allowed_user_id: int
    allowed_chat_id: int
    api_base: str = "https://api.telegram.org"
    poll_timeout_sec: int = 25
    runs_root: Path | None = None

    def redacted(self) -> dict[str, object]:
        """Safe view for logs/tests — never includes the real token."""
        token = self.bot_token
        hint = f"{token[:4]}…{token[-2:]}" if len(token) > 8 else "(set)"
        return {
            "bot_token_hint": hint,
            "allowed_user_id": self.allowed_user_id,
            "allowed_chat_id": self.allowed_chat_id,
            "api_base": self.api_base,
            "poll_timeout_sec": self.poll_timeout_sec,
        }


def load_bridge_config(
    environ: dict[str, str] | None = None,
    *,
    require_token: bool = True,
) -> BridgeConfig:
    """
    Load token + numeric allowlist from environment and optional credential file.

    Credential file path: AGENT_TELEGRAM_CREDENTIALS_FILE (outside Git).
    File values fill gaps; process environment wins on conflicts.
    """
    env = dict(os.environ if environ is None else environ)
    file_path = env.get("AGENT_TELEGRAM_CREDENTIALS_FILE", "").strip()
    file_values: dict[str, str] = {}
    if file_path:
        path = Path(file_path).expanduser()
        if not path.is_file():
            raise ConfigError(f"credential file not found: {path}")
        file_values = _load_env_file(path)

    def get(name: str) -> str:
        return (env.get(name) or file_values.get(name) or "").strip()

    token = get("AGENT_TELEGRAM_BOT_TOKEN")
    user_raw = get("AGENT_TELEGRAM_ALLOWED_USER_ID")
    chat_raw = get("AGENT_TELEGRAM_ALLOWED_CHAT_ID")
    api_base = get("AGENT_TELEGRAM_API_BASE") or "https://api.telegram.org"
    poll_raw = get("AGENT_TELEGRAM_POLL_TIMEOUT_SEC") or "25"

    if require_token and not token:
        raise ConfigError("AGENT_TELEGRAM_BOT_TOKEN is required")
    if not user_raw or not chat_raw:
        raise ConfigError(
            "AGENT_TELEGRAM_ALLOWED_USER_ID and AGENT_TELEGRAM_ALLOWED_CHAT_ID are required"
        )

    try:
        poll_timeout = int(poll_raw)
    except ValueError as exc:
        raise ConfigError("AGENT_TELEGRAM_POLL_TIMEOUT_SEC must be an integer") from exc
    if poll_timeout < 1 or poll_timeout > 50:
        raise ConfigError("AGENT_TELEGRAM_POLL_TIMEOUT_SEC must be between 1 and 50")

    runs_root_raw = get("AGENT_RUNS_ROOT")
    runs_root = Path(runs_root_raw).expanduser() if runs_root_raw else None

    return BridgeConfig(
        bot_token=token or "unused-in-tests",
        allowed_user_id=_parse_positive_int(user_raw, "AGENT_TELEGRAM_ALLOWED_USER_ID"),
        allowed_chat_id=_parse_positive_int(chat_raw, "AGENT_TELEGRAM_ALLOWED_CHAT_ID"),
        api_base=api_base.rstrip("/"),
        poll_timeout_sec=poll_timeout,
        runs_root=runs_root,
    )


def human_approval_timeout_sec(environ: dict[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    raw = (env.get("AGENT_HUMAN_APPROVAL_TIMEOUT_SEC") or "3600").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError("AGENT_HUMAN_APPROVAL_TIMEOUT_SEC must be an integer") from exc
    if value < 1:
        raise ConfigError("AGENT_HUMAN_APPROVAL_TIMEOUT_SEC must be >= 1")
    return value
