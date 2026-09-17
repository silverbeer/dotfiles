"""cycle_state.py in po-agent/scripts — the JSON /cycle reads (SB-1087).

Every number the PO agent quotes comes from this document, so the things
pinned here are the ones a conversation would get wrong silently: a plan that
exceeds capacity, velocity read from drifted membership, a stalled ticket that
looks fresh because Linear's rollover touched it, a gate aged from the wrong
label, and a schema that drifts under the standup and runner-feed readers.

Offline: gql is a tripwire in every module that could reach Linear, and the
end-to-end tests drive build() through a fake Linear that counts queries.
"""

import datetime
import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

import _cycle_fixtures as fx
from _load import load_module

cs = load_module("cycle_state", "po-agent", "cycle_state.py")

UTC = datetime.timezone.utc
MID_DAY_AFTER = fx.at("2026-08-16T12:00:00Z")   # cycle 4 active, day 0, 7 days left
BOUNDARY = fx.at("2026-08-16T04:00:00Z")
ONE_SECOND_BEFORE = fx.at("2026-08-16T03:59:59Z")


def _tripwire(*args, **kwargs):
    raise AssertionError(f"a real gql call was attempted during an offline test: {args!r}")


def setUpModule():
    for mod in (cs, cs.cycles, cs.pick, sys.modules["linear_api"]):
        p = mock.patch.object(mod, "gql", side_effect=_tripwire)
        p.start()
        unittest.addModuleCleanup(p.stop)


def _cycles():
    return fx.cycles() + [fx.c5()]


# ------------------------------------------------------------ selection

class NextCycle(unittest.TestCase):
    def setUp(self):
        self.cs = _cycles()

    def test_next_of_three_is_four_starting_at_the_exclusive_end(self):
        c3 = fx.by_number(self.cs, 3)
        nxt = cs.next_cycle(self.cs, c3)
        self.assertEqual(nxt["number"], 4)
        self.assertEqual(nxt["startsAt"], c3["endsAt"])

    # NEGATIVE: the last cycle has no next.
    def test_last_cycle_has_none(self):
        self.assertIsNone(cs.next_cycle(self.cs, fx.by_number(self.cs, 5)))

    def test_a_gap_returns_the_earliest_later_cycle(self):
        gapped = [c for c in self.cs if c["number"] != 4]
        self.assertEqual(cs.next_cycle(gapped, fx.by_number(gapped, 3))["number"], 5)

    def test_order_of_the_input_list_does_not_matter(self):
        self.cs.reverse()
        self.assertEqual(cs.next_cycle(self.cs, fx.by_number(self.cs, 2))["number"], 3)


class ResolveCycle(unittest.TestCase):
    def setUp(self):
        self.cs = _cycles()

    def _n(self, mode, now):
        return cs.resolve_cycle(self.cs, mode, now)[0]["number"]

    def test_current_at_the_boundary_is_the_cycle_that_just_started(self):
        self.assertEqual(self._n("current", BOUNDARY), 4)

    # NEGATIVE: one second before the boundary nothing has rolled over.
    def test_one_second_before_the_boundary_current_is_three_and_next_is_four(self):
        self.assertEqual(self._n("current", ONE_SECOND_BEFORE), 3)
        self.assertEqual(self._n("next", ONE_SECOND_BEFORE), 4)

    def test_next_at_the_boundary_is_five(self):
        cycle, reason = cs.resolve_cycle(self.cs, "next", BOUNDARY)
        self.assertEqual(cycle["number"], 5)
        self.assertIn("after cycle 4", reason)

    def test_a_number_selects_that_cycle_with_no_plan_reason(self):
        self.assertEqual(cs.resolve_cycle(self.cs, "2", BOUNDARY), (fx.by_number(self.cs, 2), None))

    def test_current_has_no_plan_reason(self):
        self.assertIsNone(cs.resolve_cycle(self.cs, "current", BOUNDARY)[1])

    # NEGATIVE: no cycle after the last is an error, not the last cycle again.
    def test_next_with_nothing_scheduled_raises(self):
        with self.assertRaisesRegex(LookupError, "no cycle after cycle 5"):
            cs.resolve_cycle(self.cs, "next", fx.at("2026-08-25T00:00:00Z"))

    def test_unknown_number_raises(self):
        with self.assertRaisesRegex(LookupError, "cycle 99 not found"):
            cs.resolve_cycle(self.cs, "99", BOUNDARY)


class PlanTarget(unittest.TestCase):
    """/cycle plan plans the active cycle while it is unstamped and at most two
    days in; otherwise the next one."""

    def setUp(self):
        self.cs = _cycles()
        self.c4 = fx.by_number(self.cs, 4)
        self.c4["description"] = None

    def _target(self, now):
        return cs.resolve_cycle(self.cs, "plan-target", now)

    def test_unstamped_active_cycle_on_day_two_is_the_target(self):
        cycle, reason = self._target(fx.at("2026-08-18T12:00:00Z"))
        self.assertEqual(cycle["number"], 4)
        self.assertIn("2 day(s) in", reason)

    def test_unstamped_active_cycle_on_its_first_day_is_the_target(self):
        self.assertEqual(self._target(MID_DAY_AFTER)[0]["number"], 4)

    # NEGATIVE: day three is too late to plan the running cycle.
    def test_day_three_targets_the_next_cycle(self):
        cycle, reason = self._target(fx.at("2026-08-19T12:00:00Z"))
        self.assertEqual(cycle["number"], 5)
        self.assertIn("more than 2 days in", reason)

    # NEGATIVE: a stamped cycle (skipped counts) is never re-planned.
    def test_a_stamped_active_cycle_targets_the_next_cycle(self):
        for desc in (fx.C4_DESCRIPTION, "Planning: planned"):
            with self.subTest(desc=desc):
                self.c4["description"] = desc
                cycle, reason = self._target(MID_DAY_AFTER)
                self.assertEqual(cycle["number"], 5)
                self.assertIn("already stamped", reason)

    # NEGATIVE: past the grace period with nothing scheduled after the active
    # cycle, plan-target must raise a clear error rather than fall back to the
    # active cycle it just decided NOT to target.
    def test_no_next_cycle_past_the_grace_period_raises(self):
        c5 = fx.by_number(self.cs, 5)
        c5["description"] = None
        with self.assertRaisesRegex(LookupError, "no cycle after cycle 5"):
            self._target(fx.at("2026-08-27T12:00:00Z"))  # cycle 5, day 4 (> 2 days in)

    # NEGATIVE: same, but because the active cycle is already stamped rather
    # than past the grace window.
    def test_no_next_cycle_for_a_stamped_active_cycle_raises(self):
        c5 = fx.by_number(self.cs, 5)
        c5["description"] = "Planning: planned"
        with self.assertRaisesRegex(LookupError, "no cycle after cycle 5"):
            self._target(fx.at("2026-08-24T12:00:00Z"))  # cycle 5, day 1 but stamped


