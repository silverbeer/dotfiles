"""gate.py: dual-channel gate logic, offline (SB-508).

`gate.gql` is monkeypatched to a FakeLinear (fakes.py) so no subprocess ever
runs; Gatekeeper is built directly around a FakeTransport so no socket ever
opens. GATEKEEPER_STATE points at a fresh tempdir per test.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# The quiet-hours window defaults to 21:00-08:00 local (SB-985), so an
# unpinned suite would behave differently depending on when it runs — gates
# opened during the evening would send silently and reminders would not fire
# at all. start == end disables the window; the tests that exercise quiet
# hours set the constants explicitly instead.
os.environ.setdefault("GATEKEEPER_QUIET_START", "0")
os.environ.setdefault("GATEKEEPER_QUIET_END", "0")

import gate  # noqa: E402
from fakes import FakeLinear, FakeTransport  # noqa: E402
from tg import TelegramError  # noqa: E402


class GateTestCase(unittest.TestCase):
    """Common fixture: a fake Linear, a fake Telegram, one open gate."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_environ = dict(os.environ)
        os.environ["GATEKEEPER_STATE"] = self._tmp.name
        os.environ["LINEAR_ASSIGNEE_ID"] = "user-1"
        # set_gate_label's id cache shares linear-crud's ~/.cache file by
        # default (SB-508) — redirect it into the tempdir so tests never read
        # or write the real machine's cache.
        os.environ["XDG_CACHE_HOME"] = self._tmp.name
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._old_environ)))

        self.linear = FakeLinear(ticket="SB-1", assignee_id="user-1")
        self._old_gql = gate.gql
        gate.gql = self.linear
        self.addCleanup(lambda: setattr(gate, "gql", self._old_gql))

        self.transport = FakeTransport()
        self.gk = gate.Gatekeeper(self.transport, chat_id="42", allowed_ids={42})

    def open_gate(self, kind="plan", ticket="SB-1", body="proposal body here"):
        return self.gk.open_gate(kind, ticket, body, session_id="s1", run_id="r1", link="")

    def callback(self, gate_dict, verb, user_id=42, cq_id="cb1"):
        self.gk._handle_callback({"id": cq_id, "from": {"id": user_id}, "data": f"{gate_dict['gate_id']}:{verb}"})

    def message(self, text, user_id=42, chat_id=42):
        self.gk._handle_message({"chat": {"id": chat_id, "type": "private"}, "from": {"id": user_id}, "text": text})


class OpenGateTests(GateTestCase):
    def test_open_writes_state_posts_marker_comment_sets_label_and_dms_with_a_keyboard(self):
        g = self.open_gate(kind="plan", body="do the thing carefully")
        self.assertEqual(g["status"], "awaiting")
        self.assertEqual(gate.load_gate(g["gate_id"])["gate_id"], g["gate_id"])

        marker = f"<!-- sb-agent:plan:r1:s1 -->"  # noqa: F541 (documents the exact format)
        [(_, comment)] = [(c["id"], c["body"]) for c in self.linear.comments]
        self.assertTrue(comment.startswith(marker))
        self.assertIn("do the thing carefully", comment)
        self.assertEqual(self.linear.labels, ["type:feature", "gate:awaiting-approval"])

        self.assertEqual(len(self.transport.sent), 1)
        chat_id, text, markup = self.transport.sent[0]
        self.assertEqual(chat_id, "42")
        self.assertTrue(text.startswith("[plan] SB-1 — Do the thing"))
        self.assertIsNotNone(markup)

    def test_blocked_kind_gets_no_verbs_but_still_a_link(self):
        """A blocked gate asks nothing of the reader, so offering Approve or
        Reject would be a lie — that is the invariant, and it still holds.

        It is NOT "no keyboard": the reader's whole job on a blocked gate is to
        go and look at the ticket, and a link button is the most direct way to
        let them (SB-982). Asserting "markup is None" conflated the rule with
        one implementation of it.
        """
        self.open_gate(kind="blocked")
        _, _, markup = self.transport.sent[-1]
        buttons = [b for row in markup["inline_keyboard"] for b in row]
        self.assertEqual([b for b in buttons if "callback_data" in b], [])
        self.assertEqual([b["text"] for b in buttons], ["🎫 Ticket"])


