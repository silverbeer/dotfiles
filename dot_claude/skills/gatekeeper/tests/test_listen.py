"""listen.py: the one getUpdates reader (SB-951), offline.

Same fixture as test_gate.py — a FakeLinear behind `gate.gql`, a FakeTransport
in the Gatekeeper, GATEKEEPER_STATE in a tempdir — with a Listener wrapped
around it. No socket, no git, no signal handlers installed.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
sys.path.insert(0, str(HERE))

os.environ.setdefault("GATEKEEPER_QUIET_START", "0")
os.environ.setdefault("GATEKEEPER_QUIET_END", "0")

import gate  # noqa: E402
import inbox  # noqa: E402
import listen  # noqa: E402
from tg import TelegramConflict, TelegramError  # noqa: E402
from test_gate import GateTestCase  # noqa: E402


class ListenerTestCase(GateTestCase):
    def setUp(self):
        super().setUp()
        self.sleeps: list[float] = []
        self.listener = listen.Listener(self.gk, sleep=self.sleeps.append)

    def tap(self, g, verb="approve", update_id=500, cq_id="cb-tap", user_id=42):
        return {
            "update_id": update_id,
            "callback_query": {"id": cq_id, "from": {"id": user_id}, "data": f"{g['gate_id']}:{verb}"},
        }

    def text(self, body, update_id=600, user_id=42):
        return {
            "update_id": update_id,
            "message": {
                "message_id": update_id * 10,
                "from": {"id": user_id},
                "chat": {"id": user_id, "type": "private"},
                "date": 1789000000,
                "text": body,
            },
        }

    def inbox_names(self, sub="new"):
        return sorted(p.name for p in (inbox.inbox_dir() / sub).glob("*.json"))


class OnlyReaderTests(unittest.TestCase):
    """The invariant the Deployment exists for: exactly one getUpdates reader
    per bot token. A second call site anywhere in the skills is a 409 waiting
    for the day both run."""

    def test_listen_py_is_the_only_caller_of_get_updates(self):
        skills = HERE.parent.parent
        callers = set()
        for path in list(skills.rglob("*.py")) + list(skills.rglob("*.sh")):
            if "tests" in path.parts:
                continue
            for line in path.read_text(errors="replace").splitlines():
                if re.search(r"\bget_updates\(", line) and not line.lstrip().startswith("def "):
                    callers.add(path.relative_to(skills).as_posix())
        self.assertEqual(callers, {"gatekeeper/scripts/listen.py"})


class AckTests(ListenerTestCase):
    def test_a_tap_is_answered_in_the_same_iteration(self):
        """The acceptance criterion. The toast used to arrive up to 30 minutes
        late, by which time the callback id had expired (SB-950)."""
        g = self.open_gate()
        self.transport.batches = [[self.tap(g, cq_id="cb-fast")]]

        self.listener.run_once(timeout=0)

        self.assertIn(("cb-fast", "approved — SB-1"), self.transport.answered)
        loaded = gate.load_gate(g["gate_id"])
        self.assertEqual((loaded["status"], loaded["source"]), ("approved", "telegram"))
        self.assertEqual(loaded["handoff"], "pending")
        self.assertEqual(self.listener._offset(), 501)

    def test_the_tap_is_answered_before_linear_is_touched(self):
        g = self.open_gate()
        self.transport.batches = [[self.tap(g)]]
        queries_before = len(self.linear.queries)
        answered_at = []
        real_answer = self.transport.answer_callback_query

        def answer(cq_id, text=""):
            answered_at.append(len(self.linear.queries))
            real_answer(cq_id, text)

        self.transport.answer_callback_query = answer
        self.listener.run_once(timeout=0)
        self.assertEqual(answered_at, [queries_before], "the toast waited on a Linear round trip")

    def test_linear_down_keeps_the_decision_and_the_next_loop_applies_it(self):
        g = self.open_gate()
        self.linear.fail_issue_update = SystemExit("linear-gql: 500 Internal Server Error")
        self.transport.batches = [[self.tap(g, update_id=510, cq_id="cb-down")]]

        self.listener.run_once(timeout=0)

        # Answered and acked: the decision is on disk, so the update is done.
        self.assertIn(("cb-down", "approved — SB-1"), self.transport.answered)
        self.assertEqual(self.listener._offset(), 511)
        loaded = gate.load_gate(g["gate_id"])
        self.assertEqual(loaded["status"], "awaiting")
        self.assertEqual(loaded["pending_decision"]["verb"], "approve")
        self.assertEqual(loaded["pending_decision"]["update_id"], 510)

        self.linear.fail_issue_update = None
        with self.later(minutes=1):  # past next_retry_at
            self.listener.run_once(timeout=0)

        loaded = gate.load_gate(g["gate_id"])
        self.assertEqual((loaded["status"], loaded["source"]), ("approved", "telegram"))
        self.assertNotIn("pending_decision", loaded)
        self.assertEqual(loaded["handoff"], "pending")
        self.assertEqual(self.linear.labels, ["type:feature", "gate:approved"])

    def test_the_runner_applies_a_recorded_decision_when_the_listener_is_gone(self):
        """Durable when the listener is down: it recorded the tap and died
        before Linear came back. The runner's poll finishes the job."""
        g = self.open_gate()
        self.linear.fail_issue_update = SystemExit("linear-gql: 500 Internal Server Error")
        self.transport.batches = [[self.tap(g)]]
        self.listener.run_once(timeout=0)
        self.linear.fail_issue_update = None

        with self.later(minutes=1):
            resolved = self.gk.poll_once()

        self.assertEqual([(x["gate_id"], x["status"]) for x in resolved], [(g["gate_id"], "approved")])

    def test_a_redelivered_tap_is_a_quiet_no_op(self):
        g = self.open_gate()
        update = self.tap(g, update_id=520, cq_id="cb-once")
        self.transport.batches = [[update]]
        self.listener.run_once(timeout=0)
        sent, comments, labels = list(self.transport.texts), len(self.linear.comments), list(self.linear.labels)

        # Offset lost (a failed write after the decision saved): Telegram replays.
        self.listener._remember(520)
        self.transport.batches = [[update]]
        self.listener.run_once(timeout=0)

        self.assertEqual(self.transport.texts, sent, "a replay told the human their own tap was 'not awaiting'")
        self.assertEqual(len(self.linear.comments), comments, "a replay echoed the decision to Linear twice")
        self.assertEqual(self.linear.labels, labels)
        self.assertEqual(self.listener._offset(), 521)

    def test_a_redelivered_note_tap_is_a_no_op(self):
        g = self.open_gate()
        update = self.tap(g, verb="note", update_id=530)
        self.transport.batches = [[update]]
        self.listener.run_once(timeout=0)
        sent = list(self.transport.texts)
        self.listener._remember(530)
        self.transport.batches = [[update]]
        self.listener.run_once(timeout=0)
        self.assertEqual(self.transport.texts, sent)

    def test_a_failed_disk_write_is_not_acked_and_not_answered(self):
        """The PVC refusing a write is weather, not a poison pill: the tap was
        not recorded, so it must be redelivered — and the human must not get
        a toast for a decision that is not on disk."""
        g = self.open_gate()
        self.transport.batches = [[self.tap(g, update_id=540, cq_id="cb-disk")]]

        with mock.patch.object(gate, "save_gate", side_effect=OSError("Read-only file system")):
            with self.assertRaises(OSError):
                self.listener.run_once(timeout=0)

        self.assertEqual(self.transport.answered, [])
        self.assertNotEqual(self.listener._offset(), 541)
        self.assertEqual(gate.load_gate(g["gate_id"])["status"], "awaiting")

    def test_a_transport_failure_mid_batch_does_not_ack(self):
        g = self.open_gate()
        self.transport.batches = [[self.tap(g, verb="note", update_id=700)]]
        self.transport.fail_send = TelegramError("Could not reach Telegram: timed out")

        with self.assertRaises(TelegramError):
            self.listener.drain(timeout=0)

        self.assertNotEqual(self.listener._offset(), 701)

    def test_an_unprocessable_update_is_acked_so_it_cannot_block_the_queue(self):
        g = self.open_gate()
        update = self.tap(g, update_id=900)
        update["callback_query"]["from"] = None
        self.transport.batches = [[update]]

        self.listener.drain(timeout=0)

        self.assertEqual(self.listener._offset(), 901)