# ---------------------------------------------------------------- AC

class HasAc(unittest.TestCase):
    def test_headings_bold_and_checkboxes_count(self):
        for d in ("## Acceptance criteria\n- works", "# Definition of Done\nx", "### Exit criteria",
                  "intro\n**Done when** it ships", "**Acceptance:** yes", "  ## acceptance",
                  "- [ ] one thing", "* [x] done thing", "text\n  - [X] indented"):
            with self.subTest(d=d):
                self.assertTrue(cs.has_ac(d))

    # NEGATIVE: prose that mentions acceptance, inline brackets, nothing at all.
    def test_prose_and_empty_do_not_count(self):
        for d in (None, "", "The acceptance criteria are obvious.", "we are done when it works",
                  "see [ ] inline", "- [] no space", "Acceptance criteria: TBD"):
            with self.subTest(d=d):
                self.assertFalse(cs.has_ac(d))


# ------------------------------------------------------------ activity

class LastActivity(unittest.TestCase):
    NOW = fx.at("2026-09-16T12:00:00Z")

    def _row(self, **kw):
        base = fx.issue(1, 3, state="started", created="2026-09-01T00:00:00.000Z")
        return cs.issue_row(fx.rich(base, **kw), now=self.NOW, carry_in_ids=set())

    def test_later_of_updated_and_started_wins(self):
        r = self._row(updated="2026-09-05T00:00:00.000Z", started="2026-09-14T00:00:00.000Z")
        self.assertEqual((r["last_activity"], r["activity_source"]), ("2026-09-14T00:00:00.000Z", "startedAt"))
        r = self._row(updated="2026-09-15T00:00:00.000Z", started="2026-09-10T00:00:00.000Z")
        self.assertEqual(r["activity_source"], "updatedAt")
        self.assertEqual(r["days_since_activity"], 1)

    # The SB-1010 shape: rolled into a new cycle on 09-13 (a history entry),
    # untouched since 09-05. History must not make it look active.
    def test_rollover_history_does_not_count_as_movement(self):
        r = self._row(updated="2026-09-05T10:00:00.000Z", started="2026-09-02T00:00:00.000Z",
                      history=[("2026-09-13T04:00:30.000Z", [])])
        self.assertEqual(r["days_since_activity"], 11)
        risk = cs.at_risk([r], days_left=4, at_risk_days=3, stale_days=3)
        self.assertEqual([s["identifier"] for s in risk["stalled"]], ["SB-1"])

    def test_no_timestamps_is_none(self):
        self.assertEqual(cs.last_activity({"updatedAt": None}), (None, None))


class AtRisk(unittest.TestCase):
    NOW = fx.at("2026-09-16T12:00:00Z")

    def _rows(self):
        def row(n, state, *, labels=("DOT",), updated="2026-09-16T00:00:00.000Z", est=3):
            base = fx.issue(n, est, state=state, labels=labels)
            return cs.issue_row(fx.rich(base, updated=updated), now=self.NOW, carry_in_ids=set())
        return [
            row(1, "unstarted"),
            row(2, "backlog"),
            row(3, "triage"),
            row(4, "unstarted", labels=("DOT", "adhoc")),
            row(5, "started"),
            row(6, "completed"),
            row(7, "canceled"),
            row(8, "started", updated="2026-09-13T12:00:00.000Z"),   # exactly 3 days
            row(9, "started", updated="2026-09-13T12:00:01.000Z"),   # 1s short of 3 days
            row(10, "unstarted", updated="2026-08-01T00:00:00.000Z"),  # old, but not started
        ]

    def test_not_started_is_planned_open_unstarted_work_when_days_are_short(self):
        risk = cs.at_risk(self._rows(), days_left=3, at_risk_days=3, stale_days=3)
        self.assertEqual([r["identifier"] for r in risk["not_started"]], ["SB-1", "SB-2", "SB-3", "SB-10"])
        self.assertEqual(set(risk["not_started"][0]), {"identifier", "title", "estimate", "priority"})

    # NEGATIVE: four days left with a threshold of three is not at risk yet
    # (cycle 8 on 2026-09-16), and a cycle with no days_left never is.
    def test_not_started_is_empty_with_more_days_left_or_none(self):
        for days_left in (4, None):
            with self.subTest(days_left=days_left):
                self.assertEqual(cs.at_risk(self._rows(), days_left=days_left, at_risk_days=3,
                                            stale_days=3)["not_started"], [])

    def test_stalled_is_started_work_idle_for_at_least_stale_days(self):
        risk = cs.at_risk(self._rows(), days_left=5, at_risk_days=3, stale_days=3)
        self.assertEqual(risk["stalled"], [
            {"identifier": "SB-8", "title": "issue 8", "state": "In Progress", "days_since_activity": 3}])


# ---------------------------------------------------------------- gates

class WaitingOnHuman(unittest.TestCase):
    NOW = fx.at("2026-09-16T12:00:00Z")
    C8 = {"id": "cycle-8-uuid", "number": 8}

    def _gated(self, n, labels, *, state="unstarted", history=(), cycle=None, updated="2026-09-15T00:00:00.000Z"):
        return fx.rich(fx.issue(n, 1, state=state, labels=labels), history=history, cycle=cycle, updated=updated)

    def test_age_is_from_the_newest_entry_adding_the_current_label(self):
        # SB-990's real history, newest first.
        i = self._gated(990, ("DOT", "gate:needs-human"), updated="2026-09-12T15:59:03.869Z", history=[
            ("2026-09-12T14:30:03.582Z", ["gate:needs-human"]),
            ("2026-09-09T14:05:26.164Z", ["gate:awaiting-approval"]),
            ("2026-09-07T12:36:27.428Z", ["gate:awaiting-approval"]),
        ])
        out = cs.waiting_on_human([i], cycle_id=self.C8["id"], now=self.NOW)
        self.assertEqual(out, [{
            "identifier": "SB-990", "title": "issue 990", "url": "https://linear.app/silverbeer/issue/SB-990",
            "label": "gate:needs-human", "since": "2026-09-12T14:30:03.582Z", "age_days": 3,
            "age_source": "history", "in_cycle": False,
        }])

    def test_history_order_does_not_matter(self):
        i = self._gated(1, ("gate:awaiting-approval",), history=[
            ("2026-09-01T00:00:00.000Z", ["gate:awaiting-approval"]),
            ("2026-09-10T00:00:00.000Z", ["gate:awaiting-approval"]),
        ])
        self.assertEqual(cs.waiting_on_human([i], cycle_id="x", now=self.NOW)[0]["since"],
                         "2026-09-10T00:00:00.000Z")

    # NEGATIVE: history that never added the current label falls back to updatedAt, and says so.
    def test_falls_back_to_updated_at(self):
        i = self._gated(2, ("gate:awaiting-approval",), history=[("2026-09-14T00:00:00.000Z", ["gate:needs-human"])],
                        updated="2026-09-11T12:00:00.000Z")
        out = cs.waiting_on_human([i], cycle_id="x", now=self.NOW)[0]
        self.assertEqual((out["since"], out["age_source"], out["age_days"]),
                         ("2026-09-11T12:00:00.000Z", "updatedAt", 5))

    # NEGATIVE: SB-964 is Done yet still carries gate:awaiting-approval.
    def test_done_and_canceled_are_not_waiting(self):
        gated = [self._gated(964, ("gate:awaiting-approval",), state="completed"),
                 self._gated(965, ("gate:needs-human",), state="canceled")]
        self.assertEqual(cs.waiting_on_human(gated, cycle_id="x", now=self.NOW), [])

    # NEGATIVE: resolved gates are not pending.
    def test_resolved_gate_labels_are_not_waiting(self):
        gated = [self._gated(3, ("gate:approved",)), self._gated(4, ("gate:rejected",))]
        self.assertEqual(cs.waiting_on_human(gated, cycle_id="x", now=self.NOW), [])

    def test_in_cycle_flag_and_oldest_first(self):
        gated = [self._gated(5, ("gate:needs-human",), cycle=self.C8, updated="2026-09-15T00:00:00.000Z"),
                 self._gated(6, ("gate:needs-human",), updated="2026-09-10T00:00:00.000Z")]
        out = cs.waiting_on_human(gated, cycle_id=self.C8["id"], now=self.NOW)
        self.assertEqual([(r["identifier"], r["in_cycle"]) for r in out], [("SB-6", False), ("SB-5", True)])