class CallbackTests(GateTestCase):
    def test_callback_matches_the_gate_id_and_approves(self):
        g = self.open_gate()
        self.callback(g, "approve", cq_id="cb1")

        loaded = gate.load_gate(g["gate_id"])
        self.assertEqual(loaded["status"], "approved")
        self.assertEqual(loaded["source"], "telegram")
        # The toast names the outcome and the ticket, so a tap that does land
        # tells the human what it did.
        self.assertIn(("cb1", "approved — SB-1"), self.transport.answered)
        self.assertEqual(self.linear.labels, ["type:feature", "gate:approved"])
        # echoed to the channel that did NOT decide
        self.assertTrue(any("<!-- sb-agent:echo -->" in c["body"] for c in self.linear.comments))

    def test_stranger_callback_is_dropped_but_still_answered(self):
        g = self.open_gate()
        sent_before = len(self.transport.sent)
        self.callback(g, "approve", user_id=999, cq_id="cb-stranger")

        loaded = gate.load_gate(g["gate_id"])
        self.assertEqual(loaded["status"], "awaiting")
        self.assertEqual(self.transport.answered, [("cb-stranger", "")])
        self.assertEqual(len(self.transport.sent), sent_before)

    def test_note_button_then_next_message_attaches_and_gate_stays_awaiting(self):
        g = self.open_gate()
        self.callback(g, "note", cq_id="cb-note")
        self.assertTrue(gate.load_gate(g["gate_id"])["note_pending"])

        self.message("please re-check the migration before merging")

        loaded = gate.load_gate(g["gate_id"])
        self.assertEqual(loaded["status"], "awaiting")
        self.assertFalse(loaded["note_pending"])
        self.assertEqual(loaded["note"], "please re-check the migration before merging")

    def test_telegram_free_text_reject_with_reason_decides(self):
        g = self.open_gate()
        self.message("reject: needs another pass")

        loaded = gate.load_gate(g["gate_id"])
        self.assertEqual(loaded["status"], "rejected")
        self.assertEqual(loaded["source"], "telegram")
        self.assertEqual(loaded["note"], "needs another pass")


class ParkedReminderTests(GateTestCase):
    """SB-973. The first version of the parked-gate reminder keyed on
    `hours >= 12`, which stays true forever — so it fired on EVERY tick once a
    gate crossed 12h. Five messages arrived in 96 minutes, each linking to a
    team board rather than the ticket. That is the nagging SB-945 removed and
    the generic link the user had explicitly asked against."""

    def _age_gate(self, g, hours):
        g["opened_at"] = (gate.now_utc() - timedelta(hours=hours)).isoformat()
        gate.save_gate(g)
        return g

    def test_no_reminder_before_the_threshold(self):
        g = self._age_gate(self.open_gate(), 3)
        self.assertFalse(gate._due_for_reminder(gate.load_gate(g["gate_id"])))

    def test_first_reminder_after_the_threshold(self):
        g = self._age_gate(self.open_gate(), 13)
        self.assertTrue(gate._due_for_reminder(gate.load_gate(g["gate_id"])))

    def test_a_second_tick_does_not_re_notify(self):
        # The whole bug: every 30-minute tick sent another message.
        g = self._age_gate(self.open_gate(), 13)
        self.assertTrue(gate._due_for_reminder(gate.load_gate(g["gate_id"])))
        for _ in range(5):
            self.assertFalse(gate._due_for_reminder(gate.load_gate(g["gate_id"])))

    def test_it_reminds_again_a_day_later(self):
        g = self._age_gate(self.open_gate(), 13)
        gate._due_for_reminder(gate.load_gate(g["gate_id"]))
        stale = gate.load_gate(g["gate_id"])
        stale["reminded_at"] = (gate.now_utc() - timedelta(hours=25)).isoformat()
        gate.save_gate(stale)
        self.assertTrue(gate._due_for_reminder(gate.load_gate(g["gate_id"])))

    def test_the_link_is_the_ticket_not_a_board(self):
        self.assertEqual(gate.issue_url("SB-964"), "https://linear.app/silverbeer/issue/SB-964")
        self.assertNotIn("/team/", gate.issue_url("SB-964"))


