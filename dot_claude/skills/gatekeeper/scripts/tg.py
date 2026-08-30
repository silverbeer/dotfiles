"""Telegram Bot API over stdlib urllib, narrowed to what a gate needs (SB-508).

The shape is ported from trd's notify/bot.py: a small Transport protocol, one
real implementation with no dependencies, and a fake in the tests so the suite
never touches a socket. The trading-specific parts stay in trd.

Long polling, not a webhook: the gatekeeper runs on a Mac behind NAT, so it
fetches with `getUpdates` rather than exposing an endpoint.

Two things this module refuses to do:

* Retry a 409. Two pollers on one token is a deployment mistake (the trd bot
  and this one must have SEPARATE tokens), never weather. It is raised by name
  as TelegramConflict so the caller crashes visibly.
* Put the token anywhere but the URL. Errors carry the method and the HTTP
  code, never `exc.url`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Protocol

API_ROOT = "https://api.telegram.org"

# Telegram rejects anything longer outright; chunking beats a dropped message.
MAX_MESSAGE = 4096

# Telegram caps callback_data at 64 bytes, so `gate_id:verb` needs a short id.
# 8 hex chars + ":approve" is 16 — comfortably inside.
MAX_CALLBACK_DATA = 64

VERBS = ("approve", "reject", "note")


class TelegramError(Exception):
    """Telegram refused or could not be reached. The message never carries the token."""


class TelegramConflict(TelegramError):
    """HTTP 409: another process is polling this bot token. Never retried."""


class Transport(Protocol):
    """The four Bot API calls the gatekeeper needs. Tests hand in a fake."""

    def send_message(self, chat_id: str, text: str, reply_markup: dict | None = None) -> dict: ...

    def get_updates(self, offset: int, timeout: int, allowed_updates: list[str]) -> list[dict[str, Any]]: ...

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None: ...

    def get_me(self) -> dict: ...


class TelegramTransport:
    def __init__(self, token: str, api_root: str = API_ROOT) -> None:
        self.token = token
        self.api_root = api_root

    def _call(self, method: str, payload: dict[str, Any], timeout: int) -> Any:
        request = urllib.request.Request(
            f"{self.api_root}/bot{self.token}/{method}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            # The token is in the URL — never include exc.url or str(exc).
            if exc.code == 409:
                raise TelegramConflict(
                    "Telegram 409 Conflict: another process is polling this bot token. "
                    "Only one poller may run — the gatekeeper needs its own bot, not trd's."
                ) from None
            raise TelegramError(f"Telegram rejected {method} (HTTP {exc.code}).") from None
        except urllib.error.URLError as exc:
            raise TelegramError(f"Could not reach Telegram: {exc.reason}") from None
        if not body.get("ok"):
            raise TelegramError(f"Telegram refused {method}: {body.get('description', 'unknown')}")
        return body.get("result")

    def send_message(self, chat_id: str, text: str, reply_markup: dict | None = None) -> dict:
        # No parse_mode: plan bodies carry `_`, `*` and `[` freely, and legacy
        # Markdown rejects the whole message on one unmatched character.
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self._call("sendMessage", payload, timeout=15) or {}

    def get_updates(self, offset: int, timeout: int, allowed_updates: list[str]) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": allowed_updates}
        if offset:
            payload["offset"] = offset
        # Socket timeout must outlast the long poll or every idle poll raises.
        return list(self._call("getUpdates", payload, timeout=timeout + 10) or [])

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        self._call("answerCallbackQuery", payload, timeout=15)

    def get_me(self) -> dict:
        return self._call("getMe", {}, timeout=15) or {}


# ------------------------------------------------------------------ helpers


def chunks(text: str) -> list[str]:
    return [text[i : i + MAX_MESSAGE] for i in range(0, len(text), MAX_MESSAGE)] or [""]


def send_text(transport: Transport, chat_id: str, text: str, reply_markup: dict | None = None) -> dict:
    """Send `text` in as many messages as it takes; the keyboard rides on the
    last one so the buttons sit under the end of the proposal. Returns the last
    sendMessage result (its message_id is what the gate state records)."""
    parts = chunks(text)
    result: dict = {}
    for i, part in enumerate(parts):
        markup = reply_markup if i == len(parts) - 1 else None
        result = transport.send_message(chat_id, part, markup)
    return result


def approve_keyboard(gate_id: str) -> dict:
    """One row: ✅ Approve · ❌ Reject · 💬 Note, callback_data `gate_id:verb`."""
    row = []
    for label, verb in (("✅ Approve", "approve"), ("❌ Reject", "reject"), ("💬 Note", "note")):
        data = f"{gate_id}:{verb}"
        if len(data.encode()) > MAX_CALLBACK_DATA:
            raise ValueError(f"callback_data {data!r} exceeds {MAX_CALLBACK_DATA} bytes — gate ids must be short")
        row.append({"text": label, "callback_data": data})
    return {"inline_keyboard": [row]}


def parse_callback(data: str) -> tuple[str, str] | None:
    """`gate_id:verb` → (gate_id, verb), or None for anything else."""
    gate_id, sep, verb = (data or "").partition(":")
    if not sep or not gate_id or verb not in VERBS:
        return None
    return gate_id, verb
