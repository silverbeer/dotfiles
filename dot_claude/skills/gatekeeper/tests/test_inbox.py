"""inbox.py: the Maildir seam between the listener and the PO chat (SB-951).

Plain files in a tempdir — no fakes needed.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import inbox  # noqa: E402


def message(text="what's at risk this cycle?", message_id=11, user_id=42):
    return {
        "message_id": message_id,
        "from": {"id": user_id},
        "chat": {"id": user_id, "type": "private"},
        "date": 1789000000,
        "text": text,
    }


class InboxTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "inbox" / "telegram"

    def names(self, sub):
        return sorted(p.name for p in (self.root / sub).glob("*.json"))


class RecordTests(InboxTestCase):
    def test_record_lands_in_new_with_the_schema_and_leaves_nothing_in_tmp(self):
        path = inbox.record(message(), 600, self.root)

        self.assertEqual(path, self.root / "new" / "600.json")
        self.assertEqual(self.names("tmp"), [])
        rec = json.loads(path.read_text())
        self.assertEqual(rec["schema"], "gatekeeper.inbox/1")
        self.assertEqual(
            {k: rec[k] for k in ("update_id", "message_id", "chat_id", "from_id", "date", "text")},
            {
                "update_id": 600,
                "message_id": 11,
                "chat_id": 42,
                "from_id": 42,
                "date": 1789000000,
                "text": "what's at risk this cycle?",
            },
        )
        self.assertIn("received_at", rec)

    def test_a_redelivered_update_is_not_recorded_again_wherever_it_has_got_to(self):
        """Telegram redelivers anything unacked. The PO must not see a message
        twice because it had already been claimed, or even handled."""
        inbox.record(message(), 600, self.root)
        self.assertIsNone(inbox.record(message(), 600, self.root))
        self.assertEqual(self.names("new"), ["600.json"])

        claimed = inbox.claim_next(self.root)
        self.assertIsNone(inbox.record(message(), 600, self.root))
        inbox.complete(claimed)
        self.assertIsNone(inbox.record(message(), 600, self.root))

        self.assertEqual((self.names("new"), self.names("cur"), self.names("done")), ([], [], ["600.json"]))

    def test_a_stale_tmp_file_does_not_block_the_redelivery(self):
        """A crash between write and rename leaves tmp behind and no ack. If tmp
        counted as recorded, the redelivery would be skipped and acked — lost."""
        inbox._root(self.root)
        (self.root / "tmp" / "600.json").write_text("{partial")
        self.assertIsNotNone(inbox.record(message(), 600, self.root))
        self.assertEqual(self.names("new"), ["600.json"])


class ClaimTests(InboxTestCase):
    def test_claim_is_exclusive(self):
        inbox.record(message(), 600, self.root)
        first = inbox.claim_next(self.root)
        self.assertEqual(first, self.root / "cur" / "600.json")
        self.assertIsNone(inbox.claim_next(self.root), "one message was claimed twice")

    def test_a_message_another_consumer_took_first_is_skipped_not_an_error(self):
        inbox.record(message(), 600, self.root)
        inbox.record(message(), 601, self.root)
        # the other consumer wins the race for 600 between our glob and rename
        real_rename = inbox.os.rename
        calls = []

        def racing_rename(src, dst):
            calls.append(Path(src).name)
            if len(calls) == 1:
                real_rename(src, self.root / "cur" / Path(src).name)
            return real_rename(src, dst)

        inbox.os.rename = racing_rename
        try:
            got = inbox.claim_next(self.root)
        finally:
            inbox.os.rename = real_rename
        self.assertEqual(got.name, "601.json")

    def test_oldest_first_by_update_id_not_by_string(self):
        for uid in (10, 9, 100):
            inbox.record(message(), uid, self.root)
        order = [inbox.claim_next(self.root).name for _ in range(3)]
        self.assertEqual(order, ["9.json", "10.json", "100.json"])

    def test_requeue_puts_it_back_for_the_next_claim(self):
        inbox.record(message(), 600, self.root)
        claimed = inbox.claim_next(self.root)
        self.assertEqual(inbox.requeue(claimed), self.root / "new" / "600.json")
        self.assertEqual(inbox.claim_next(self.root).name, "600.json")

    def test_complete_moves_it_to_done(self):
        inbox.record(message(), 600, self.root)
        done = inbox.complete(inbox.claim_next(self.root))
        self.assertEqual(done, self.root / "done" / "600.json")
        self.assertIsNone(inbox.claim_next(self.root))

    def test_empty_inbox_claims_nothing(self):
        self.assertIsNone(inbox.claim_next(self.root))


if __name__ == "__main__":
    unittest.main()