class SupersededGateTests(GateTestCase):
    """SB-949. A ticket can reach Done without its gate ever being answered:
    the PR merges, Linear closes the issue via "Fixes SB-N", and nothing tells
    the gate. It then sits `awaiting` forever — and since SB-944 makes a
    pending gate skip the ticket, a stale gate can park a ticket permanently.
    Four accumulated in two days."""

    def test_gate_on_a_done_ticket_is_closed_as_superseded(self):
        g = self.open_gate()
        self.linear.state_name, self.linear.state_type = "Done", "completed"

        closed = gate.superseded_gates()

        self.assertEqual([c["gate_id"] for c in closed], [g["gate_id"]])
        loaded = gate.load_gate(g["gate_id"])
        self.assertEqual(loaded["status"], "superseded")
        self.assertEqual(loaded["source"], "ticket-closed")
        self.assertIn("Done", loaded["note"])

    def test_the_gate_label_is_removed_not_restamped(self):
        # gate:approved would imply a human answered. Nobody did.
        self.open_gate()
        self.linear.state_name, self.linear.state_type = "Done", "completed"
        gate.superseded_gates()
        self.assertEqual([n for n in self.linear.labels if n.startswith("gate:")], [])

    def test_a_canceled_ticket_counts_too(self):
        g = self.open_gate()
        self.linear.state_name, self.linear.state_type = "Canceled", "canceled"
        gate.superseded_gates()
        self.assertEqual(gate.load_gate(g["gate_id"])["status"], "superseded")

    def test_an_open_ticket_is_left_alone(self):
        g = self.open_gate()
        self.assertEqual(gate.superseded_gates(), [])
        self.assertEqual(gate.load_gate(g["gate_id"])["status"], "awaiting")

    def test_poll_closes_superseded_gates_before_draining(self):
        g = self.open_gate()
        self.linear.state_name, self.linear.state_type = "Done", "completed"
        self.gk.poll_once(timeout=0)
        self.assertEqual(gate.load_gate(g["gate_id"])["status"], "superseded")
        # and it must no longer count as awaiting, or it keeps parking the ticket
        self.assertEqual([x["gate_id"] for x in gate.awaiting_gates()], [])


class TelegramTextTests(unittest.TestCase):
    """SB-954. A `pr` gate arrived carrying only the PR link, so the message
    had no route back to the ticket — on exactly the gate where a human wants
    both, and where a Linear comment is currently the more reliable way to
    answer (SB-951)."""

    TICKET_URL = "https://linear.app/silverbeer/issue/SB-940"
    PR_URL = "https://github.com/silverbeer/missing-table/pull/590"

    def test_pr_gate_carries_both_links_labelled(self):
        text = gate.telegram_text("pr", "SB-940", "Dark mode", "body", self.PR_URL, self.TICKET_URL)
        self.assertIn(f"Ticket: {self.TICKET_URL}", text)
        self.assertIn(self.PR_URL, text.split("Ticket:")[1])

    def test_plan_gate_shows_one_link_when_both_are_the_same(self):
        text = gate.telegram_text("plan", "SB-624", "Prep", "body", self.TICKET_URL, self.TICKET_URL)
        self.assertEqual(text.count(self.TICKET_URL), 1)
        self.assertNotIn("PR:", text)

    def test_every_kind_carries_the_ticket_link(self):
        for kind in ("plan", "pr", "merge", "blocked"):
            text = gate.telegram_text(kind, "SB-940", "t", "body", self.PR_URL, self.TICKET_URL)
            self.assertIn(self.TICKET_URL, text, f"{kind} gate lost the ticket link")

    def test_a_decidable_gate_says_how_to_answer_it(self):
        text = gate.telegram_text("pr", "SB-940", "t", "body", self.PR_URL, self.TICKET_URL)
        self.assertIn("comment `approve` on the ticket", text)

    def test_blocked_gate_does_not_promise_buttons_it_does_not_have(self):
        # open_gate sends no keyboard for `blocked`, so telling the reader to
        # tap would be a lie.
        text = gate.telegram_text("blocked", "SB-593", "t", "body", self.TICKET_URL, self.TICKET_URL)
        self.assertNotIn("buttons", text)

    def test_the_body_is_still_truncated_and_the_trailer_survives(self):
        long_body = "x" * (gate.SUMMARY_CHARS + 500)
        text = gate.telegram_text("pr", "SB-940", "t", long_body, self.PR_URL, self.TICKET_URL)
        self.assertIn("…", text)
        # The trailer is what the reader acts on; truncation must never eat it.
        self.assertIn(f"Ticket: {self.TICKET_URL}", text)