class NotReady(unittest.TestCase):
    def test_open_tickets_missing_estimate_or_ac(self):
        now = fx.at("2026-09-16T12:00:00Z")
        rows = [cs.issue_row(fx.rich(fx.issue(n, est, state=st), description=d), now=now, carry_in_ids=set())
                for n, est, st, d in [
                    (1, None, "unstarted", None),
                    (2, 3, "started", "prose only"),
                    (3, None, "unstarted", "## Acceptance\n- x"),
                    (4, 2, "unstarted", "- [ ] x"),
                    (5, None, "completed", None),
                    (6, None, "canceled", None),
                ]]
        self.assertEqual(cs.not_ready(rows), [
            {"identifier": "SB-1", "title": "issue 1", "missing": ["estimate", "acceptance_criteria"]},
            {"identifier": "SB-2", "title": "issue 2", "missing": ["acceptance_criteria"]},
            {"identifier": "SB-3", "title": "issue 3", "missing": ["estimate"]},
        ])

    # A 0 estimate is deliberate (superseded/duplicate), not missing.
    def test_zero_estimate_is_not_missing(self):
        row = cs.issue_row(fx.rich(fx.issue(1, 0, state="unstarted"), description="- [x] y"),
                           now=MID_DAY_AFTER, carry_in_ids=set())
        self.assertEqual(cs.not_ready([row]), [])


# ------------------------------------------------------------ velocity

class Velocity(unittest.TestCase):
    def setUp(self):
        self.cs = _cycles()

    def _data(self):
        return {"cycle-2-uuid": ([], []), "cycle-3-uuid": (fx.c3_members(), fx.c3_uncompleted())}

    def test_window_is_closed_cycles_ending_before_the_reference_starts(self):
        for ref, window, want in ((4, 3, [2, 3]), (4, 1, [3]), (3, 3, [2]), (2, 3, []), (4, 0, [])):
            with self.subTest(ref=ref, window=window):
                got = cs.closed_before(self.cs, fx.by_number(self.cs, ref), window)
                self.assertEqual([c["number"] for c in got], want)

    # NEGATIVE: cycle 4 ended by the clock but Linear never closed it; it has no
    # history totals and no carry-out, so it must not count.
    def test_an_unclosed_cycle_is_skipped(self):
        got = cs.closed_before(self.cs, fx.by_number(self.cs, 5), 3)
        self.assertEqual([c["number"] for c in got], [2, 3])

    def test_points_done_come_from_history_not_drifted_membership(self):
        window = cs.closed_before(self.cs, fx.by_number(self.cs, 4), 3)
        v, _ = cs.velocity(window, self._data(), window=3, now=MID_DAY_AFTER)
        self.assertEqual(v["cycles"], [
            {"number": 2, "points_done": 30, "issues_done": 9, "planned_points_done": 0, "adhoc_points_done": 0},
            {"number": 3, "points_done": 48, "issues_done": 11, "planned_points_done": 32, "adhoc_points_done": 14},
        ])

    def test_capacity_is_floor_of_mean_times_one_minus_pooled_adhoc_share(self):
        window = cs.closed_before(self.cs, fx.by_number(self.cs, 4), 3)
        v, warnings = cs.velocity(window, self._data(), window=3, now=MID_DAY_AFTER)
        # mean (30 + 48) / 2 = 39; adhoc share 14 / 46; 39 * 32/46 = 27.13
        self.assertEqual(v["mean_points_done"], 39.0)
        self.assertEqual(v["adhoc_share_points"], round(100 * 14 / 46))
        self.assertEqual(v["capacity_points"], 27)
        self.assertIn("= 27", v["formula"])
        self.assertIn("14/46", v["formula"])
        self.assertEqual(warnings, ["velocity uses 2 closed cycle(s), fewer than the window of 3"])

    # NEGATIVE: the share is pooled over points, not a mean of per-cycle shares.
    def test_adhoc_share_is_pooled_across_cycles(self):
        a = fx.make_cycle(20, "2026-01-01T05:00:00.000Z", "2026-01-08T05:00:00.000Z",
                          completed="2026-01-08T05:00:30.000Z",
                          issues=[2], done_issues=[2], scope=[10], done_scope=[10])
        b = fx.make_cycle(21, "2026-01-08T05:00:00.000Z", "2026-01-15T05:00:00.000Z",
                          completed="2026-01-15T05:00:30.000Z",
                          issues=[1], done_issues=[1], scope=[2], done_scope=[2])
        data = {
            a["id"]: ([fx.issue(1, 9), fx.issue(2, 1, labels=("adhoc",))], []),   # 10% adhoc
            b["id"]: ([fx.issue(3, 2, labels=("adhoc",))], []),                    # 100% adhoc
        }
        v, warnings = cs.velocity([a, b], data, window=2, now=MID_DAY_AFTER)
        # pooled 3/12 = 25% -> floor(6 * 0.75) = 4; a mean of shares (55%) would give 2
        self.assertEqual((v["adhoc_share_points"], v["capacity_points"]), (25, 4))
        self.assertEqual(warnings, [])

    # NEGATIVE: no closed cycles is no capacity, never zero or a guess.
    def test_no_closed_cycles_means_no_capacity(self):
        v, warnings = cs.velocity([], {}, window=3, now=MID_DAY_AFTER)
        self.assertIsNone(v["capacity_points"])
        self.assertIsNone(v["mean_points_done"])
        self.assertEqual(len(warnings), 1)


