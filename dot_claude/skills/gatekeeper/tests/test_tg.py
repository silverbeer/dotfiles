"""tg.py: the Telegram transport, offline (SB-508).

TelegramTransport is exercised by monkeypatching urllib.request.urlopen —
never a real socket. The helpers (chunks/send_text/approve_keyboard/
parse_callback) are exercised against FakeTransport from fakes.py.
"""

from __future__ import annotations

import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tg  # noqa: E402
from fakes import FakeTransport  # noqa: E402

# A distinctive placeholder, not a real-credential-shaped string (a secret
# scanner should never have a reason to look twice at this repo's own tests).
FAKE_TOKEN = "not-a-real-telegram-token-used-only-in-tests"


class ChunksTests(unittest.TestCase):
    def test_short_text_is_one_chunk(self):
        self.assertEqual(tg.chunks("hello"), ["hello"])

    def test_empty_text_is_one_empty_chunk(self):
        # sendMessage rejects an empty body outright; chunks() still must not
        # return an empty list, or send_text would silently send nothing.
        self.assertEqual(tg.chunks(""), [""])

    def test_splits_exactly_at_4096(self):
        text = "x" * (tg.MAX_MESSAGE * 2 + 10)
        parts = tg.chunks(text)
        self.assertEqual(len(parts), 3)
        self.assertEqual([len(p) for p in parts], [4096, 4096, 10])
        self.assertEqual("".join(parts), text)


class SendTextTests(unittest.TestCase):
    def test_short_message_is_one_send_with_the_keyboard(self):
        transport = FakeTransport()
        kb = tg.approve_keyboard("abcd1234")
        tg.send_text(transport, "42", "short body", kb)
        self.assertEqual(len(transport.sent), 1)
        chat_id, text, markup = transport.sent[0]
        self.assertEqual(chat_id, "42")
        self.assertEqual(text, "short body")
        self.assertEqual(markup, kb)

    def test_long_message_is_chunked_and_keyboard_rides_the_last_chunk(self):
        transport = FakeTransport()
        kb = tg.approve_keyboard("abcd1234")
        body = "y" * (tg.MAX_MESSAGE + 500)
        tg.send_text(transport, "42", body, kb)
        self.assertEqual(len(transport.sent), 2)
        _, _, first_markup = transport.sent[0]
        _, _, last_markup = transport.sent[1]
        self.assertIsNone(first_markup)
        self.assertEqual(last_markup, kb)
        self.assertEqual("".join(t for _, t, _ in transport.sent), body)


class ApproveKeyboardTests(unittest.TestCase):
    def test_shape_and_callback_data(self):
        kb = tg.approve_keyboard("a1b2c3d4")
        [row] = kb["inline_keyboard"]
        self.assertEqual([b["callback_data"] for b in row], ["a1b2c3d4:approve", "a1b2c3d4:reject", "a1b2c3d4:note"])
        self.assertEqual([b["text"] for b in row], ["✅ Approve", "❌ Reject", "💬 Note"])

    def test_rejects_a_gate_id_that_would_overflow_64_bytes(self):
        with self.assertRaises(ValueError):
            tg.approve_keyboard("x" * 60)


class ParseCallbackTests(unittest.TestCase):
    def test_valid_verbs(self):
        self.assertEqual(tg.parse_callback("ab12cd34:approve"), ("ab12cd34", "approve"))
        self.assertEqual(tg.parse_callback("ab12cd34:reject"), ("ab12cd34", "reject"))
        self.assertEqual(tg.parse_callback("ab12cd34:note"), ("ab12cd34", "note"))

    def test_unknown_verb_is_rejected(self):
        self.assertIsNone(tg.parse_callback("ab12cd34:snooze"))

    def test_malformed_data_is_rejected(self):
        for bad in ("", "no-colon-here", ":approve", "ab12cd34:"):
            self.assertIsNone(tg.parse_callback(bad), msg=bad)


def _closed_http_error(url: str, code: int, msg: str) -> urllib.error.HTTPError:
    # fp=None gives a real HTTPError with no body to read, but leaves the
    # object thinking it owns an unclosed file — close() immediately so
    # garbage collection doesn't emit a ResourceWarning for every negative test.
    err = urllib.error.HTTPError(url, code, msg, hdrs=None, fp=None)
    err.close()
    return err


def _fake_response(payload: dict):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(payload).encode()

    return _Resp()


class TelegramTransportTests(unittest.TestCase):
    """The real transport, with urlopen replaced. No socket, ever."""

    def test_send_message_has_no_parse_mode_and_the_token_is_only_in_the_url(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["body"] = request.data.decode()
            return _fake_response({"ok": True, "result": {"message_id": 7}})

        transport = tg.TelegramTransport(FAKE_TOKEN)
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = transport.send_message("42", "hello there", None)

        self.assertEqual(result, {"message_id": 7})
        self.assertIn(FAKE_TOKEN, captured["url"])
        self.assertNotIn(FAKE_TOKEN, captured["body"])
        body = json.loads(captured["body"])
        self.assertNotIn("parse_mode", body)
        self.assertEqual(body["text"], "hello there")

    def test_get_updates_passes_offset_and_allowed_updates(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["body"] = json.loads(request.data.decode())
            return _fake_response({"ok": True, "result": [{"update_id": 5}]})

        transport = tg.TelegramTransport(FAKE_TOKEN)
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            updates = transport.get_updates(offset=99, timeout=25, allowed_updates=["callback_query", "message"])

        self.assertEqual(updates, [{"update_id": 5}])
        self.assertEqual(captured["body"]["offset"], 99)
        self.assertEqual(captured["body"]["allowed_updates"], ["callback_query", "message"])

    def test_409_raises_conflict_and_is_never_retried(self):
        calls = {"n": 0}

        def fake_urlopen(request, timeout=None):
            calls["n"] += 1
            raise _closed_http_error(request.full_url, 409, "Conflict")

        transport = tg.TelegramTransport(FAKE_TOKEN)
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(tg.TelegramConflict):
                transport.get_updates(offset=0, timeout=1, allowed_updates=["message"])

        self.assertEqual(calls["n"], 1)

    def test_error_message_never_contains_the_token(self):
        def fake_urlopen(request, timeout=None):
            raise _closed_http_error(request.full_url, 409, "Conflict")

        transport = tg.TelegramTransport(FAKE_TOKEN)
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(tg.TelegramConflict) as ctx:
                transport.send_message("42", "hi")

        self.assertNotIn(FAKE_TOKEN, str(ctx.exception))

    def test_non_409_http_error_raises_plain_telegram_error(self):
        def fake_urlopen(request, timeout=None):
            raise _closed_http_error(request.full_url, 400, "Bad Request")

        transport = tg.TelegramTransport(FAKE_TOKEN)
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(tg.TelegramError) as ctx:
                transport.answer_callback_query("cbid")

        self.assertNotIsInstance(ctx.exception, tg.TelegramConflict)
        self.assertNotIn(FAKE_TOKEN, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