class CallbackAnswerFailureTests(GateTestCase):
    """SB-950. A callback query id expires about a minute after the tap; this
    poller runs on a 30-minute tick, so `answerCallbackQuery` nearly always
    fails in production. It used to be called FIRST and unguarded, so the raise
    propagated, `drain_telegram` acked the update in its `finally` anyway, and
    a real human approval was destroyed by the failure of a courtesy toast."""

    def test_expired_callback_id_still_records_the_decision(self):
        g = self.open_gate()
        self.transport.fail_answer = gate.TelegramError("Telegram rejected answerCallbackQuery (HTTP 400).")

        self.callback(g, "approve", cq_id="cb-expired")

        loaded = gate.load_gate(g["gate_id"])
        self.assertEqual(loaded["status"], "approved")
        self.assertEqual(loaded["source"], "telegram")
        self.assertEqual(self.linear.labels, ["type:feature", "gate:approved"])

    def test_a_failing_answer_never_escapes_the_handler(self):
        g = self.open_gate()
        self.transport.fail_answer = gate.TelegramError("Telegram rejected answerCallbackQuery (HTTP 400).")
        try:
            self.callback(g, "reject", cq_id="cb-expired-2")
        except gate.TelegramError as exc:  # pragma: no cover - the regression
            self.fail(f"a failed courtesy answer escaped _handle_callback: {exc}")
        self.assertEqual(gate.load_gate(g["gate_id"])["status"], "rejected")

    def test_stranger_is_still_answered_when_the_answer_works(self):
        g = self.open_gate()
        self.callback(g, "approve", user_id=999, cq_id="cb-stranger-2")
        self.assertEqual(gate.load_gate(g["gate_id"])["status"], "awaiting")
        self.assertIn(("cb-stranger-2", ""), self.transport.answered)


class DrainAckPolicyTests(GateTestCase):
    """SB-950, the other half: what `drain_telegram` acks. Telegram replays an
    unacked update for 24h, which is the safety net — acking one whose decision
    was never recorded throws the human's answer away permanently."""

    def _batch(self, gate_dict, verb="approve", update_id=500, cq_id="cb-drain"):
        return [
            {
                "update_id": update_id,
                "callback_query": {"id": cq_id, "from": {"id": 42}, "data": f"{gate_dict['gate_id']}:{verb}"},
            }
        ]

    def test_a_clean_update_is_acked(self):
        g = self.open_gate()
        self.transport.batches = [self._batch(g, update_id=500)]
        self.gk.drain_telegram(timeout=0)

        self.assertEqual(gate.load_gate(g["gate_id"])["status"], "approved")
        self.assertEqual(self.gk._offset(), 501)

    def test_a_transport_failure_does_not_ack_so_telegram_redelivers(self):
        g = self.open_gate()
        # send_text runs after the decision is saved; make it blow up to stand
        # in for any transport failure mid-handling.
        self.transport.batches = [self._batch(g, verb="note", update_id=700)]
        self.transport.fail_send = gate.TelegramError("Could not reach Telegram: timed out")

        self.gk.drain_telegram(timeout=0)

        # Offset must NOT have advanced past an update we could not finish.
        self.assertNotEqual(self.gk._offset(), 701)

    def test_an_unprocessable_update_is_acked_so_it_cannot_block_the_queue(self):
        g = self.open_gate()
        batch = self._batch(g, update_id=900)
        # A shape the handler cannot process at all — not a transport problem.
        batch[0]["callback_query"]["from"] = None
        self.transport.batches = [batch]

        self.gk.drain_telegram(timeout=0)

        self.assertEqual(self.gk._offset(), 901)