# ------------------------------------------------------------------ plan

def cand(n, est, *, priority=2, created="2026-08-01T00:00:00.000Z", blocks=(), state="unstarted", description=None):
    return fx.rich(fx.issue(n, est, state=state, labels=("DOT",), created=created),
                   priority=priority, blocks=blocks, description=description)


TARGET = fx.c5()


def _plan(capacity, pools, **kw):
    return cs.plan(TARGET, capacity, pools, **kw)[0]


class PlanFit(unittest.TestCase):
    # The bash harness breaks the fit comparison and asserts THIS test is
    # named in the failure.
    def test_fit_never_exceeds_capacity(self):
        p = _plan(5, [("backlog", [cand(1, 3), cand(2, 3), cand(3, 3)])])
        self.assertEqual(p["fits"], ["SB-1"])
        self.assertEqual(p["does_not_fit"], ["SB-2", "SB-3"])
        self.assertLessEqual(p["proposed_points"], p["capacity_points"])
        self.assertEqual(p["proposed_points"], 3)

    def test_an_exact_fit_fits(self):
        p = _plan(5, [("backlog", [cand(1, 2), cand(2, 3)])])
        self.assertEqual((p["fits"], p["proposed_points"]), (["SB-1", "SB-2"], 5))

    def test_filling_continues_past_a_ticket_that_does_not_fit(self):
        p = _plan(5, [("backlog", [cand(1, 3, created="2026-01-01T00:00:00Z"),
                                   cand(2, 5, created="2026-01-02T00:00:00Z"),
                                   cand(3, 2, created="2026-01-03T00:00:00Z")])])
        self.assertEqual((p["fits"], p["does_not_fit"]), (["SB-1", "SB-3"], ["SB-2"]))
        self.assertEqual([c["cumulative_points"] for c in p["candidates"]], [3, 3, 5])

    def test_carry_over_alone_exceeding_capacity_is_flagged_and_still_capped(self):
        p = _plan(5, [("carry_over", [cand(1, 4), cand(2, 4)]), ("backlog", [cand(3, 1, priority=1)])])
        self.assertTrue(p["carry_over_exceeds_capacity"])
        self.assertEqual(p["fits"], ["SB-1", "SB-3"])
        self.assertEqual(p["does_not_fit"], ["SB-2"])
        self.assertLessEqual(p["proposed_points"], 5)

    # NEGATIVE: carry-over that fits is not flagged.
    def test_carry_over_within_capacity_is_not_flagged(self):
        self.assertFalse(_plan(8, [("carry_over", [cand(1, 4), cand(2, 4)])])["carry_over_exceeds_capacity"])

    # NEGATIVE: an unestimated ticket never fits, however much room is left.
    def test_unestimated_never_fits_and_needs_an_estimate(self):
        p = _plan(100, [("backlog", [cand(1, None), cand(2, 0)])])
        self.assertEqual(p["needs_estimate"], ["SB-1"])
        self.assertEqual(p["fits"], ["SB-2"])
        self.assertNotIn("SB-1", p["does_not_fit"])

    def test_no_capacity_fits_nothing(self):
        p = _plan(None, [("backlog", [cand(1, 1)])])
        self.assertEqual((p["fits"], p["does_not_fit"]), ([], ["SB-1"]))
        self.assertFalse(p["carry_over_exceeds_capacity"])

    def test_exclude_refits_the_next_candidate(self):
        pools = [("backlog", [cand(1, 5, created="2026-01-01T00:00:00Z"), cand(2, 5, created="2026-01-02T00:00:00Z")])]
        self.assertEqual(_plan(5, pools)["fits"], ["SB-1"])
        p = _plan(5, pools, exclude={"SB-1"})
        self.assertEqual(p["fits"], ["SB-2"])
        self.assertNotIn("SB-1", [c["identifier"] for c in p["candidates"]])

    # NEGATIVE: done and canceled tickets are not candidates.
    def test_closed_tickets_are_not_candidates(self):
        p = _plan(10, [("carry_over", [cand(1, 1, state="completed"), cand(2, 1, state="canceled")])])
        self.assertEqual(p["candidates"], [])

    def test_target_and_reason(self):
        p = _plan(5, [], reason="next cycle after cycle 4")
        self.assertEqual(p["target"], {"number": 5, "id": "cycle-5-uuid", "starts_at": TARGET["startsAt"],
                                       "reason": "next cycle after cycle 4"})

    # Boundary: a cycle with no room left still cannot admit a real ticket, but
    # a genuinely zero-point one is an exact fit at zero.
    def test_zero_capacity_fits_only_zero_point_tickets(self):
        p = _plan(0, [("backlog", [cand(1, 0), cand(2, 1)])])
        self.assertEqual((p["fits"], p["does_not_fit"], p["proposed_points"]), (["SB-1"], ["SB-2"], 0))
        self.assertFalse(p["carry_over_exceeds_capacity"])

    # carry-over that is entirely unestimated cannot be summed, so it reads as
    # zero rather than exceeding capacity — every ticket still needs an
    # estimate before it can be planned at all.
    def test_all_carry_over_unestimated_is_not_flagged_and_all_need_estimates(self):
        p = _plan(5, [("carry_over", [cand(1, None), cand(2, None)])])
        self.assertFalse(p["carry_over_exceeds_capacity"])
        self.assertEqual(sorted(p["needs_estimate"]), ["SB-1", "SB-2"])
        self.assertEqual((p["fits"], p["proposed_points"]), ([], 0))

    # NEGATIVE: excluding an identifier that is not a candidate at all is a
    # no-op, not an error.
    def test_exclude_of_an_identifier_not_among_candidates_is_a_no_op(self):
        p = _plan(5, [("backlog", [cand(1, 3)])], exclude={"SB-999"})
        self.assertEqual(p["fits"], ["SB-1"])


