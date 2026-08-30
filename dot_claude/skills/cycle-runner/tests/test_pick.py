"""pick.py: driven x estimate x label policy, ready-queue blocker detection,
and the priority/oldest sort (SB-929).

The pure functions (driven_label, is_ready, policy_ok, ready_queue,
_priority_rank) take plain dicts shaped like Q_READY's nodes and never call
`gql` — no network for any of that. One end-to-end test drives `main()`
through a monkeypatched `pick.gql` to prove the query wiring and the stdout
contract, still without ever touching a socket (same monkeypatch shape
gatekeeper's test_gate.py uses for `gate.gql`).
"""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pick  # noqa: E402


def issue(
    identifier="SB-1",
    driven="driven:agent-supervised",
    estimate=None,
    extra_labels=(),
    state_type="unstarted",
    state_name="Todo",
    blockers=(),
    priority=0,
    created_at="2026-01-01T00:00:00.000Z",
):
    """One Q_READY-shaped node. `driven=None` omits the driven:* label
    entirely — the "no label at all" case."""
    labels = [{"name": driven}] if driven else []
    labels += [{"name": n} for n in extra_labels]
    return {
        "identifier": identifier,
        "title": "t",
        "estimate": estimate,
        "url": f"https://linear.app/silverbeer/issue/{identifier}",
        "state": {"name": state_name, "type": state_type},
        "labels": {"nodes": labels},
        "relations": {"nodes": []},
        "inverseRelations": {
            "nodes": [
                {"type": "blocks", "issue": {"identifier": b[0], "state": {"name": b[1]}}} for b in blockers
            ]
        },
        "priority": priority,
        "createdAt": created_at,
    }


class DrivenLabelTests(unittest.TestCase):
    def test_returns_the_driven_label(self):
        self.assertEqual(pick.driven_label(issue(driven="driven:agent-auto")), "driven:agent-auto")

    def test_none_when_no_driven_label_at_all(self):
        self.assertIsNone(pick.driven_label(issue(driven=None)))

    # Kill-switch: driven:human must win no matter where it sits in the list.
    def test_human_wins_when_listed_after_agent_auto(self):
        i = issue(driven="driven:agent-auto", extra_labels=["driven:human"])
        self.assertEqual(pick.driven_label(i), "driven:human")

    def test_human_wins_when_listed_before_agent_supervised(self):
        i = issue(driven="driven:human", extra_labels=["driven:agent-supervised"])
        self.assertEqual(pick.driven_label(i), "driven:human")


class IsReadyTests(unittest.TestCase):
    """Ready = no `blocks` relation whose target is still open."""

    def test_ready_with_no_blockers(self):
        self.assertTrue(pick.is_ready(issue()))

    def test_not_ready_with_an_open_blocker(self):
        self.assertFalse(pick.is_ready(issue(blockers=[("SB-9", "In Progress")])))

    # NEGATIVE pair: a closed blocker, either terminal state, does not block.
    def test_ready_once_the_blocker_is_done(self):
        self.assertTrue(pick.is_ready(issue(blockers=[("SB-9", "Done")])))

    def test_ready_once_the_blocker_is_canceled(self):
        self.assertTrue(pick.is_ready(issue(blockers=[("SB-9", "Canceled")])))

    def test_one_open_blocker_among_several_closed_still_blocks(self):
        self.assertFalse(pick.is_ready(issue(blockers=[("SB-8", "Done"), ("SB-9", "Todo")])))


class PolicyMatrixTests(unittest.TestCase):
    """driven x estimate x label — the exact matrix the ticket asks for."""

    def test_agent_supervised_is_always_eligible_regardless_of_estimate(self):
        ok, warn = pick.policy_ok(issue(estimate=8), "driven:agent-supervised")
        self.assertTrue(ok)
        self.assertIsNone(warn)

    def test_agent_auto_estimate_2_and_adhoc_is_eligible(self):
        i = issue(driven="driven:agent-auto", estimate=2, extra_labels=["adhoc"])
        ok, warn = pick.policy_ok(i, "driven:agent-auto")
        self.assertTrue(ok)
        self.assertIsNone(warn)

    def test_agent_auto_estimate_1_and_chore_is_eligible(self):
        i = issue(driven="driven:agent-auto", estimate=1, extra_labels=["chore"])
        ok, warn = pick.policy_ok(i, "driven:agent-auto")
        self.assertTrue(ok)
        self.assertIsNone(warn)

    # NEGATIVE: estimate above 2 is a POLICY VIOLATION, not a silent skip.
    def test_agent_auto_estimate_3_is_a_policy_violation(self):
        i = issue(driven="driven:agent-auto", estimate=3, extra_labels=["adhoc"])
        ok, warn = pick.policy_ok(i, "driven:agent-auto")
        self.assertFalse(ok)
        self.assertIn("POLICY VIOLATION", warn)
        self.assertIn(i["identifier"], warn)

    def test_agent_auto_without_adhoc_or_chore_is_a_policy_violation(self):
        i = issue(driven="driven:agent-auto", estimate=1, extra_labels=["feature"])
        ok, warn = pick.policy_ok(i, "driven:agent-auto")
        self.assertFalse(ok)
        self.assertIn("POLICY VIOLATION", warn)

    def test_agent_auto_with_no_estimate_is_a_policy_violation(self):
        i = issue(driven="driven:agent-auto", estimate=None, extra_labels=["adhoc"])
        ok, warn = pick.policy_ok(i, "driven:agent-auto")
        self.assertFalse(ok)
        self.assertIn("POLICY VIOLATION", warn)

    # NEGATIVE: driven:human never warns — it is expected, not a violation.
    def test_driven_human_is_never_eligible_and_never_warned(self):
        ok, warn = pick.policy_ok(issue(), "driven:human")
        self.assertFalse(ok)
        self.assertIsNone(warn)