class LinearChannelTests(GateTestCase):
    def test_linear_approve_with_note_is_parsed(self):
        g = self.open_gate()
        self.linear.add_comment("approve: looks good", user_id="user-1")
        self.gk.check_linear(g)

        self.assertEqual(g["status"], "approved")
        self.assertEqual(g["source"], "linear")
        self.assertEqual(g["note"], "looks good")
        self.assertTrue(any("approved via linear" in t for t in self.transport.texts))

    def test_linear_reject_with_reason_is_parsed(self):
        g = self.open_gate()
        self.linear.add_comment("reject: not ready", user_id="user-1")
        self.gk.check_linear(g)

        self.assertEqual(g["status"], "rejected")
        self.assertEqual(g["source"], "linear")
        self.assertEqual(g["note"], "not ready")

    def test_discussion_comment_is_forwarded_once_and_gate_stays_open(self):
        g = self.open_gate()
        self.linear.add_comment("what does this affect downstream?", user_id="user-1")

        self.gk.check_linear(g)
        self.assertEqual(g["status"], "awaiting")
        self.assertTrue(any("what does this affect downstream?" in t for t in self.transport.texts))
        forwarded_count = sum("what does this affect downstream?" in t for t in self.transport.texts)

        self.gk.check_linear(g)  # a second poll tick must not forward it again
        self.assertEqual(sum("what does this affect downstream?" in t for t in self.transport.texts), forwarded_count)

    def test_comment_before_the_marker_is_ignored(self):
        g = self.open_gate()
        # Inserted directly so its createdAt sorts before the marker comment,
        # simulating a stray comment that predates the gate (e.g. from a prior
        # gate on the same ticket).
        self.linear.comments.insert(
            0, {"id": "c-early", "body": "approve", "createdAt": "2026-01-01T00:00:00Z", "user": {"id": "user-1"}}
        )
        self.gk.check_linear(g)
        self.assertEqual(g["status"], "awaiting")

    def test_echo_comment_is_never_read_back_as_a_decision(self):
        g = self.open_gate()
        self.callback(g, "approve")  # decides via telegram, posts a Linear echo
        loaded = gate.load_gate(g["gate_id"])
        self.assertEqual(loaded["status"], "approved")
        # A second, already-resolved gate must not be re-decided by its own echo.
        self.gk.check_linear(loaded)
        self.assertEqual(loaded["source"], "telegram")


class FirstDecisionWinsTests(GateTestCase):
    def test_both_channels_decide_first_wins_and_source_is_recorded(self):
        g = self.open_gate()
        gate_id = g["gate_id"]
        approve_cb = {"id": "cb1", "from": {"id": 42}, "data": f"{gate_id}:approve"}
        self.transport.batches = [[{"update_id": 1, "callback_query": approve_cb}]]
        self.linear.add_comment("reject: too slow", user_id="user-1")

        resolved = self.gk.poll_once(timeout=5)

        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["status"], "approved")
        self.assertEqual(resolved[0]["source"], "telegram")
        loaded = gate.load_gate(gate_id)
        self.assertEqual(loaded["status"], "approved")
        self.assertEqual(loaded["source"], "telegram")
        self.assertIsNone(loaded["note"])  # the Linear rejection never applied


class TimeoutTests(GateTestCase):
    def test_gate_older_than_the_timeout_becomes_needs_human(self):
        g = self.open_gate()
        loaded = gate.load_gate(g["gate_id"])
        loaded["opened_at"] = (gate.now_utc() - timedelta(hours=100)).isoformat()
        gate.save_gate(loaded)

        self.gk.poll_once(timeout=1)

        loaded = gate.load_gate(g["gate_id"])
        self.assertEqual(loaded["status"], "needs-human")
        self.assertEqual(self.linear.labels, ["type:feature", "gate:needs-human"])
        self.assertTrue(any("stuck" in t for t in self.transport.texts))

    def test_a_gate_within_the_timeout_is_left_alone(self):
        g = self.open_gate()
        self.gk.poll_once(timeout=0)
        self.assertEqual(gate.load_gate(g["gate_id"])["status"], "awaiting")

    def test_timeout_shows_up_in_poll_onces_resolved_list(self):
        g = self.open_gate()
        loaded = gate.load_gate(g["gate_id"])
        loaded["opened_at"] = (gate.now_utc() - timedelta(hours=100)).isoformat()
        gate.save_gate(loaded)

        resolved = self.gk.poll_once(timeout=1)

        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["gate_id"], g["gate_id"])
        self.assertEqual(resolved[0]["status"], "needs-human")
        self.assertEqual(resolved[0]["decision"], "needs-human")
        self.assertEqual(resolved[0]["source"], "timeout")