class PlanOrder(unittest.TestCase):
    def _order(self, pools):
        return [c["identifier"] for c in _plan(100, pools)["candidates"]]

    def test_carry_over_comes_before_higher_priority_backlog(self):
        self.assertEqual(self._order([("carry_over", [cand(1, 1, priority=4)]),
                                      ("backlog", [cand(2, 1, priority=1)])]), ["SB-1", "SB-2"])

    def test_priority_urgent_first_and_none_last(self):
        pools = [("backlog", [cand(1, 1, priority=0), cand(2, 1, priority=4), cand(3, 1, priority=1)])]
        self.assertEqual(self._order(pools), ["SB-3", "SB-2", "SB-1"])

    def test_critical_path_breaks_a_priority_tie_then_unblocks_then_age(self):
        pools = [("backlog", [
            cand(1, 1, created="2026-01-01T00:00:00Z"),
            cand(2, 1, created="2026-01-02T00:00:00Z", blocks=[("SB-9", "unstarted")]),  # unblocks 1, off-path
            cand(3, 1, created="2026-01-03T00:00:00Z", blocks=[("SB-4", "unstarted")]),
            cand(4, 1, created="2026-01-04T00:00:00Z", blocks=[("SB-5", "unstarted")]),
            cand(5, 1, created="2026-01-05T00:00:00Z"),
        ])]
        p = _plan(100, pools)
        self.assertEqual([c["identifier"] for c in p["candidates"]], ["SB-3", "SB-4", "SB-5", "SB-2", "SB-1"])
        flags = {c["identifier"]: (c["on_critical_path"], c["unblocks"]) for c in p["candidates"]}
        self.assertEqual(flags["SB-3"], (True, 1))
        self.assertEqual(flags["SB-2"], (False, 1))
        self.assertEqual([c["rank"] for c in p["candidates"]], [1, 2, 3, 4, 5])

    # NEGATIVE: a done ticket does not count as something this one unblocks.
    def test_unblocks_ignores_closed_targets(self):
        p = _plan(100, [("backlog", [cand(1, 1, blocks=[("SB-9", "completed"), ("SB-8", "canceled")])])])
        self.assertEqual(p["candidates"][0]["unblocks"], 0)

    def test_a_ticket_in_two_pools_keeps_the_first_source(self):
        p = _plan(100, [("carry_over", [cand(1, 1)]), ("backlog", [cand(1, 1)])])
        self.assertEqual([(c["identifier"], c["source"]) for c in p["candidates"]], [("SB-1", "carry_over")])

    # NEGATIVE: a loop in the blocks graph is a warning, not a hang.
    def test_a_blocks_cycle_warns_and_marks_no_critical_path(self):
        p, warnings = cs.plan(TARGET, 10, [("backlog", [cand(1, 1, blocks=[("SB-2", "unstarted")]),
                                                        cand(2, 1, blocks=[("SB-1", "unstarted")])])])
        self.assertTrue(any("cycle" in w for w in warnings))
        self.assertFalse(any(c["on_critical_path"] for c in p["candidates"]))


class PlanPin(unittest.TestCase):
    """--pin: the user says a ticket must be in the cycle. It moves to the
    front of the ranking; it never lifts capacity."""

    def _pools(self):
        return [("carry_over", [cand(4, 1, priority=1)]),
                ("backlog", [cand(1, 1, priority=1), cand(2, 1, priority=2), cand(3, 1, priority=3)])]

    def test_pinned_rank_first_in_the_order_given_and_keep_their_source(self):
        p = _plan(100, self._pools(), pin=["SB-3", "SB-2"])
        self.assertEqual([(c["identifier"], c["source"], c["pinned"], c["rank"]) for c in p["candidates"]], [
            ("SB-3", "backlog", True, 1), ("SB-2", "backlog", True, 2),
            ("SB-4", "carry_over", False, 3), ("SB-1", "backlog", False, 4)])

    # NEGATIVE: the order of --pin is the order of the ranking, not priority's.
    def test_reversing_the_pins_reverses_their_ranks(self):
        p = _plan(100, self._pools(), pin=["SB-2", "SB-3"])
        self.assertEqual([c["identifier"] for c in p["candidates"]][:2], ["SB-2", "SB-3"])

    def test_without_pins_nothing_is_pinned(self):
        p = _plan(100, self._pools())
        self.assertFalse(any(c["pinned"] for c in p["candidates"]))
        self.assertFalse(p["pinned_exceeds_capacity"])

    # NEGATIVE: a pin never pushes the plan past capacity.
    def test_pinned_tickets_are_still_bound_by_capacity(self):
        pools = [("carry_over", [cand(3, 1)]), ("backlog", [cand(1, 4), cand(2, 4)])]
        p = _plan(5, pools, pin=["SB-1", "SB-2"])
        self.assertEqual((p["fits"], p["does_not_fit"]), (["SB-1", "SB-3"], ["SB-2"]))
        self.assertLessEqual(p["proposed_points"], p["capacity_points"])
        self.assertTrue(p["pinned_exceeds_capacity"])

    def test_pins_within_capacity_are_not_flagged(self):
        p = _plan(8, [("backlog", [cand(1, 4), cand(2, 4)])], pin=["SB-1", "SB-2"])
        self.assertFalse(p["pinned_exceeds_capacity"])
        self.assertEqual(p["fits"], ["SB-1", "SB-2"])
        self.assertFalse(_plan(None, [("backlog", [cand(1, 4)])], pin=["SB-1"])["pinned_exceeds_capacity"])

    # NEGATIVE: pinning an unestimated ticket does not make it fit.
    def test_an_unestimated_pin_needs_an_estimate(self):
        p = _plan(100, [("backlog", [cand(1, None), cand(2, 1)])], pin=["SB-1"])
        self.assertEqual((p["needs_estimate"], p["fits"]), (["SB-1"], ["SB-2"]))
        self.assertTrue(p["candidates"][0]["pinned"])

    # NEGATIVE: pinned and excluded at once is a contradiction, not a guess.
    def test_pin_and_exclude_of_the_same_ticket_raises(self):
        with self.assertRaisesRegex(ValueError, "SB-1"):
            _plan(100, [("backlog", [cand(1, 1)])], pin=["SB-1"], exclude={"SB-1"})


class PlanProjected(unittest.TestCase):
    """Carry-over from a previous cycle that has not closed is a projection:
    ranked as if rolled, but still in that cycle, so no move may be written."""

    POOLS = [("carry_over", [cand(1, 1)]), ("in_target", [cand(2, 1)]), ("backlog", [cand(3, 1)])]

    def test_projected_marks_only_carry_over(self):
        p = _plan(10, self.POOLS, carry_over_projected=True)
        self.assertTrue(p["carry_over_projected"])
        self.assertEqual({c["identifier"]: c["projected"] for c in p["candidates"]},
                         {"SB-1": True, "SB-2": False, "SB-3": False})

    # NEGATIVE: from a closed cycle the carry-over is real, not projected.
    def test_not_projected_by_default(self):
        p = _plan(10, self.POOLS)
        self.assertFalse(p["carry_over_projected"])
        self.assertFalse(any(c["projected"] for c in p["candidates"]))

    def test_projection_does_not_change_ranking_or_fit(self):
        a, b = _plan(2, self.POOLS), _plan(2, self.POOLS, carry_over_projected=True)
        for k in ("fits", "does_not_fit", "proposed_points"):
            self.assertEqual(a[k], b[k])