class ReadyQueueTests(unittest.TestCase):
    def test_unlabeled_issue_is_skipped(self):
        self.assertEqual(pick.ready_queue([issue(driven=None)]), [])

    def test_human_driven_is_skipped(self):
        self.assertEqual(pick.ready_queue([issue(driven="driven:human")]), [])

    # Kill-switch, at the ready-queue level: driven:human alongside an
    # agent-* label must still resolve to "skip", never "run".
    def test_human_alongside_agent_auto_is_still_skipped(self):
        i = issue(driven="driven:agent-auto", extra_labels=["driven:human", "adhoc"], estimate=1)
        self.assertEqual(pick.ready_queue([i]), [])

    def test_backlog_state_type_is_excluded(self):
        self.assertEqual(pick.ready_queue([issue(state_type="backlog")]), [])

    def test_triage_state_type_is_excluded(self):
        self.assertEqual(pick.ready_queue([issue(state_type="triage")]), [])

    def test_started_state_type_is_included(self):
        i = issue(state_type="started")
        self.assertEqual([x["identifier"] for x in pick.ready_queue([i])], [i["identifier"]])

    def test_open_blocker_excludes_from_the_queue(self):
        self.assertEqual(pick.ready_queue([issue(blockers=[("SB-9", "Todo")])]), [])

    def test_closed_blocker_does_not_exclude(self):
        i = issue(blockers=[("SB-9", "Done")])
        self.assertEqual([x["identifier"] for x in pick.ready_queue([i])], [i["identifier"]])

    def test_policy_violating_agent_auto_is_skipped_from_the_queue(self):
        i = issue(driven="driven:agent-auto", estimate=5)
        self.assertEqual(pick.ready_queue([i]), [])

    def test_sorted_by_priority_then_oldest(self):
        urgent = issue(identifier="SB-2", priority=1, created_at="2026-01-05T00:00:00.000Z")
        no_priority_old = issue(identifier="SB-1", priority=0, created_at="2026-01-01T00:00:00.000Z")
        high_newer = issue(identifier="SB-3", priority=2, created_at="2026-01-02T00:00:00.000Z")
        queue = pick.ready_queue([no_priority_old, high_newer, urgent])
        self.assertEqual([i["identifier"] for i in queue], ["SB-2", "SB-3", "SB-1"])

    def test_tie_on_priority_breaks_on_oldest_first(self):
        newer = issue(identifier="SB-2", priority=2, created_at="2026-01-05T00:00:00.000Z")
        older = issue(identifier="SB-1", priority=2, created_at="2026-01-01T00:00:00.000Z")
        queue = pick.ready_queue([newer, older])
        self.assertEqual([i["identifier"] for i in queue], ["SB-1", "SB-2"])


class MainEndToEndTests(unittest.TestCase):
    """One offline pass through main(), monkeypatching pick.gql — proves the
    query wiring and stdout contract without ever hitting the network."""

    def setUp(self):
        self._old_gql = pick.gql
        self.addCleanup(lambda: setattr(pick, "gql", self._old_gql))

    def test_prints_the_winning_ticket_and_asks_for_all_three_driven_labels(self):
        queried = {}

        def fake_gql(query, variables=None):
            queried["query"] = query
            queried["variables"] = variables
            return {"issues": {"nodes": [issue(identifier="SB-42")]}}

        pick.gql = fake_gql
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pick.main()
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().strip(), "SB-42")
        self.assertEqual(
            set(queried["variables"]["labels"]),
            {"driven:human", "driven:agent-supervised", "driven:agent-auto"},
        )

    def test_prints_nothing_when_the_queue_is_empty(self):
        pick.gql = lambda query, variables=None: {"issues": {"nodes": []}}
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pick.main()
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