class LabelUpdateFailureTests(GateTestCase):
    """decide(): a failed label update or echo must not mark the gate resolved
    — it has to stay "awaiting" on disk so `poll` retries it (SB-508 review)."""

    def test_label_update_failure_leaves_the_gate_awaiting(self):
        g = self.open_gate()
        sent_before = len(self.transport.sent)
        self.linear.fail_issue_update = SystemExit("linear-gql: 500 Internal Server Error")

        self.gk.decide(g, "approve", None, "telegram")

        loaded = gate.load_gate(g["gate_id"])
        self.assertEqual(loaded["status"], "awaiting")
        self.assertIsNone(loaded["decision"])
        self.assertIsNone(loaded["source"])
        # No echo went out either — the label update failed before it got there.
        self.assertEqual(self.linear.labels, ["type:feature", "gate:awaiting-approval"])
        self.assertEqual(len(self.transport.sent), sent_before)

    def test_label_update_recovers_and_resolves_on_a_later_retry(self):
        g = self.open_gate()
        self.linear.fail_issue_update = SystemExit("linear-gql: 500 Internal Server Error")
        self.gk.decide(g, "approve", None, "telegram")
        self.assertEqual(gate.load_gate(g["gate_id"])["status"], "awaiting")

        self.linear.fail_issue_update = None
        self.gk.decide(gate.load_gate(g["gate_id"]), "approve", None, "telegram")

        loaded = gate.load_gate(g["gate_id"])
        self.assertEqual(loaded["status"], "approved")
        self.assertEqual(loaded["source"], "telegram")


class TelegramOpenFailureTests(GateTestCase):
    """open_gate(): a Telegram send failure must not orphan the gate — the
    Linear comment and label are already posted, so the gate stays resolvable
    via the Linear channel even without a DM ever going out (SB-508 review)."""

    def test_telegram_failure_leaves_a_gate_resolvable_via_linear(self):
        self.transport.fail_send = TelegramError("could not reach Telegram")

        g = self.open_gate()

        self.assertEqual(g["status"], "awaiting")
        self.assertIsNone(g["tg_message_id"])
        loaded = gate.load_gate(g["gate_id"])
        self.assertEqual(loaded["status"], "awaiting")
        # The Linear side already landed despite the Telegram failure.
        self.assertEqual(self.linear.labels, ["type:feature", "gate:awaiting-approval"])
        self.assertEqual(len(self.linear.comments), 1)

        # And the gate is still resolvable via Linear.
        self.transport.fail_send = None
        self.linear.add_comment("approve", user_id="user-1")
        self.gk.check_linear(loaded)
        self.assertEqual(loaded["status"], "approved")
        self.assertEqual(loaded["source"], "linear")


class NoSecretInMessagesTests(GateTestCase):
    def test_no_message_ever_carries_anything_token_shaped(self):
        g = self.open_gate()
        self.callback(g, "approve")
        self.linear.add_comment("reject: too slow", user_id="user-1")
        self.gk.check_linear(g)

        # Gatekeeper is built around a Transport, never a token — this proves
        # nothing token-shaped rides in any outbound text.
        for text in self.transport.texts:
            self.assertNotRegex(text, r"\d{6,}:[A-Za-z0-9_-]{30,}")


class ResolveTests(GateTestCase):
    def test_manual_resolve_echoes_to_both_channels(self):
        g = self.open_gate()
        self.gk.decide(g, "approve", "shipped from the CLI", "cli")

        self.assertEqual(g["status"], "approved")
        self.assertTrue(any("approved via cli" in t for t in self.transport.texts))
        self.assertTrue(any("approved via cli" in c["body"] for c in self.linear.comments))