# ------------------------------------------------------------------- CLI

class ModeArg(unittest.TestCase):
    def test_accepts_the_keywords_and_any_digit_string(self):
        for s in ("current", "next", "plan-target", "7", "007", "0"):
            with self.subTest(s=s):
                self.assertEqual(cs._mode(s), s)

    # NEGATIVE: anything else, including a near-miss, is rejected.
    def test_rejects_anything_else(self):
        for s in ("last", "Next", " 7", "7 ", "plan_target", ""):
            with self.subTest(s=s):
                with self.assertRaises(cs.argparse.ArgumentTypeError):
                    cs._mode(s)


class IdentifierArg(unittest.TestCase):
    def test_normalizes_case_and_surrounding_whitespace(self):
        self.assertEqual(cs._identifier(" sb-12 "), "SB-12")
        self.assertEqual(cs._identifier("Sb-3"), "SB-3")

    # NEGATIVE: not the team's key shape, or another team's.
    def test_rejects_a_non_matching_string(self):
        for s in ("SB12", "SB-", "SB-1a", "XX-12", "12", "SB-1-2"):
            with self.subTest(s=s):
                with self.assertRaises(cs.argparse.ArgumentTypeError):
                    cs._identifier(s)


class AsOfArg(unittest.TestCase):
    def test_a_bare_date_is_local_midnight(self):
        dt = cs._as_of("2026-09-20")
        self.assertEqual((dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second), (2026, 9, 20, 0, 0, 0))
        self.assertIsNotNone(dt.tzinfo)

    def test_a_naive_datetime_is_read_as_local_time(self):
        dt = cs._as_of("2026-09-20T09:30:00")
        self.assertEqual((dt.hour, dt.minute), (9, 30))
        self.assertIsNotNone(dt.tzinfo)

    # NEGATIVE: an aware datetime is kept exactly as given, not converted.
    def test_an_aware_datetime_keeps_its_own_offset(self):
        dt = cs._as_of("2026-09-20T09:00:00-04:00")
        self.assertEqual(dt.utcoffset(), datetime.timedelta(hours=-4))
        self.assertEqual(dt.hour, 9)

    # NEGATIVE: not an ISO date or datetime at all.
    def test_an_unparsable_string_raises(self):
        with self.assertRaises(cs.argparse.ArgumentTypeError):
            cs._as_of("not-a-date")


# --------------------------------------------------------- end to end

class FakeLinear:
    """Answers each query cycle_state sends from fixtures, and records it."""

    def __init__(self, *, cycles, members, uncompleted, gated, backlog, by_number):
        self.cycles, self.members, self.uncompleted = cycles, members, uncompleted
        self.gated, self.backlog, self.by_number = gated, backlog, by_number
        self.calls = []

    def __call__(self, query, variables=None):
        self.calls.append((query, json.dumps(variables, sort_keys=True)))
        if "mutation" in query:
            raise AssertionError("cycle_state sent a mutation")
        if "uncompletedIssuesUponClose" in query:
            return {"cycle": {"uncompletedIssuesUponClose": {"nodes": self.uncompleted.get(variables["id"], [])}}}
        if "cycles(" in query:
            return {"cycles": {"nodes": self.cycles}}
        if "cycle:{id:{eq:$id}}" in query:
            return {"issues": {"nodes": self.members.get(variables["id"], [])}}
        if "labels:{name:{in:$labels}}" in query:
            return {"issues": {"nodes": self.gated}}
        if "cycle:{null:true}" in query:
            return {"issues": {"nodes": self.backlog}}
        if "number:{in:$n}" in query:
            return {"issues": {"nodes": [i for i in self.by_number
                                         if int(i["identifier"].split("-")[1]) in variables["n"]]}}
        raise AssertionError(f"unexpected query: {query}")


TOP_KEYS = ["schema", "generated_at", "team", "mode", "params", "cycle", "summary", "issues", "at_risk",
            "waiting_on_human", "not_ready", "velocity", "plan", "warnings"]
ISSUE_KEYS = ["identifier", "title", "url", "state", "estimate", "priority", "adhoc", "driven", "gate", "project",
              "repo", "created_at", "started_at", "last_activity", "activity_source", "days_since_activity",
              "carry_in", "has_ac", "blocked_by", "blocks"]
CANDIDATE_KEYS = ["identifier", "title", "source", "pinned", "projected", "rank", "estimate", "priority", "repo",
                  "on_critical_path", "unblocks", "has_ac", "fits", "cumulative_points"]
PLAN_KEYS = ["target", "capacity_points", "proposed_points", "candidates", "fits", "does_not_fit", "needs_estimate",
             "carry_over_exceeds_capacity", "pinned_exceeds_capacity", "carry_over_projected"]


# ------------------------------------------------------------- SKILL.md

SKILL_MD_PATH = Path(cs.__file__).resolve().parents[1] / "SKILL.md"
SKILL_MD_TEXT = SKILL_MD_PATH.read_text()


def _section(start: str, end: str) -> str:
    m = re.search(re.escape(start) + r"(.*?)" + re.escape(end), SKILL_MD_TEXT, re.S)
    assert m, f"SKILL.md: {start!r} .. {end!r} not found — did a heading move?"
    return m.group(1)


def _field_column_names(section_text: str) -> list[str]:
    """Backtick-wrapped names in the Field (first) column of every `| ... |`
    row in a markdown table, `.nested` and trailing `[]` stripped, deduped in
    first-seen order. A row may name several fields in one cell, e.g.
    "`fits[]`, `does_not_fit[]`"."""
    names = []
    for line in section_text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cell = line.split("|")[1]
        for tok in re.findall(r"`([^`]+)`", cell):
            name = tok.split(".")[0].rstrip("[]")
            if name and name not in names:
                names.append(name)
    return names


def _braced_fields(row_start: str) -> list[str]:
    """The comma-separated names inside the first `{...}` shape found after
    `row_start` in SKILL.md."""
    m = re.search(re.escape(row_start) + r".*?\{([^}]*)\}", SKILL_MD_TEXT)
    assert m, f"SKILL.md: no {{...}} shape found after {row_start!r}"
    return [s.strip() for s in m.group(1).split(",")]


