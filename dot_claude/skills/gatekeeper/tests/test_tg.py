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


class LinkKeyboardTests(unittest.TestCase):
    """Entities make a URL clickable; a URL button makes it tappable. Different
    guarantee: the button carries its target in the markup, so no offset, no
    chunk boundary and no client-side detection can break it (SB-982)."""

    TICKET = "https://linear.app/silverbeer/issue/SB-870"
    PR = "https://github.com/silverbeer/dotfiles/pull/1"

    def test_approve_keyboard_keeps_the_verbs_and_adds_links(self):
        kb = tg.approve_keyboard("abcd1234", self.TICKET, self.PR)
        verbs, links = kb["inline_keyboard"]
        self.assertEqual([b["callback_data"] for b in verbs],
                         ["abcd1234:approve", "abcd1234:reject", "abcd1234:note"])
        self.assertEqual([b["url"] for b in links], [self.TICKET, self.PR])

    def test_no_pr_no_pr_button(self):
        kb = tg.approve_keyboard("abcd1234", self.TICKET)
        _, links = kb["inline_keyboard"]
        self.assertEqual([b["text"] for b in links], ["🎫 Ticket"])

    def test_a_pr_equal_to_the_ticket_is_not_repeated(self):
        kb = tg.approve_keyboard("abcd1234", self.TICKET, self.TICKET)
        _, links = kb["inline_keyboard"]
        self.assertEqual(len(links), 1)

    def test_no_urls_leaves_the_verb_keyboard_unchanged(self):
        kb = tg.approve_keyboard("abcd1234")
        self.assertEqual(len(kb["inline_keyboard"]), 1)

    def test_links_keyboard_has_no_callback_buttons(self):
        """`blocked` gates use this. Offering Approve there would be a lie —
        nothing is being asked — but pointing at the ticket is the whole ask."""
        kb = tg.links_keyboard(self.TICKET)
        buttons = [b for row in kb["inline_keyboard"] for b in row]
        self.assertEqual([b for b in buttons if "callback_data" in b], [])

    def test_links_keyboard_with_nothing_to_link_is_none(self):
        self.assertIsNone(tg.links_keyboard("", ""))


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


class UrlEntitiesTests(unittest.TestCase):
    """SB-982. Without entities a message carries no link information and each
    client guesses: the phones and the App Store macOS build auto-detect URLs,
    Telegram Desktop on macOS does not — so a gate DM read there could not be
    acted on, which is the only thing a gate DM is for."""

    TICKET = "https://linear.app/silverbeer/issue/SB-870"

    def test_finds_a_bare_url(self):
        text = f"Ticket: {self.TICKET}"
        self.assertEqual(
            tg.url_entities(text),
            [{"type": "url", "offset": 8, "length": len(self.TICKET)}],
        )

    def test_offsets_are_utf16_not_python_characters(self):
        """The trap. An emoji is ONE Python character but TWO UTF-16 units, and
        Telegram counts the latter. Computed with len() the entity starts one
        unit early and the link covers 'https://example.com/' plus a stray
        character short of the end — a silently wrong link, not an error.

        Gate bodies routinely contain emoji, so this is the normal case."""
        url = "https://example.com/x"
        text = f"\U0001f3ab see {url}"
        (ent,) = tg.url_entities(text)
        self.assertEqual(ent["offset"], 7)
        self.assertNotEqual(ent["offset"], text.index(url))  # len() would say 6
        self.assertEqual(ent["length"], len(url))

    def test_trailing_punctuation_is_not_part_of_the_link(self):
        (ent,) = tg.url_entities("see https://example.com/x, then stop")
        self.assertEqual(ent["length"], len("https://example.com/x"))

    def test_several_urls_each_get_an_entity(self):
        text = f"Ticket: {self.TICKET}\nPR:     https://github.com/o/r/pull/1"
        self.assertEqual(len(tg.url_entities(text)), 2)

    def test_no_url_no_entities(self):
        self.assertEqual(tg.url_entities("nothing to see"), [])


class EntitiesOnSendTests(unittest.TestCase):
    def test_send_text_attaches_entities_per_chunk(self):
        """Offsets are relative to the message they ride on, so they must be
        computed per chunk — the same link sits at a different offset in chunk
        2 than it did in the full text."""
        transport = FakeTransport()
        url = "https://example.com/x"
        body = ("filler line\n" * 400) + f"Ticket: {url}"
        tg.send_text(transport, "42", body)
        self.assertGreater(len(transport.sent), 1)
        last_text = transport.sent[-1][1]
        (ent,) = transport.entities[-1]
        self.assertEqual(
            last_text[ent["offset"] : ent["offset"] + ent["length"]],
            url,
            "the entity does not cover the URL in the chunk it was sent with",
        )

    def test_a_url_is_never_split_across_chunks(self):
        """A hard slice at MAX_MESSAGE can cut a URL in half, leaving each half
        marked as a `url` entity over text that is not a URL. These messages
        are line-oriented, so chunks() breaks at a newline instead."""
        url = "https://example.com/" + ("z" * 60)
        filler = "a" * 100
        body = "\n".join([filler] * 60) + f"\n{url}\n" + "\n".join([filler] * 10)
        parts = tg.chunks(body)
        self.assertGreater(len(parts), 1)
        self.assertTrue(
            any(url in p for p in parts),
            "the URL was split across a chunk boundary",
        )


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