class RoutingTests(ListenerTestCase):
    def test_free_text_goes_to_the_inbox_exactly_once(self):
        update = self.text("what's at risk this cycle?", update_id=600)
        self.transport.batches = [[update]]
        self.listener.run_once(timeout=0)
        self.assertEqual(self.inbox_names(), ["600.json"])

        self.listener._remember(600)
        self.transport.batches = [[update]]
        self.listener.run_once(timeout=0)
        self.assertEqual(self.inbox_names(), ["600.json"])

        # ...and still once after the consumer has claimed it.
        inbox.claim_next()
        self.listener._remember(600)
        self.transport.batches = [[update]]
        self.listener.run_once(timeout=0)
        self.assertEqual((self.inbox_names("new"), self.inbox_names("cur")), ([], ["600.json"]))

    def test_a_note_reply_attaches_to_its_gate_and_skips_the_inbox(self):
        g = self.open_gate()
        self.transport.batches = [
            [self.tap(g, verb="note", update_id=610), self.text("check the migration first", update_id=611)]
        ]
        self.listener.run_once(timeout=0)

        self.assertEqual(gate.load_gate(g["gate_id"])["note"], "check the migration first")
        self.assertEqual(self.inbox_names(), [])

    def test_a_free_text_approve_decides_and_skips_the_inbox(self):
        g = self.open_gate()
        self.transport.batches = [[self.text("approve: ship it", update_id=620)]]
        self.listener.run_once(timeout=0)
        loaded = gate.load_gate(g["gate_id"])
        self.assertEqual((loaded["status"], loaded["note"]), ("approved", "ship it"))
        self.assertEqual(self.inbox_names(), [])

    def test_a_stranger_is_not_recorded(self):
        self.transport.batches = [[self.text("hello bot", update_id=630, user_id=999)]]
        self.listener.run_once(timeout=0)
        self.assertEqual(self.inbox_names(), [])
        self.assertEqual(self.listener._offset(), 631)