class SkillMdSchema(unittest.TestCase):
    """SKILL.md is the contract every reader — /cycle review, /cycle plan, the
    standup, the runner feed, the Telegram chat — is told to trust instead of
    re-deriving a number. A field renamed in code without a matching SKILL.md
    edit is a reader quoting a field that no longer exists, silently, in a
    conversation nobody is re-checking against the code."""

    def test_top_level_fields_match_the_emitted_keys(self):
        section = _section("## Schema `po-agent.cycle_state/1`", "### `issues[]`")
        self.assertEqual(_field_column_names(section), TOP_KEYS)

    def test_issue_fields_match_the_emitted_keys(self):
        section = _section("### `issues[]`", "### `velocity`")
        self.assertEqual(_field_column_names(section), ISSUE_KEYS)

    def test_plan_top_level_fields_match_the_emitted_keys(self):
        section = _section("### `plan`", "`source` is one of")
        self.assertEqual(_field_column_names(section), PLAN_KEYS)

    def test_plan_candidate_fields_match_the_emitted_keys(self):
        self.assertEqual(_braced_fields("| `candidates[]` |"), CANDIDATE_KEYS)

    def test_at_risk_not_started_fields_match(self):
        self.assertEqual(_braced_fields("| `at_risk.not_started[]` |"),
                         ["identifier", "title", "estimate", "priority"])

    def test_at_risk_stalled_fields_match(self):
        self.assertEqual(_braced_fields("| `at_risk.stalled[]` |"),
                         ["identifier", "title", "state", "days_since_activity"])

    def test_waiting_on_human_fields_match(self):
        self.assertEqual(_braced_fields("| `waiting_on_human[]` |"),
                         ["identifier", "title", "url", "label", "since", "age_days", "age_source", "in_cycle"])

    def test_velocity_fields_match(self):
        self.assertEqual(_braced_fields("| `velocity` |"),
                         ["cycles[]", "mean_points_done", "adhoc_share_points", "capacity_points", "formula"])


