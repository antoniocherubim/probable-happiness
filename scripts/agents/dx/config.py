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
    allowed_chat_id: int
    api_base: str = "https://api.telegram.org"
    runs_root: Path | None = None

    def redacted(self) -> dict[str, object]:
        """Safe view for logs/tests — never includes the real token."""
        token = self.bot_token
        hint = f"{token[:4]}…{token[-2:]}" if len(token) > 8 else "(set)"
        return {
            "bot_token_hint": hint,
            "allowed_chat_id": self.allowed_chat_id,
            "api_base": self.api_base,
        }


def load_bridge_config(
    environ: dict[str, str] | None = None,
    *,
    require_token: bool = True,
) -> BridgeConfig:
    """
    Load bot token + destination chat from environment and a credential file.

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
    chat_raw = get("AGENT_TELEGRAM_ALLOWED_CHAT_ID")
    api_base = get("AGENT_TELEGRAM_API_BASE") or "https://api.telegram.org"

    if require_token and not token:
        raise ConfigError("AGENT_TELEGRAM_BOT_TOKEN is required")
    if not chat_raw:
        raise ConfigError("AGENT_TELEGRAM_ALLOWED_CHAT_ID is required")

    runs_root_raw = get("AGENT_RUNS_ROOT")
    runs_root = Path(runs_root_raw).expanduser() if runs_root_raw else None

    return BridgeConfig(
        bot_token=token or "unused-in-tests",
        allowed_chat_id=_parse_positive_int(chat_raw, "AGENT_TELEGRAM_ALLOWED_CHAT_ID"),
        api_base=api_base.rstrip("/"),
        runs_root=runs_root,
    )
