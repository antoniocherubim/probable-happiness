"""Telegram Bot API client (long polling only; no public webhook)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urljoin


class TelegramError(RuntimeError):
    """Bot API failure that must not mutate approval state."""


@dataclass
class FakeHttpResponse:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float | None = None,
    ) -> FakeHttpResponse:
        ...


class UrllibTransport:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float | None = None,
    ) -> FakeHttpResponse:
        req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return FakeHttpResponse(
                    status=getattr(resp, "status", 200),
                    body=resp.read(),
                    headers={k.lower(): v for k, v in resp.headers.items()},
                )
        except urllib.error.HTTPError as exc:
            return FakeHttpResponse(status=exc.code, body=exc.read() or b"")
        except urllib.error.URLError as exc:
            raise TelegramError(f"network error: {exc}") from exc


class TelegramClient:
    def __init__(
        self,
        bot_token: str,
        *,
        api_base: str = "https://api.telegram.org",
        transport: HttpTransport | None = None,
        default_timeout: float = 35.0,
    ) -> None:
        if not bot_token:
            raise TelegramError("bot token required")
        self._token = bot_token
        self._api_base = api_base.rstrip("/")
        self._transport = transport or UrllibTransport()
        self._default_timeout = default_timeout

    def _url(self, method: str) -> str:
        # Token is only used in URL path for Bot API; never logged by callers.
        return urljoin(f"{self._api_base}/", f"bot{self._token}/{method}")

    def call(self, method: str, payload: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        body = json.dumps(payload or {}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        try:
            resp = self._transport.request(
                "POST",
                self._url(method),
                headers=headers,
                body=body,
                timeout=self._default_timeout if timeout is None else timeout,
            )
        except TelegramError:
            raise
        except Exception as exc:  # transport bugs
            raise TelegramError(f"transport failure calling {method}") from exc

        if resp.status >= 500:
            raise TelegramError(f"telegram server error {resp.status} for {method}")
        try:
            data = json.loads(resp.body.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise TelegramError(f"invalid JSON from telegram for {method}") from exc
        if resp.status >= 400 or not data.get("ok"):
            description = data.get("description", resp.body[:200])
            raise TelegramError(f"telegram API error for {method}: {description}")
        return data.get("result")

    def get_updates(self, offset: int | None = None, timeout: int = 25) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        # Long poll needs timeout > Telegram's timeout parameter.
        result = self.call("getUpdates", payload, timeout=float(timeout) + 10)
        if not isinstance(result, list):
            raise TelegramError("getUpdates did not return a list")
        return result

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = self.call("sendMessage", payload)
        if not isinstance(result, dict):
            raise TelegramError("sendMessage did not return an object")
        return result

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> bool:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text is not None:
            payload["text"] = text
        return bool(self.call("answerCallbackQuery", payload))


@dataclass
class FakeTelegramAPI:
    """In-memory Bot API fake for unit tests (no network)."""

    allowed_token: str
    updates: list[dict[str, Any]] = field(default_factory=list)
    sent_messages: list[dict[str, Any]] = field(default_factory=list)
    answered_callbacks: list[dict[str, Any]] = field(default_factory=list)
    fail_methods: set[str] = field(default_factory=set)
    timeout_methods: set[str] = field(default_factory=set)
    next_update_id: int = 1

    def as_transport(self) -> HttpTransport:
        api = self

        class _Transport:
            def request(
                self,
                method: str,
                url: str,
                *,
                headers: dict[str, str] | None = None,
                body: bytes | None = None,
                timeout: float | None = None,
            ) -> FakeHttpResponse:
                if f"bot{api.allowed_token}/" not in url:
                    return FakeHttpResponse(
                        status=401,
                        body=json.dumps({"ok": False, "description": "unauthorized"}).encode(),
                    )
                api_method = url.rstrip("/").rsplit("/", 1)[-1]
                if api_method in api.timeout_methods:
                    raise TelegramError(f"timeout calling {api_method}")
                if api_method in api.fail_methods:
                    return FakeHttpResponse(
                        status=502,
                        body=json.dumps({"ok": False, "description": "upstream"}).encode(),
                    )
                payload = json.loads(body.decode("utf-8") if body else "{}")
                if api_method == "getUpdates":
                    offset = payload.get("offset")
                    batch = []
                    for update in api.updates:
                        if offset is not None and update["update_id"] < offset:
                            continue
                        batch.append(update)
                    return FakeHttpResponse(
                        status=200,
                        body=json.dumps({"ok": True, "result": batch}).encode(),
                    )
                if api_method == "sendMessage":
                    api.sent_messages.append(payload)
                    msg = {"message_id": len(api.sent_messages), "chat": {"id": payload["chat_id"]}, "text": payload["text"]}
                    return FakeHttpResponse(
                        status=200,
                        body=json.dumps({"ok": True, "result": msg}).encode(),
                    )
                if api_method == "answerCallbackQuery":
                    api.answered_callbacks.append(payload)
                    return FakeHttpResponse(
                        status=200,
                        body=json.dumps({"ok": True, "result": True}).encode(),
                    )
                return FakeHttpResponse(
                    status=404,
                    body=json.dumps({"ok": False, "description": "unknown method"}).encode(),
                )

        return _Transport()

    def push_callback(
        self,
        *,
        user_id: int,
        chat_id: int,
        data: str,
        callback_query_id: str = "cb-1",
        chat_type: str = "private",
    ) -> None:
        update = {
            "update_id": self.next_update_id,
            "callback_query": {
                "id": callback_query_id,
                "from": {"id": user_id, "username": "ignored"},
                "data": data,
                "message": {
                    "message_id": 1,
                    "chat": {"id": chat_id, "type": chat_type},
                },
            },
        }
        self.next_update_id += 1
        self.updates.append(update)

    def push_message(
        self,
        *,
        user_id: int,
        chat_id: int,
        text: str,
        chat_type: str = "private",
    ) -> None:
        update = {
            "update_id": self.next_update_id,
            "message": {
                "message_id": self.next_update_id,
                "text": text,
                "from": {"id": user_id, "username": "ignored"},
                "chat": {"id": chat_id, "type": chat_type},
            },
        }
        self.next_update_id += 1
        self.updates.append(update)
