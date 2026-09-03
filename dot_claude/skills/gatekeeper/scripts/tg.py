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
import re
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

    def send_message(
        self,
        chat_id: str,
        text: str,
        reply_markup: dict | None = None,
        entities: list[dict] | None = None,
        silent: bool = False,
    ) -> dict: ...

    def edit_message_text(
        self, chat_id: str, message_id: int, text: str, reply_markup: dict | None = None
    ) -> None: ...

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

    def send_message(
        self,
        chat_id: str,
        text: str,
        reply_markup: dict | None = None,
        entities: list[dict] | None = None,
        silent: bool = False,
    ) -> dict:
        # Still no parse_mode: plan bodies carry `_`, `*` and `[` freely, and
        # legacy Markdown rejects the whole message on one unmatched character.
        # A gate DM that fails to send is worse than one that reads plainly.
        #
        # `entities` gives the same result with none of that risk (SB-982).
        # An entity is an offset and a length over the text as sent — nothing
        # to escape, nothing a stray backtick can break. Without them the
        # message carries no link information at all and each client guesses:
        # iOS, Android and the App Store macOS build auto-detect URLs,
        # Telegram Desktop on macOS does not, so the one action a gate DM
        # exists to prompt could not be taken from the desktop it was read on.
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        # Delivered, but with no sound or vibration (SB-985). Used inside quiet
        # hours, and for every reminder at any hour — a reminder is by
        # definition not new information, so it never earns a notification.
        if silent:
            payload["disable_notification"] = True
        if entities:
            payload["entities"] = entities
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self._call("sendMessage", payload, timeout=15) or {}

    def edit_message_text(
        self, chat_id: str, message_id: int, text: str, reply_markup: dict | None = None
    ) -> None:
        """Rewrite a message already sent — used to close a resolved gate's DM.

        `tg_message_id` was recorded on every gate from the start and read by
        nothing, so a decided gate kept its Approve / Reject / Note buttons
        for ever and a reader could not tell a live gate from a dead one
        (SB-988). Passing `reply_markup={"inline_keyboard": []}` is how the Bot
        API removes a keyboard; omitting the field leaves the old one in place,
        which is the opposite of the intent here.

        Entities are recomputed over the new text, so the ticket link in a
        closed message stays clickable (SB-982).
        """
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        entities = url_entities(text)
        if entities:
            payload["entities"] = entities
        payload["reply_markup"] = reply_markup if reply_markup is not None else {"inline_keyboard": []}
        self._call("editMessageText", payload, timeout=15)

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


# A bare `https://…` run, up to whitespace. Trailing `.,;:!?` and a closing
# bracket are excluded so "see https://x/y." does not linkify the full stop.
_URL_RE = re.compile(r"https?://[^\s<>\"]+[^\s<>\".,;:!?)\]]")


def _utf16_len(s: str) -> int:
    """Length in UTF-16 code units — the unit Telegram entity offsets use.

    NOT len(). A non-BMP character (any emoji outside the basic set) is one
    Python character but TWO UTF-16 units, so a plan body containing one
    shifts every later offset by one and the links land on the wrong text.
    """
    return len(s.encode("utf-16-le")) // 2


def url_entities(text: str) -> list[dict]:
    """`url` entities for every bare URL in `text` (SB-982).

    Computed from the text as it will be SENT, so callers never do offset
    arithmetic and a caller that forgets cannot silently produce a message
    with no links.
    """
    return [
        {"type": "url", "offset": _utf16_len(text[: m.start()]), "length": _utf16_len(m.group())}
        for m in _URL_RE.finditer(text)
    ]


def chunks(text: str) -> list[str]:
    """Split into sendable messages, preferring a line boundary.

    A fixed-width slice can cut a URL in half, which leaves each half marked as
    a `url` entity over text that is not a URL. These messages are
    line-oriented, so breaking at the last newline inside the window costs
    nothing and makes the entity offsets computed per chunk trustworthy.

    A single line longer than MAX_MESSAGE still gets a hard cut — there is no
    boundary to prefer — which is the pre-existing behaviour.
    """
    if len(text) <= MAX_MESSAGE:
        return [text]
    parts: list[str] = []
    rest = text
    while len(rest) > MAX_MESSAGE:
        window = rest[:MAX_MESSAGE]
        cut = window.rfind("\n")
        if cut <= 0:
            cut = MAX_MESSAGE
        parts.append(rest[:cut])
        rest = rest[cut:].lstrip("\n") if cut < MAX_MESSAGE else rest[cut:]
    if rest:
        parts.append(rest)
    return parts or [""]


def send_text(
    transport: Transport, chat_id: str, text: str, reply_markup: dict | None = None, silent: bool = False
) -> dict:
    """Send `text` in as many messages as it takes; the keyboard rides on the
    last one so the buttons sit under the end of the proposal. Returns the last
    sendMessage result (its message_id is what the gate state records).

    Entities are derived per chunk, not once over the whole text: offsets are
    relative to the message they are sent with, and chunk N's link is at a
    different offset in chunk N than it was in the original.
    """
    parts = chunks(text)
    result: dict = {}
    for i, part in enumerate(parts):
        markup = reply_markup if i == len(parts) - 1 else None
        result = transport.send_message(chat_id, part, markup, url_entities(part), silent)
    return result


def link_row(ticket_url: str = "", pr_url: str = "") -> list[dict]:
    """A row of URL buttons (SB-982).

    Entities make the inline URLs clickable; this makes them tappable, which
    is a different guarantee. A URL button carries its target in the markup —
    no offsets, no client-side detection, no way for a chunk boundary or a
    stray character to break it. It is also the better interaction: the reader
    taps a labelled button instead of hunting for a line of text.
    """
    row = []
    if ticket_url:
        row.append({"text": "🎫 Ticket", "url": ticket_url})
    if pr_url and pr_url != ticket_url:
        row.append({"text": "🔗 PR", "url": pr_url})
    return row


def approve_keyboard(gate_id: str, ticket_url: str = "", pr_url: str = "") -> dict:
    """✅ Approve · ❌ Reject · 💬 Note, plus a row of link buttons."""
    row = []
    for label, verb in (("✅ Approve", "approve"), ("❌ Reject", "reject"), ("💬 Note", "note")):
        data = f"{gate_id}:{verb}"
        if len(data.encode()) > MAX_CALLBACK_DATA:
            raise ValueError(f"callback_data {data!r} exceeds {MAX_CALLBACK_DATA} bytes — gate ids must be short")
        row.append({"text": label, "callback_data": data})
    rows = [row]
    links = link_row(ticket_url, pr_url)
    if links:
        rows.append(links)
    return {"inline_keyboard": rows}


def links_keyboard(ticket_url: str = "", pr_url: str = "") -> dict | None:
    """Link buttons with NO verbs — for `blocked` gates.

    A blocked gate deliberately has no Approve/Reject: telling its reader to
    tap one would be a lie, since nothing is being asked of them. Giving them
    a way to open the ticket is not a lie, and it is exactly what they need.
    """
    links = link_row(ticket_url, pr_url)
    return {"inline_keyboard": [links]} if links else None


def parse_callback(data: str) -> tuple[str, str] | None:
    """`gate_id:verb` → (gate_id, verb), or None for anything else."""
    gate_id, sep, verb = (data or "").partition(":")
    if not sep or not gate_id or verb not in VERBS:
        return None
    return gate_id, verb
