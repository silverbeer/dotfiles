"""gate.py: dual-channel gate logic, offline (SB-508).

`gate.gql` is monkeypatched to a FakeLinear (fakes.py) so no subprocess ever
runs; Gatekeeper is built directly around a FakeTransport so no socket ever
opens. GATEKEEPER_STATE points at a fresh tempdir per test.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
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
import inbox  # noqa: E402
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

    def later(self, **delta):
        """now_utc() moved forward, for anything waiting on `next_retry_at`."""
        return mock.patch.object(gate, "now_utc", return_value=datetime.now(timezone.utc) + timedelta(**delta))

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


class ReplyRoutingTests(GateTestCase):
    """SB-1089: a Telegram reply names what it answers. Only a reply to a gate's
    own DM may decide or annotate a gate, and only that gate."""

    def reply(self, text, reply_to, update_id):
        self.gk._handle_message(
            {"message_id": update_id * 10, "chat": {"id": 42, "type": "private"}, "from": {"id": 42},
             "date": 1789000000, "text": text, "reply_to_message": {"message_id": reply_to}},
            update_id,
        )

    def inbox_ids(self):
        return sorted(int(p.stem) for p in (inbox.inbox_dir() / "new").glob("*.json"))

    def test_approve_replying_to_a_po_question_leaves_an_awaiting_merge_gate_untouched(self):
        g = self.open_gate(kind="merge")
        self.reply("approve: x", reply_to=999, update_id=700)
        self.assertEqual(gate.load_gate(g["gate_id"])["status"], "awaiting")
        self.assertIsNone(gate.load_gate(g["gate_id"]).get("pending_decision"))
        self.assertEqual(self.inbox_ids(), [700])

    def test_a_pending_note_does_not_capture_a_reply_to_another_message(self):
        g = self.open_gate()
        self.callback(g, "note", cq_id="cb-note")
        self.reply("3, it's small", reply_to=999, update_id=701)
        loaded = gate.load_gate(g["gate_id"])
        self.assertTrue(loaded["note_pending"])
        self.assertIsNone(loaded["note"])
        self.assertEqual(self.inbox_ids(), [701])

    def test_a_reply_to_a_gate_dm_decides_that_gate_not_the_newest(self):
        older = self.open_gate(kind="plan")
        newer = self.open_gate(kind="merge")
        self.reply("approve", reply_to=older["tg_message_id"], update_id=702)
        self.assertEqual(gate.load_gate(older["gate_id"])["status"], "approved")
        self.assertEqual(gate.load_gate(newer["gate_id"])["status"], "awaiting")
        self.assertEqual(self.inbox_ids(), [])

    def test_a_reply_to_a_gate_dm_with_a_pending_note_attaches_to_that_gate(self):
        older = self.open_gate()
        newer = self.open_gate()
        self.callback(older, "note", cq_id="cb-note")
        self.reply("check the migration", reply_to=older["tg_message_id"], update_id=703)
        self.assertEqual(gate.load_gate(older["gate_id"])["note"], "check the migration")
        self.assertIsNone(gate.load_gate(newer["gate_id"])["note"])

    def test_a_decision_replying_to_a_resolved_gate_decides_nothing(self):
        g = self.open_gate()
        self.callback(g, "reject", cq_id="cb-r")
        other = self.open_gate()
        self.reply("approve", reply_to=g["tg_message_id"], update_id=704)
        self.assertEqual(gate.load_gate(g["gate_id"])["status"], "rejected")
        self.assertEqual(gate.load_gate(other["gate_id"])["status"], "awaiting")
        self.assertIn("already rejected — nothing decided", self.transport.texts[-1])


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
        self.gk.poll_once()
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
        # The listener handles the tap; the runner's poll then reads a Linear
        # rejection posted after it (SB-951 split the two).
        self.gk.handle_update(
            {"update_id": 1, "callback_query": {"id": "cb1", "from": {"id": 42}, "data": f"{gate_id}:approve"}}
        )
        self.linear.add_comment("reject: too slow", user_id="user-1")

        resolved = self.gk.poll_once()

        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["status"], "approved")
        self.assertEqual(resolved[0]["source"], "telegram")
        loaded = gate.load_gate(gate_id)
        self.assertEqual(loaded["status"], "approved")
        self.assertEqual(loaded["source"], "telegram")
        self.assertIsNone(loaded["note"])  # the Linear rejection never applied

    def test_a_stale_copy_cannot_overwrite_a_decision_made_elsewhere(self):
        """decide() re-reads under the lock. The runner's poll works from a
        dict it read before the listener recorded a tap; deciding from that
        copy must not flip the gate or post a second echo."""
        g = self.open_gate()
        stale = gate.load_gate(g["gate_id"])
        self.callback(g, "approve")
        comments = len(self.linear.comments)

        self.assertFalse(self.gk.decide(stale, "reject", "too slow", "linear"))

        loaded = gate.load_gate(g["gate_id"])
        self.assertEqual((loaded["status"], loaded["source"]), ("approved", "telegram"))
        self.assertEqual(len(self.linear.comments), comments)
        self.assertEqual(self.linear.labels, ["type:feature", "gate:approved"])

    def test_a_recorded_but_unapplied_tap_still_beats_a_later_linear_comment(self):
        """Linear down when the tap arrived: the decision is on disk as
        `pending_decision`. A Linear comment read once Linear is back came
        second, and must not overtake it."""
        g = self.open_gate()
        self.linear.fail_issue_update = SystemExit("linear-gql: 500 Internal Server Error")
        self.callback(g, "approve")
        self.linear.fail_issue_update = None
        self.linear.add_comment("reject: too slow", user_id="user-1")

        self.gk.check_linear(gate.load_gate(g["gate_id"]))
        self.assertEqual(gate.load_gate(g["gate_id"])["status"], "awaiting")

        with self.later(minutes=1):
            self.gk.poll_once()
        loaded = gate.load_gate(g["gate_id"])
        self.assertEqual((loaded["status"], loaded["source"]), ("approved", "telegram"))


class RetryBackoffTests(GateTestCase):
    """SB-951 review. A recorded decision that cannot apply — the ticket was
    deleted, or moved out of reach — used to be retried every listener loop for
    ever, and the gate never left `awaiting` because the timeout skips gates
    with a recorded decision."""

    def setUp(self):
        super().setUp()
        self.g = self.open_gate()
        self.linear.fail_issue_update = SystemExit("linear-gql: Entity not found: Issue")
        self.gk.handle_update(self.tap())

    def tap(self):
        cq = {"id": "cb77", "from": {"id": 42}, "data": f"{self.g['gate_id']}:approve"}
        return {"update_id": 77, "callback_query": cq}

    def pending(self):
        return gate.load_gate(self.g["gate_id"])["pending_decision"]

    def test_the_first_failure_is_recorded_with_a_retry_30s_out(self):
        pd = self.pending()
        self.assertEqual(pd["attempts"], 1)
        self.assertIn("Entity not found", pd["last_error"])
        wait = (datetime.fromisoformat(pd["next_retry_at"]) - gate.now_utc()).total_seconds()
        self.assertTrue(25 <= wait <= 30, wait)

    def test_no_retry_before_next_retry_at(self):
        queries = len(self.linear.queries)
        self.gk.retry_pending()
        self.assertEqual(len(self.linear.queries), queries, "the listener retried before next_retry_at")
        self.gk.poll_once()
        self.assertEqual(self.pending()["attempts"], 1, "poll retried before next_retry_at")

    def test_the_delay_doubles_and_caps_at_15_minutes(self):
        delays = []
        for _ in range(7):
            when = datetime.fromisoformat(self.pending()["next_retry_at"]) + timedelta(seconds=1)
            with mock.patch.object(gate, "now_utc", return_value=when):
                self.gk.retry_pending()
            delays.append(round((datetime.fromisoformat(self.pending()["next_retry_at"]) - when).total_seconds()))
        self.assertEqual(delays, [60, 120, 240, 480, 900, 900, 900])
        self.assertEqual(self.pending()["attempts"], 8)

    def test_a_later_successful_retry_clears_the_pending_decision(self):
        self.linear.fail_issue_update = None
        with self.later(seconds=31):
            self.gk.retry_pending()
        loaded = gate.load_gate(self.g["gate_id"])
        self.assertEqual((loaded["status"], loaded["source"], loaded["handoff"]), ("approved", "telegram", "pending"))
        self.assertNotIn("pending_decision", loaded)

    def test_after_24h_it_gives_up_to_needs_human_and_says_so_once(self):
        recorded = datetime.fromisoformat(self.pending()["at"])
        with mock.patch.object(gate, "now_utc", return_value=recorded + timedelta(hours=24, seconds=1)):
            self.gk.retry_pending()
        with mock.patch.object(gate, "now_utc", return_value=recorded + timedelta(hours=30)):
            self.gk.retry_pending()
            self.gk.poll_once()

        loaded = gate.load_gate(self.g["gate_id"])
        self.assertEqual((loaded["status"], loaded["reason"]), ("needs-human", "decision_not_applied"))
        self.assertEqual(loaded["unapplied_decision"]["verb"], "approve")
        self.assertNotIn("pending_decision", loaded)
        self.assertEqual(loaded["handoff"], "claimed", "the runner was never handed the stuck gate")
        dms = [t for t in self.transport.texts if "couldn't apply it to Linear" in t]
        self.assertEqual(len(dms), 1, self.transport.texts)
        self.assertIn("recorded your approve on SB-1", dms[0])
        self.assertIn("Entity not found", dms[0])

        # the original tap, redelivered after the give-up, is still a quiet no-op
        sent = list(self.transport.texts)
        self.gk.handle_update(self.tap())
        self.assertEqual(self.transport.texts, sent)

    def test_a_failing_dm_does_not_block_the_give_up(self):
        self.transport.fail_send = TelegramError("Could not reach Telegram")
        recorded = datetime.fromisoformat(self.pending()["at"])
        with mock.patch.object(gate, "now_utc", return_value=recorded + timedelta(hours=25)):
            self.gk.retry_pending()
        self.assertEqual(gate.load_gate(self.g["gate_id"])["status"], "needs-human")


class PollNeverReadsTelegramTests(GateTestCase):
    """SB-951. Telegram allows one getUpdates reader per bot token, and that
    reader is listen.py. A poll that reads too is a 409 for both."""

    def _pending_tap(self, g):
        self.transport.batches = [
            [{"update_id": 9, "callback_query": {"id": "cb9", "from": {"id": 42}, "data": f"{g['gate_id']}:approve"}}]
        ]

    def test_poll_once_never_calls_get_updates(self):
        g = self.open_gate()
        self._pending_tap(g)
        self.gk.poll_once()
        self.assertEqual(self.transport.offsets, [], "poll_once read Telegram — only listen.py may")
        self.assertEqual(gate.load_gate(g["gate_id"])["status"], "awaiting")

    def test_cmd_poll_never_calls_get_updates(self):
        g = self.open_gate()
        self._pending_tap(g)
        with mock.patch.object(gate, "gatekeeper_from_env", return_value=self.gk), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            gate.main(["poll", "--once"])
        self.assertEqual(self.transport.offsets, [], "gate.py poll read Telegram — only listen.py may")
        self.assertEqual(json.loads(out.getvalue())["resolved"], [])


class HandoffTests(GateTestCase):
    """SB-951. The runner learns of a decision from `handoff`, not from having
    witnessed it: the listener decides between ticks, so `poll` has to find
    decisions it did not make."""

    def test_a_gate_decided_between_ticks_is_emitted_once_then_claimed(self):
        g = self.open_gate()
        self.callback(g, "approve")  # the listener, while no tick is running

        first = self.gk.poll_once()
        self.assertEqual([x["gate_id"] for x in first], [g["gate_id"]])
        self.assertEqual(first[0]["status"], "approved")
        loaded = gate.load_gate(g["gate_id"])
        self.assertEqual(loaded["handoff"], "claimed")
        self.assertIsNotNone(loaded.get("claimed_at"))

        self.assertEqual(self.gk.poll_once(), [], "a claimed gate was handed to the runner twice")

    def test_a_gate_resolved_before_sb_951_is_never_emitted(self):
        """No `handoff` field means an older poll already acted on it. The first
        deploy must not resume every session in the gate history."""
        g = self.open_gate()
        legacy = gate.load_gate(g["gate_id"])
        legacy.update(status="approved", decision="approve", source="telegram")
        gate.save_gate(legacy)
        self.assertEqual(self.gk.poll_once(), [])

    def test_a_cli_resolve_is_handed_over_too(self):
        g = self.open_gate()
        self.gk.decide(g, "reject", "not now", "cli")
        self.assertEqual([x["status"] for x in self.gk.poll_once()], ["rejected"])

    def test_a_failed_decision_is_not_handed_over(self):
        g = self.open_gate()
        self.linear.fail_issue_update = SystemExit("linear-gql: 500 Internal Server Error")
        self.gk.decide(g, "approve", None, "cli")
        self.assertNotIn("handoff", gate.load_gate(g["gate_id"]))


class GateLockTests(GateTestCase):
    """SB-951. The listener and the runner's poll write the same gate files, so
    decide() takes a per-gate kernel lock around re-read, check and save."""

    def test_decide_waits_for_another_process_holding_the_gate_lock(self):
        g = self.open_gate()
        lock_path = gate.gates_dir() / f"{g['gate_id']}.lock"
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import fcntl, sys, time\n"
                "f = open(sys.argv[1], 'a')\n"
                "fcntl.flock(f, fcntl.LOCK_EX)\n"
                "print('held', flush=True)\n"
                "time.sleep(float(sys.argv[2]))\n",
                str(lock_path),
                "0.8",
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(holder.wait)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        started = time.monotonic()

        self.assertTrue(self.gk.decide(g, "approve", None, "cli"))

        self.assertGreaterEqual(time.monotonic() - started, 0.5, "decide() did not wait for the gate lock")
        holder.stdout.close()
        self.assertEqual(gate.load_gate(g["gate_id"])["status"], "approved")

    def test_the_lock_is_reentrant_within_a_thread(self):
        g = self.open_gate()
        with gate.gate_lock(g["gate_id"]):
            self.assertTrue(self.gk.decide(g, "approve", None, "cli"))


class TimeoutTests(GateTestCase):
    def test_gate_older_than_the_timeout_becomes_needs_human(self):
        g = self.open_gate()
        loaded = gate.load_gate(g["gate_id"])
        loaded["opened_at"] = (gate.now_utc() - timedelta(hours=100)).isoformat()
        gate.save_gate(loaded)

        self.gk.poll_once()

        loaded = gate.load_gate(g["gate_id"])
        self.assertEqual(loaded["status"], "needs-human")
        self.assertEqual(self.linear.labels, ["type:feature", "gate:needs-human"])
        self.assertTrue(any("stuck" in t for t in self.transport.texts))

    def test_a_gate_within_the_timeout_is_left_alone(self):
        g = self.open_gate()
        self.gk.poll_once()
        self.assertEqual(gate.load_gate(g["gate_id"])["status"], "awaiting")

    def test_timeout_shows_up_in_poll_onces_resolved_list(self):
        g = self.open_gate()
        loaded = gate.load_gate(g["gate_id"])
        loaded["opened_at"] = (gate.now_utc() - timedelta(hours=100)).isoformat()
        gate.save_gate(loaded)

        resolved = self.gk.poll_once()

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


class CloseGateDmTests(GateTestCase):
    """SB-988. `tg_message_id` was recorded on every gate from the start and
    read by nothing, so a decided gate kept its Approve / Reject / Note
    buttons for ever. A reader scrolling the chat could not tell a live gate
    from a dead one — which is how "is anything waiting on me?" became
    unanswerable from Telegram."""

    def _buttons(self, markup):
        return [b for row in (markup or {}).get("inline_keyboard", []) for b in row]

    def test_a_decided_gate_loses_its_verb_buttons(self):
        g = self.open_gate()
        self.callback(g, "approve", cq_id="cb1")
        self.assertTrue(self.transport.edited, "the gate DM was never edited")
        _, msg_id, text, markup = self.transport.edited[-1]
        self.assertEqual(msg_id, g["tg_message_id"])
        self.assertIn("approved", text)
        self.assertEqual([b for b in self._buttons(markup) if "callback_data" in b], [])

    def test_the_closed_message_keeps_a_link_to_the_ticket(self):
        """Closed is not the same as useless — the message stays the record of
        what happened, so it keeps a way back to the ticket."""
        g = self.open_gate()
        self.callback(g, "approve", cq_id="cb1")
        _, _, text, markup = self.transport.edited[-1]
        self.assertIn(g["ticket"], text)
        self.assertEqual([b["text"] for b in self._buttons(markup)], ["🎫 Ticket"])

    def test_a_gate_resolved_in_linear_also_closes_the_dm(self):
        """The path with no Telegram feedback at all before this. A button tap
        at least produced a toast; answering in Linear produced nothing."""
        g = self.open_gate()
        self.linear.add_comment("approve")
        self.gk.check_linear(gate.load_gate(g["gate_id"]))
        self.assertTrue(self.transport.edited, "a Linear-resolved gate left its DM untouched")

    def test_a_superseded_gate_closes_its_dm(self):
        """The worst case, and the one actually hit on SB-870: the ticket was
        merged by hand, so the gate was never answered and Telegram was never
        told anything. Its buttons sat there looking live."""
        self.open_gate()
        self.linear.state_type = "completed"
        self.gk.poll_once()
        self.assertTrue(self.transport.edited, "a superseded gate left its DM untouched")
        _, _, text, markup = self.transport.edited[-1]
        self.assertIn("superseded", text)
        self.assertEqual([b for b in self._buttons(markup) if "callback_data" in b], [])

    def test_a_gate_whose_dm_never_sent_resolves_without_an_edit(self):
        """`open_gate` warns and carries on when Telegram is unreachable but
        the Linear comment and label land, so a gate with no message id is a
        normal state, not an error."""
        g = self.open_gate()
        g["tg_message_id"] = None
        gate.save_gate(g)
        self.gk.close_gate_dm(g, "approved")
        self.assertEqual(self.transport.edited, [])

    def test_a_failed_edit_never_undoes_the_decision(self):
        """The gate is already decided and saved by the time the edit runs.
        Losing a cosmetic rewrite must not send `poll` round again."""
        g = self.open_gate()
        self.transport.fail_edit = RuntimeError("telegram is down")
        self.gk.close_gate_dm(g, "approved")  # must not raise

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
        g = gate.load_gate(g["gate_id"])
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