class QuietHoursTests(unittest.TestCase):
    """SB-985. REMINDER_AFTER_HOURS is a debounce, not a curfew: it fires 12h
    after a gate opens, whenever that lands. SB-870's gate opened 16:03 and
    reminded at 04:30 — and because REMINDER_EVERY_HOURS is exactly 24, it
    would have gone on reminding at 04:30 every day."""

    def setUp(self):
        self._saved = (gate.QUIET_START_HOUR, gate.QUIET_END_HOUR, gate.QUIET_TZ)
        gate.QUIET_START_HOUR, gate.QUIET_END_HOUR = 21, 8
        gate.QUIET_TZ = "America/New_York"

    def tearDown(self):
        gate.QUIET_START_HOUR, gate.QUIET_END_HOUR, gate.QUIET_TZ = self._saved

    def at(self, hour):
        return datetime(2026, 9, 3, hour, 0, tzinfo=ZoneInfo("America/New_York"))

    def test_window_wraps_midnight(self):
        """The trap. A single `start <= h < end` is EMPTY for 21..8, so the
        feature would silently do nothing rather than fail."""
        for hour in (21, 23, 0, 2, 7):
            self.assertTrue(gate.in_quiet_hours(self.at(hour)), f"{hour}:00 should be quiet")
        for hour in (8, 9, 12, 20):
            self.assertFalse(gate.in_quiet_hours(self.at(hour)), f"{hour}:00 should not be quiet")

    def test_start_equal_to_end_disables_the_window(self):
        gate.QUIET_START_HOUR = gate.QUIET_END_HOUR = 0
        self.assertFalse(gate.in_quiet_hours(self.at(3)))

    def test_an_unknown_timezone_does_not_silence_everything(self):
        """No tzdata is not a reason to start waking someone at 04:30, but it
        is also not a reason to go permanently silent."""
        gate.QUIET_TZ = "Mars/Olympus_Mons"
        self.assertFalse(gate.in_quiet_hours(self.at(3)))


class QuietReminderTests(GateTestCase):
    def setUp(self):
        super().setUp()
        self._saved = (gate.QUIET_START_HOUR, gate.QUIET_END_HOUR)
        gate.QUIET_START_HOUR, gate.QUIET_END_HOUR = 21, 8

    def tearDown(self):
        gate.QUIET_START_HOUR, gate.QUIET_END_HOUR = self._saved

    def _old_gate(self):
        g = self.open_gate()
        g = gate.load_gate(g["gate_id"]) if hasattr(gate, "load_gate") else g
        g["opened_at"] = (gate.now_utc() - timedelta(hours=30)).isoformat()
        gate.save_gate(g)
        return g

    def test_a_reminder_due_in_the_window_does_not_fire_and_does_not_stamp(self):
        """Suppressing while still stamping would drop the reminder for a full
        24 hours — worse than the 04:30 ping it is meant to prevent."""
        g = self._old_gate()
        with mock.patch.object(gate, "in_quiet_hours", return_value=True):
            self.assertFalse(gate._due_for_reminder(g))
        self.assertIsNone(g.get("reminded_at"), "the gate was stamped despite not notifying")

    def test_the_same_reminder_fires_once_the_window_closes(self):
        g = self._old_gate()
        with mock.patch.object(gate, "in_quiet_hours", return_value=True):
            gate._due_for_reminder(g)
        with mock.patch.object(gate, "in_quiet_hours", return_value=False):
            self.assertTrue(gate._due_for_reminder(g), "the held reminder never fired in the morning")
        self.assertIsNotNone(g.get("reminded_at"))


class SilentDeliveryTests(GateTestCase):
    def test_a_gate_opened_in_the_window_is_delivered_silently(self):
        """Silent, NOT held. open_gate records tg_message_id from this send and
        edits that message when the gate resolves; a held DM has no id and
        would degrade into the 'DM failed' path — no buttons overnight, and
        gate state disagreeing with the chat."""
        with mock.patch.object(gate, "in_quiet_hours", return_value=True):
            g = self.open_gate()
        self.assertTrue(self.transport.silent[-1])
        self.assertIsNotNone(g.get("tg_message_id"), "a silent send still has to record its message id")

    def test_a_gate_opened_outside_the_window_makes_a_sound(self):
        with mock.patch.object(gate, "in_quiet_hours", return_value=False):
            self.open_gate()
        self.assertFalse(self.transport.silent[-1])


if __name__ == "__main__":
    unittest.main()
