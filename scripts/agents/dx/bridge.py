"""Long-polling Telegram bridge for agent-loop notifications and human approval."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .approval import (
    apply_human_approval,
    apply_human_rejection,
    find_run_dir_by_token,
    list_pending_notifications,
    mark_notification_message_sent,
    mark_notification_sent,
    truncate_message,
)
from .config import BridgeConfig
from .delivery_job import callback_delivery_message, ensure_delivery_job
from .telegram import TelegramClient, TelegramError

logger = logging.getLogger("agent_dx.bridge")

NEUTRAL_UNAUTHORIZED = "OK."
NEUTRAL_UNSUPPORTED = "Only the approval button is supported."
NEUTRAL_GROUP = "OK."


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
        self._offset: int | None = None

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
            reply_markup = None
            # Only the final chunk offers a decision; blocked/failure never do.
            if (
                index == len(messages) - 1
                and kind == "awaiting_human_approval"
                and payload.get("offer_approval_button")
                and payload.get("callback_token")
            ):
                token = str(payload["callback_token"])
                reply_markup = {
                    "inline_keyboard": [
                        [
                            {
                                "text": "Aprovar e publicar branch",
                                "callback_data": token[:64],
                            },
                            {
                                "text": "Rejeitar",
                                "callback_data": f"reject:{token}"[:64],
                            },
                        ]
                    ]
                }
            result = self.client.send_message(
                self.config.allowed_chat_id,
                messages[index],
                reply_markup=reply_markup,
            )
            message_id = result.get("message_id")
            if type(message_id) is not int:
                raise TelegramError("sendMessage returned no integer message_id")
            if not mark_notification_message_sent(run_dir, notification_id, message_id):
                logger.info("notification replaced while sending chunks for %s", run_dir.name)
                return False
        return True

    def process_updates_once(self) -> int:
        try:
            updates = self.client.get_updates(
                offset=self._offset,
                timeout=self.config.poll_timeout_sec,
            )
        except TelegramError as exc:
            logger.warning("getUpdates failed: %s", exc)
            return 0

        handled = 0
        for update in updates:
            update_id = int(update["update_id"])
            self._offset = update_id + 1
            try:
                self._handle_update(update)
            except TelegramError as exc:
                logger.warning("update handling telegram error: %s", exc)
            except Exception:
                logger.exception("update handling failed")
            handled += 1
        return handled

    def _handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
            return
        if "message" in update:
            self._handle_message(update["message"])

    def _handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = chat.get("id")
        user_id = sender.get("id")
        chat_type = chat.get("type")

        if chat_type != "private":
            # Ignore groups/channels; optional neutral ack only to allowlisted private chats.
            return

        if user_id != self.config.allowed_user_id or chat_id != self.config.allowed_chat_id:
            # Neutral response — no paths, tasks, logs, or host state.
            try:
                self.client.send_message(int(chat_id), NEUTRAL_UNAUTHORIZED)
            except (TypeError, TelegramError):
                pass
            return

        # Authorized operator: still no free-text command execution surface.
        self.client.send_message(int(chat_id), NEUTRAL_UNSUPPORTED)

    def _handle_callback(self, callback: dict[str, Any]) -> None:
        callback_id = str(callback.get("id") or "")
        data = str(callback.get("data") or "")
        sender = callback.get("from") or {}
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        user_id = sender.get("id")
        chat_id = chat.get("id")
        chat_type = chat.get("type")

        def answer(text: str | None = None) -> None:
            if callback_id:
                try:
                    self.client.answer_callback_query(callback_id, text=text)
                except TelegramError as exc:
                    logger.warning("answerCallbackQuery failed: %s", exc)

        if chat_type != "private":
            answer(NEUTRAL_GROUP)
            return

        if user_id != self.config.allowed_user_id or chat_id != self.config.allowed_chat_id:
            answer(NEUTRAL_UNAUTHORIZED)
            return

        rejecting = data.startswith("reject:")
        token = data.removeprefix("reject:") if rejecting else data
        run_dir = find_run_dir_by_token(self.runs_root, token)
        if run_dir is None:
            answer("Unknown or expired approval.")
            return

        if rejecting:
            result = apply_human_rejection(
                run_dir=run_dir,
                callback_token=token,
                telegram_user_id=int(user_id),
                telegram_chat_id=int(chat_id),
                allowed_user_id=self.config.allowed_user_id,
                allowed_chat_id=self.config.allowed_chat_id,
            )
            if result == "rejected":
                answer("Rejeitado. Nenhuma branch foi publicada.")
            elif result == "rejected_unauthorized":
                answer(NEUTRAL_UNAUTHORIZED)
            else:
                answer("Rejection not applicable.")
            return

        result, _decision = apply_human_approval(
            run_dir=run_dir,
            callback_token=token,
            telegram_user_id=int(user_id),
            telegram_chat_id=int(chat_id),
            allowed_user_id=self.config.allowed_user_id,
            allowed_chat_id=self.config.allowed_chat_id,
        )

        if result in {"accepted", "idempotent_replay"}:
            # Ensure job under approval semantics; never call Git from the bridge.
            try:
                ensure_delivery_job(run_dir)
            except Exception as exc:
                logger.warning("delivery job ensure failed for %s: %s", run_dir.name, exc)
            answer(callback_delivery_message(run_dir))
            return
        if result == "rejected_unauthorized":
            answer(NEUTRAL_UNAUTHORIZED)
            return
        answer("Approval not applicable.")

    def run_forever(self, *, max_cycles: int | None = None) -> None:
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            self.process_outbox_once()
            self.process_updates_once()
            cycles += 1


def build_awaiting_summary(task_id: str, review_report: str) -> str:
    return truncate_message(
        f"Technical review APPROVED for {task_id}. "
        f"Human gate pending. Review file: {Path(review_report).name}"
    )


def build_blocked_summary(reason: str, report_hint: str = "") -> str:
    parts = ["Loop BLOCKED.", reason.strip()]
    if report_hint:
        parts.append(f"See {Path(report_hint).name}")
    return truncate_message(" ".join(p for p in parts if p))