class Build(unittest.TestCase):
    def setUp(self):
        cycles = _cycles()
        c4, c5 = fx.by_number(cycles, 4), fx.by_number(cycles, 5)
        carried = {i["identifier"]: i for i in fx.c3_uncompleted()}
        self.fake = FakeLinear(
            cycles=cycles,
            members={
                "cycle-2-uuid": [],
                "cycle-3-uuid": fx.c3_members(),
                "cycle-4-uuid": [
                    fx.rich(carried["SB-200"], priority=2, updated="2026-08-16T08:00:00.000Z"),
                    fx.rich(carried["SB-300"], priority=3, started="2026-08-10T00:00:00.000Z",
                            updated="2026-08-11T00:00:00.000Z", history=[("2026-08-16T04:00:30.000Z", [])]),
                    fx.rich(fx.issue(501, 2, state="unstarted", labels=("MT",)), priority=1,
                            description="## Acceptance criteria\n- [ ] x", project="Paper AI Proof of Concept"),
                    fx.rich(fx.issue(502, None, state="started", labels=("DOT", "gate:needs-human")),
                            updated="2026-08-16T10:00:00.000Z", blocked_by=[("SB-501", "unstarted")]),
                    fx.rich(fx.issue(503, 3, labels=("DOT",))),
                ],
                "cycle-5-uuid": [fx.rich(fx.issue(600, 2, state="unstarted", labels=("DOT",)), priority=2)],
            },
            uncompleted={"cycle-2-uuid": [], "cycle-3-uuid": fx.c3_uncompleted()},
            gated=[
                fx.rich(fx.issue(502, None, state="started", labels=("DOT", "gate:needs-human")), cycle=c4,
                        updated="2026-08-16T10:00:00.000Z",
                        history=[("2026-08-15T12:00:00.000Z", ["gate:needs-human"])]),
                fx.rich(fx.issue(901, 1, state="unstarted", labels=("gate:awaiting-approval",)),
                        updated="2026-08-12T12:00:00.000Z"),
                fx.rich(fx.issue(964, 1, labels=("gate:awaiting-approval",)), updated="2026-08-12T12:00:00.000Z"),
            ],
            backlog=[fx.rich(fx.issue(700, 3, state="backlog", labels=("DOT",)), priority=1),
                     fx.rich(fx.issue(701, None, state="backlog", labels=("DOT",)), priority=2)],
            by_number=[fx.rich(fx.issue(800, 1, state="backlog", labels=("DOT",)), priority=4)],
        )
        self.c5 = c5
        for mod in (cs, cs.cycles):
            p = mock.patch.object(mod, "gql", side_effect=self.fake)
            p.start()
            self.addCleanup(p.stop)

    def _build(self, mode, **kw):
        doc = cs.build(cs.Fetcher(), mode, now=MID_DAY_AFTER, **kw)
        # Tuples, sets and datetimes would be reshaped or rejected.
        self.assertEqual(json.loads(json.dumps(doc)), doc)
        return doc

    def test_current_document_shape_and_schema(self):
        doc = self._build("current")
        self.assertEqual(list(doc), TOP_KEYS)
        self.assertEqual(doc["schema"], "po-agent.cycle_state/1")
        self.assertEqual((doc["team"], doc["mode"]), ("SB", "current"))
        self.assertEqual(list(doc["cycle"]), ["id", "number", "starts_at", "ends_at", "state", "closed", "days_left",
                                              "days_elapsed", "planning"])
        self.assertEqual(list(doc["issues"][0]), ISSUE_KEYS)
        self.assertEqual(list(doc["velocity"]), ["cycles", "mean_points_done", "adhoc_share_points",
                                                 "capacity_points", "formula"])
        self.assertEqual(list(doc["at_risk"]), ["not_started", "stalled"])
        self.assertIsNone(doc["plan"])
        self.assertEqual(doc["summary"]["schema"], 1)

    def test_current_cycle_contents(self):
        doc = self._build("current")
        self.assertEqual((doc["cycle"]["number"], doc["cycle"]["days_left"], doc["cycle"]["days_elapsed"]), (4, 7, 0))
        rows = {r["identifier"]: r for r in doc["issues"]}
        self.assertEqual([r["identifier"] for r in doc["issues"]], ["SB-200", "SB-300", "SB-501", "SB-502", "SB-503"])
        self.assertTrue(rows["SB-200"]["carry_in"])
        self.assertFalse(rows["SB-501"]["carry_in"])
        self.assertEqual((rows["SB-501"]["repo"], rows["SB-501"]["project"], rows["SB-501"]["has_ac"]),
                         ("MT", "Paper AI Proof of Concept", True))
        self.assertEqual((rows["SB-502"]["gate"], rows["SB-502"]["blocked_by"]), ("gate:needs-human", ["SB-501"]))
        self.assertEqual(rows["SB-300"]["activity_source"], "updatedAt")
        self.assertEqual([s["identifier"] for s in doc["at_risk"]["stalled"]], ["SB-300"])
        self.assertEqual(doc["at_risk"]["not_started"], [])
        self.assertEqual([(w["identifier"], w["in_cycle"], w["age_source"]) for w in doc["waiting_on_human"]],
                         [("SB-901", False, "updatedAt"), ("SB-502", True, "history")])
        self.assertEqual({n["identifier"] for n in doc["not_ready"]}, {"SB-200", "SB-300", "SB-502"})
        self.assertEqual(doc["velocity"]["capacity_points"], 27)
        self.assertEqual(doc["summary"]["carry_in"]["from_cycle"], 3)

    def test_next_fills_the_plan_from_carry_over_target_backlog_and_include(self):
        doc = self._build("next", include=["SB-800", "SB-700", "SB-999"], exclude=["SB-503"])
        p = doc["plan"]
        self.assertEqual(list(p), PLAN_KEYS)
        self.assertEqual(list(p["candidates"][0]), CANDIDATE_KEYS)
        self.assertEqual(doc["cycle"]["number"], 5)
        self.assertEqual(p["target"]["reason"], "next cycle after cycle 4")
        sources = {c["identifier"]: c["source"] for c in p["candidates"]}
        self.assertEqual(sources, {"SB-200": "carry_over", "SB-300": "carry_over", "SB-501": "carry_over",
                                   "SB-502": "carry_over", "SB-600": "in_target", "SB-700": "backlog",
                                   "SB-701": "backlog", "SB-800": "included"})
        self.assertEqual(p["capacity_points"], 27)
        self.assertLessEqual(p["proposed_points"], p["capacity_points"])
        self.assertEqual(sorted(p["needs_estimate"]), ["SB-502", "SB-701"])
        self.assertFalse(p["carry_over_exceeds_capacity"])
        self.assertIn("--include not found in Linear: SB-999", doc["warnings"])

    # Cycle 4 is still running when cycle 5 is planned: its open work is projected.
    def test_carry_over_from_an_unclosed_previous_cycle_is_projected(self):
        p = self._build("next")["plan"]
        self.assertTrue(p["carry_over_projected"])
        flags = {c["identifier"]: (c["source"], c["projected"]) for c in p["candidates"]}
        self.assertEqual(flags["SB-200"], ("carry_over", True))
        self.assertEqual(flags["SB-600"], ("in_target", False))
        self.assertEqual(flags["SB-700"], ("backlog", False))

    # NEGATIVE: cycle 3 has closed, so cycle 4's carry-in is real membership.
    def test_carry_over_from_a_closed_previous_cycle_is_not_projected(self):
        p = self._build("current", want_plan=True)["plan"]
        self.assertFalse(p["carry_over_projected"])
        self.assertEqual({c["identifier"]: c["projected"] for c in p["candidates"] if c["source"] == "carry_over"},
                         {"SB-200": False, "SB-300": False})

    def test_a_done_or_canceled_pin_or_include_is_a_warning_not_a_candidate(self):
        self.fake.by_number += [fx.rich(fx.issue(801, 3, state="completed", labels=("DOT",))),
                                fx.rich(fx.issue(802, 2, state="canceled", labels=("DOT",)))]
        doc = self._build("next", pin=["SB-801"], include=["SB-802", "SB-800"])
        self.assertIn("--pin SB-801 is Done (completed); not a candidate", doc["warnings"])
        self.assertIn("--include SB-802 is Canceled (canceled); not a candidate", doc["warnings"])
        ids = [c["identifier"] for c in doc["plan"]["candidates"]]
        self.assertNotIn("SB-801", ids)
        self.assertNotIn("SB-802", ids)
        self.assertIn("SB-800", ids)
        self.assertFalse(doc["plan"]["pinned_exceeds_capacity"])
        self.assertFalse(any("not found" in w for w in doc["warnings"]))

    def test_plan_flag_on_the_active_cycle_uses_its_carry_in_as_carry_over(self):
        p = self._build("current", want_plan=True)["plan"]
        sources = {c["identifier"]: c["source"] for c in p["candidates"]}
        self.assertEqual((sources["SB-200"], sources["SB-501"]), ("carry_over", "in_target"))
        self.assertNotIn("SB-503", sources)   # done
        self.assertEqual(p["target"]["reason"], "requested with --plan")

    def test_pin_pulls_a_low_priority_ticket_from_outside_the_pools_and_ranks_it_first(self):
        doc = self._build("next", pin=["SB-800", "SB-600"])
        p = doc["plan"]
        self.assertEqual(doc["params"]["pin"], ["SB-800", "SB-600"])
        head = [(c["identifier"], c["source"], c["pinned"]) for c in p["candidates"][:3]]
        self.assertEqual(head[:2], [("SB-800", "included", True), ("SB-600", "in_target", True)])
        self.assertFalse(head[2][2])
        self.assertEqual(p["fits"][:2], ["SB-800", "SB-600"])
        self.assertLessEqual(p["proposed_points"], p["capacity_points"])
        self.assertFalse(p["pinned_exceeds_capacity"])

    def test_a_pin_not_in_linear_is_a_warning(self):
        doc = self._build("next", pin=["SB-998"])
        self.assertIn("--pin not found in Linear: SB-998", doc["warnings"])
        self.assertNotIn("SB-998", [c["identifier"] for c in doc["plan"]["candidates"]])

    # NEGATIVE: --pin and --exclude of one ticket stops before any query.
    def test_pin_and_exclude_conflict_errors_before_any_query(self):
        with self.assertRaisesRegex(ValueError, "--pin and --exclude name the same ticket: SB-600"):
            cs.build(cs.Fetcher(), "next", now=MID_DAY_AFTER, pin=["SB-600"], exclude=["SB-600"])
        with self.assertRaises(SystemExit) as cm:
            cs.main(["--cycle", "next", "--pin", "SB-600", "--exclude", "sb-600"])
        self.assertIn("--pin and --exclude name the same ticket: SB-600", str(cm.exception.code))
        self.assertEqual(self.fake.calls, [])

    def test_query_budget(self):
        for mode, kw in (("current", {}), ("next", {"include": ["SB-800"]}), ("current", {"want_plan": True}),
                         ("next", {"include": ["SB-800"], "pin": ["SB-998", "SB-600", "SB-800"]})):
            with self.subTest(mode=mode, **kw):
                self.fake.calls.clear()
                self._build(mode, **kw)
                self.assertLessEqual(len(self.fake.calls), 12)
                # The per-cycle cache: nothing is asked twice.
                self.assertEqual(len(self.fake.calls), len(set(self.fake.calls)))

    def test_main_prints_one_json_document(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cs.main(["--cycle", "current", "--as-of", "2026-08-16T12:00:00+00:00"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["cycle"]["number"], 4)

    # NEGATIVE: a bad --cycle is a usage error before any query.
    def test_bad_cycle_argument_is_rejected(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            cs.main(["--cycle", "last"])
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(self.fake.calls, [])

    def test_an_unknown_cycle_exits_non_zero_with_the_message(self):
        with self.assertRaises(SystemExit) as cm:
            cs.main(["--cycle", "99", "--as-of", "2026-08-16T12:00:00+00:00"])
        self.assertEqual(cm.exception.code, "cycle_state: cycle 99 not found")


if __name__ == "__main__":
    unittest.main()
