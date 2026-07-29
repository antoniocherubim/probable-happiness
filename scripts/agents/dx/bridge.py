"""Outbound-only Telegram notifier for terminal agent-loop messages."""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .approval import (
    list_pending_notifications,
    mark_notification_message_sent,
    mark_notification_sent,
    truncate_message,
)
from .config import BridgeConfig
from .telegram import TelegramClient, TelegramError

logger = logging.getLogger("agent_dx.bridge")

BRIDGE_ALREADY_RUNNING_EXIT = 73
OUTBOX_POLL_INTERVAL_SEC = 1.0


class BridgeAlreadyRunning(RuntimeError):
    """Another local notifier already owns delivery for this bot."""


def _bridge_runtime_dir(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    configured = (env.get("XDG_RUNTIME_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser()
    if hasattr(os, "getuid"):
        conventional = Path("/run/user") / str(os.getuid())
        if conventional.is_dir():
            return conventional
        return Path(tempfile.gettempdir()) / f"codex-cursor-agent-loop-{os.getuid()}"
    return Path(tempfile.gettempdir()) / "codex-cursor-agent-loop"


@contextmanager
def bridge_instance_lock(
    config: BridgeConfig,
    *,
    environ: dict[str, str] | None = None,
) -> Iterator[Path]:
    """
    Allow only one notifier per Telegram bot on this host.

    The lock intentionally lives outside ``runs_root``: two bridges pointed at
    different state roots could otherwise duplicate or inconsistently mark
    outbound deliveries.
    """
    runtime_root = _bridge_runtime_dir(environ)
    lock_dir = runtime_root / "codex-cursor-agent-loop"
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory_info = lock_dir.lstat()
    if (
        stat.S_ISLNK(directory_info.st_mode)
        or not stat.S_ISDIR(directory_info.st_mode)
        or (hasattr(os, "getuid") and directory_info.st_uid != os.getuid())
        or directory_info.st_mode & 0o077
    ):
        raise OSError(f"unsafe Telegram bridge lock directory: {lock_dir}")

    bot_id = hashlib.sha256(config.bot_token.encode("utf-8")).hexdigest()[:24]
    lock_path = lock_dir / f"telegram-{bot_id}.lock"
    fd = os.open(
        str(lock_path),
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        file_info = os.fstat(fd)
        if (
            not stat.S_ISREG(file_info.st_mode)
            or (hasattr(os, "getuid") and file_info.st_uid != os.getuid())
        ):
            raise OSError(f"unsafe Telegram bridge lock file: {lock_path}")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BridgeAlreadyRunning(
                "another agent-loop notifier is already using this Telegram bot; "
                "use one bridge for the shared state root"
            ) from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()}\n".encode("ascii"))
        os.fsync(fd)
        yield lock_path
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class Bridge:
    def __init__(
        self,
        config: BridgeConfig,
        client: TelegramClient,
        runs_root: Path,
    ) -> None:
        self.config = config
        self.client = client
        self.runs_root = Path(runs_root)

    def process_outbox_once(self) -> int:
        """Send pending notifications. Telegram failures leave outbox unsent."""
        sent = 0
        for run_dir, payload in list_pending_notifications(self.runs_root):
            try:
                completed = self._send_notification(run_dir, payload)
            except TelegramError as exc:
                logger.warning("telegram notify failed for %s: %s", run_dir.name, exc)
                continue
            except Exception:
                logger.exception("unexpected notify failure for %s", run_dir.name)
                continue
            if not completed:
                continue
            try:
                marked = mark_notification_sent(
                    run_dir,
                    str(payload.get("notification_id") or ""),
                )
            except Exception:
                logger.exception("failed to mark notify sent for %s", run_dir.name)
                continue
            if not marked:
                logger.info("notification replaced while sending for %s", run_dir.name)
                continue
            sent += 1
        return sent

    def _send_notification(self, run_dir: Path, payload: dict[str, Any]) -> bool:
        kind = payload.get("kind")
        run_id = payload.get("run_id", run_dir.name)
        summary = truncate_message(str(payload.get("summary") or kind or "update"))
        report_hint = str(payload.get("report_hint") or "")
        # Never include credentials, env, or full host logs.
        lines = [
            f"Agent loop: {kind}",
            f"run: {run_id}",
        ]
        if payload.get("task_id"):
            lines.append(f"task: {payload['task_id']}")
        if report_hint:
            lines.append(f"report: {report_hint}")
        lines.append(summary)
        legacy_text = truncate_message("\n".join(lines))
        configured = payload.get("messages")
        messages = (
            [truncate_message(str(item)) for item in configured]
            if isinstance(configured, list) and configured
            else [legacy_text]
        )
        sent_ids = payload.get("sent_message_ids")
        sent_count = len(sent_ids) if isinstance(sent_ids, list) else 0
        if sent_count > len(messages):
            logger.warning("invalid Telegram chunk cursor for %s", run_dir.name)
            return False

        notification_id = str(payload.get("notification_id") or "")
        for index in range(sent_count, len(messages)):
            result = self.client.send_message(
                self.config.allowed_chat_id,
                messages[index],
            )
            message_id = result.get("message_id")
            if type(message_id) is not int:
                raise TelegramError("sendMessage returned no integer message_id")
            if not mark_notification_message_sent(run_dir, notification_id, message_id):
                logger.info("notification replaced while sending chunks for %s", run_dir.name)
                return False
        return True

    def run_forever(self, *, max_cycles: int | None = None) -> None:
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            self.process_outbox_once()
            cycles += 1
            if max_cycles is None or cycles < max_cycles:
                time.sleep(OUTBOX_POLL_INTERVAL_SEC)


def build_approved_summary(task_id: str, review_report: str) -> str:
    return truncate_message(
        f"Technical review APPROVED for {task_id}. "
        f"Run completed; integration remains manual. "
        f"Review file: {Path(review_report).name}"
    )


def build_blocked_summary(reason: str, report_hint: str = "") -> str:
    parts = ["Loop BLOCKED.", reason.strip()]
    if report_hint:
        parts.append(f"See {Path(report_hint).name}")
    return truncate_message(" ".join(p for p in parts if p))