class LoopTests(ListenerTestCase):
    def test_a_409_exits_with_the_dedicated_conflict_code(self):
        """A second getUpdates reader exists. That is a deployment mistake, not
        weather — crash visibly, with an exit code nothing else uses, so
        doctor.sh can say "second reader" rather than "crashed"."""
        self.transport.fail_get_updates = TelegramConflict("Telegram 409 Conflict")
        self.assertEqual(self.listener.run(timeout=0), 3)
        self.assertEqual(listen.EXIT_CONFLICT, 3)
        self.assertEqual(self.sleeps, [], "a 409 was retried")

    def _stop_after_one_backoff(self):
        def sleep(seconds):
            self.sleeps.append(seconds)
            self.listener.stopping = True

        self.listener.sleep = sleep

    def test_a_cut_off_response_backs_off_instead_of_crashing(self):
        self.transport.fail_get_updates = http.client.IncompleteRead(b'{"ok": true, "resu')
        self._stop_after_one_backoff()
        self.assertEqual(self.listener.run(timeout=0), 0)
        self.assertEqual(len(self.sleeps), 1)

    def test_a_garbled_response_backs_off_instead_of_crashing(self):
        self.transport.fail_get_updates = json.JSONDecodeError("Expecting value", "<html>", 0)
        self._stop_after_one_backoff()
        self.assertEqual(self.listener.run(timeout=0), 0)
        self.assertEqual(len(self.sleeps), 1)

    def test_a_dropped_connection_backs_off_instead_of_crashing(self):
        self.transport.fail_get_updates = ConnectionResetError("Connection reset by peer")
        self._stop_after_one_backoff()
        self.assertEqual(self.listener.run(timeout=0), 0)
        self.assertEqual(len(self.sleeps), 1)

    def test_telegram_unreachable_backs_off_with_jitter_and_keeps_going(self):
        self.transport.fail_get_updates = TelegramError("Could not reach Telegram: timed out")

        def sleep(seconds):
            self.sleeps.append(seconds)
            if len(self.sleeps) == 5:
                self.listener.stopping = True

        self.listener.sleep = sleep
        self.assertEqual(self.listener.run(timeout=0), 0)
        self.assertTrue(4.5 <= self.sleeps[0] <= 5.5, self.sleeps)
        self.assertTrue(all(b >= a for a, b in zip(self.sleeps, self.sleeps[1:])), self.sleeps)
        self.assertLessEqual(max(self.sleeps), 66)

    def test_pending_decisions_are_retried_even_while_telegram_is_down(self):
        g = self.open_gate()
        self.linear.fail_issue_update = SystemExit("linear-gql: 500")
        self.transport.batches = [[self.tap(g)]]
        self.listener.run_once(timeout=0)
        self.linear.fail_issue_update = None
        self.transport.fail_get_updates = TelegramError("Could not reach Telegram")

        with self.assertRaises(TelegramError), self.later(minutes=1):
            self.listener.run_once(timeout=0)

        self.assertEqual(gate.load_gate(g["gate_id"])["status"], "approved")

    def test_a_heartbeat_is_written_every_iteration(self):
        self.listener.run_once(timeout=0)
        self.assertTrue(self.listener.heartbeat_path.is_file())

    def test_when_main_moves_it_fetches_resets_and_reexecs_in_process(self):
        """Not by exiting: every container exit bumps restartCount for good and
        is subject to the kubelet's restart backoff (SB-951 review)."""
        calls = []

        class Reexecuted(Exception):
            pass

        def reexec():
            calls.append("reexec")
            raise Reexecuted()

        listener = listen.Listener(
            self.gk,
            start_sha="aaaa111",
            ls_remote=lambda: "bbbb222",
            fetch_reset=lambda: calls.append("fetch+reset") or "bbbb222",
            reexec=reexec,
            update_every=0,
            sleep=self.sleeps.append,
        )
        with self.assertRaises(Reexecuted):
            listener.run(timeout=0)  # never returns an exit code on this path
        self.assertEqual(calls, ["fetch+reset", "reexec"])
        self.assertEqual(len(self.transport.offsets), 1, "it did not finish the batch in hand first")

    def test_a_failed_fetch_keeps_the_current_code_running_and_retries_next_check(self):
        fetches, reexecs = [], []

        def fetch_reset():
            fetches.append(1)
            if len(fetches) == 3:
                listener.stopping = True
            raise subprocess.CalledProcessError(128, ["git", "fetch"])

        listener = listen.Listener(
            self.gk,
            start_sha="aaaa111",
            ls_remote=lambda: "bbbb222",
            fetch_reset=fetch_reset,
            reexec=lambda: reexecs.append(1),
            update_every=0,
        )
        self.assertEqual(listener.run(timeout=0), 0)
        self.assertEqual((len(fetches), reexecs), (3, []))
        self.assertEqual(len(self.transport.offsets), 3, "the listener stopped listening while the update failed")

    def test_it_keeps_listening_while_main_has_not_moved(self):
        checks = []

        def ls_remote():
            checks.append(1)
            if len(checks) == 3:
                listener.stopping = True
            return "aaaa111"

        reexecs = []
        listener = listen.Listener(
            self.gk, start_sha="aaaa111", ls_remote=ls_remote, update_every=0, reexec=lambda: reexecs.append(1)
        )
        self.assertEqual(listener.run(timeout=0), 0)
        self.assertEqual((len(checks), reexecs), (3, []))

    def test_github_unreachable_does_not_stop_the_listener(self):
        def ls_remote():
            raise OSError("could not resolve github.com")

        listener = listen.Listener(self.gk, start_sha="aaaa111", ls_remote=ls_remote)
        self.assertIsNone(listener.moved_to())

    def test_sigterm_during_the_long_poll_stops_promptly(self):
        listener = self.listener

        class Blocking(type(self.transport)):
            def get_updates(self, offset, timeout, allowed_updates):
                listener.request_stop()  # the signal lands mid-wait
                raise AssertionError("request_stop did not interrupt the long poll")

        self.gk.transport = Blocking()
        self.assertEqual(listener.run(timeout=25), 0)


if __name__ == "__main__":
    unittest.main()
