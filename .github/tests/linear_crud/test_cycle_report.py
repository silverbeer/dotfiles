"""cycle-report.py in linear-crud/scripts — the CLI over cycles.py (SB-626).

The one thing this script can break for real is a write: --mark-planned and
--mark-unplanned rewrite a cycle's description in Linear, and without --yes
they must only show the before/after. These tests drive main() with every
fetch and the write stubbed at the names cycle-report bound them to, and with
cycles.gql / linear_api.gql replaced by tripwires, so nothing can reach Linear
even if a stub is missed. The write path itself is exercised by calling mark()
with yes=True against the recording stub; the CLI is never given --yes.

Also covered: flag validation, cycle selection at the 2026-08-16T04:00Z
boundary through --as-of, the query plan per phase, and that --json and the
text report both render every phase.
"""

import contextlib
import io
import json
import sys
import unittest
from unittest import mock

import _cycle_fixtures as fx
from _load import load_module

report = load_module("cycle_report", "linear-crud", "cycle-report.py")
cycles_mod = sys.modules[report.summarize.__module__]
linear_api = sys.modules["linear_api"]

BOUNDARY = "2026-08-16T04:00:00+00:00"
MID_DAY_AFTER = "2026-08-16T12:00:00+00:00"


def _tripwire(*args, **kwargs):
    raise AssertionError(f"a real gql call was attempted during an offline test: {args!r}")


class _Stubbed(unittest.TestCase):
    """Every network-facing name cycle-report uses, replaced for each test."""

    def setUp(self):
        self.cycles = fx.cycles()
        self.members = {
            "cycle-3-uuid": fx.c3_members(),
            "cycle-4-uuid": fx.c3_uncompleted() + [fx.issue(500, 3, labels=("DOT",))],
            "cycle-2-uuid": [],
        }
        self.uncompleted = {"cycle-3-uuid": fx.c3_uncompleted(), "cycle-2-uuid": [], "cycle-4-uuid": []}
        stubs = {
            "fetch_cycles": mock.Mock(side_effect=lambda: self.cycles),
            "fetch_members": mock.Mock(side_effect=lambda cid: self.members[cid]),
            "fetch_uncompleted_upon_close": mock.Mock(side_effect=lambda cid: self.uncompleted[cid]),
            "mark_planning": mock.Mock(side_effect=self._fake_write),
        }
        for name, stub in stubs.items():
            p = mock.patch.object(report, name, stub)
            p.start()
            self.addCleanup(p.stop)
            setattr(self, name, stub)
        for mod in (cycles_mod, linear_api):
            p = mock.patch.object(mod, "gql", side_effect=_tripwire)
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def _fake_write(cycle_id, description):
        return {"success": True, "cycle": {"number": 0, "description": description}}

    def run_main(self, *argv):
        """(exit code, stdout, stderr) of main() with argv."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["cycle-report.py", *argv]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = report.main()
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def assert_no_write(self):
        self.mark_planning.assert_not_called()
        for c in self.cycles:
            self.assertEqual(c["description"], fx.by_number(fx.cycles(), c["number"])["description"])


class MarkDryRun(_Stubbed):
    def test_mark_unplanned_without_yes_writes_nothing(self):
        rc, out, _ = self.run_main("--mark-unplanned", "--note", "firefighting", "--cycle", "3", "--as-of", BOUNDARY)
        self.assertEqual(rc, 0)
        self.assertIn("dry run — nothing written; re-run with --yes to write", out)
        self.assertIn("'Planning: skipped -- firefighting\\nShipped: 11/37 issues", out)
        self.assert_no_write()

    def test_mark_planned_without_yes_writes_nothing(self):
        self.cycles[2]["description"] = None
        rc, out, _ = self.run_main("--mark-planned", "--cycle", "4", "--as-of", BOUNDARY)
        self.assertEqual(rc, 0)
        self.assertIn("before (0/255 chars): None", out)
        self.assertIn("after  (17/255 chars): 'Planning: planned'", out)
        self.assertIn("dry run", out)
        self.mark_planning.assert_not_called()

    def test_default_target_at_the_boundary_is_the_newly_active_cycle(self):
        rc, out, _ = self.run_main("--mark-planned", "--as-of", BOUNDARY)
        self.assertEqual(rc, 0)
        self.assertIn("cycle 4 description", out)
        self.mark_planning.assert_not_called()

    def test_previous_at_the_boundary_targets_the_cycle_that_just_ended(self):
        rc, out, _ = self.run_main("--mark-unplanned", "--previous", "--as-of", BOUNDARY)
        self.assertEqual(rc, 0)
        self.assertIn("cycle 3 description", out)
        self.mark_planning.assert_not_called()

    def test_restamping_with_identical_values_reports_unchanged(self):
        rc, out, _ = self.run_main("--mark-unplanned", "--note", "focus set ad hoc 2026-08-16",
                                   "--cycle", "4", "--as-of", BOUNDARY)
        self.assertEqual(rc, 0)
        self.assertIn("unchanged — nothing to write", out)
        self.mark_planning.assert_not_called()

    # NEGATIVE: an over-long result exits naming the cycle and never writes.
    def test_over_long_description_exits_without_writing(self):
        rc, out, _ = self.run_main("--mark-unplanned", "--note", "n" * 60, "--cycle", "4", "--as-of", BOUNDARY)
        self.assertIsInstance(rc, str)
        self.assertIn("cycle 4:", rc)
        self.assertIn("255", rc)
        self.assertNotIn("dry run", out)
        self.assert_no_write()

    # NEGATIVE: marking never needs members or the carry-out set.
    def test_marking_fetches_only_the_cycle_list(self):
        self.run_main("--mark-planned", "--cycle", "3", "--as-of", BOUNDARY)
        self.fetch_cycles.assert_called_once_with()
        self.fetch_members.assert_not_called()
        self.fetch_uncompleted_upon_close.assert_not_called()


class MarkWrite(_Stubbed):
    """mark() with yes=True, against the recording stub. The CLI is not given --yes."""

    def test_yes_writes_exactly_the_previewed_description(self):
        c3 = fx.by_number(self.cycles, 3)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = report.mark(c3, "skipped", "firefighting", True)
        self.assertEqual(rc, 0)
        expected = "Planning: skipped -- firefighting\n" + fx.C3_DESCRIPTION.split("\n", 1)[1]
        self.mark_planning.assert_called_once_with("cycle-3-uuid", expected)
        self.assertIn("written: cycle 0 description is now", out.getvalue())

    # NEGATIVE: an unsuccessful cycleUpdate is an exit, not a silent success.
    def test_an_unsuccessful_update_exits(self):
        self.mark_planning.side_effect = None
        self.mark_planning.return_value = {"success": False}
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm:
            report.mark(fx.by_number(self.cycles, 3), "skipped", None, True)
        self.assertIn("cycleUpdate did not succeed for cycle 3", str(cm.exception.code))


class FlagValidation(_Stubbed):
    # NEGATIVE: --yes / --note without a mark flag is a usage error before any fetch.
    def test_yes_without_a_mark_flag_is_rejected(self):
        for argv in (["--yes"], ["--note", "x"]):
            with self.subTest(argv=argv):
                rc, _, err = self.run_main(*argv)
                self.assertEqual(rc, 2)
                self.assertIn("--note and --yes only apply with", err)
        self.fetch_cycles.assert_not_called()
        self.mark_planning.assert_not_called()

    # NEGATIVE: --json and a mark flag are mutually exclusive.
    def test_json_and_mark_are_mutually_exclusive(self):
        rc, _, err = self.run_main("--json", "--mark-planned")
        self.assertEqual(rc, 2)
        self.assertIn("not allowed with", err)
        self.fetch_cycles.assert_not_called()

    # NEGATIVE: an unparseable --as-of is a usage error.
    def test_bad_as_of_is_rejected(self):
        rc, _, err = self.run_main("--as-of", "last tuesday")
        self.assertEqual(rc, 2)
        self.assertIn("not an ISO date or datetime", err)

    def test_unknown_cycle_exits_with_the_lookup_message(self):
        rc, _, _ = self.run_main("--cycle", "99", "--as-of", BOUNDARY)
        self.assertEqual(rc, "cycle 99 not found")


class ReportQueries(_Stubbed):
    def test_previous_json_at_the_boundary_is_cycle_3_from_history(self):
        rc, out, _ = self.run_main("--previous", "--json", "--as-of", BOUNDARY)
        self.assertEqual(rc, 0)
        s = json.loads(out)
        self.assertEqual(s["cycle"]["number"], 3)
        self.assertEqual(s["totals"]["source"], "history")
        self.assertEqual(s["carry_out"]["points"], 81)
        self.assertEqual(s["carry_out"]["points_current_estimates"], 71)
        # An ended cycle needs its own carry-out set and no predecessor.
        self.fetch_uncompleted_upon_close.assert_called_once_with("cycle-3-uuid")
        self.mark_planning.assert_not_called()

    def test_active_json_at_the_boundary_is_cycle_4_with_carry_in_from_3(self):
        rc, out, _ = self.run_main("--json", "--as-of", BOUNDARY)
        self.assertEqual(rc, 0)
        s = json.loads(out)
        self.assertEqual(s["cycle"]["number"], 4)
        self.assertEqual(s["carry_in"]["from_cycle"], 3)
        self.assertEqual(s["carry_in"]["issues"], 26)
        # An active cycle needs its predecessor's set, not its own.
        self.fetch_uncompleted_upon_close.assert_called_once_with("cycle-3-uuid")
        self.fetch_members.assert_called_once_with("cycle-4-uuid")

    # One second before the boundary the clock still selects cycle 3, but Linear
    # has since closed it: its close data is shown, not today's membership as
    # "active" (~100%), and no carry-in is read.
    def test_one_second_before_the_boundary_selects_cycle_3_and_reads_close_data(self):
        rc, out, err = self.run_main("--json", "--as-of", "2026-08-16T03:59:59+00:00")
        self.assertEqual(rc, 0)
        s = json.loads(out)
        self.assertEqual(s["cycle"]["number"], 3)
        self.assertEqual((s["cycle"]["state"], s["cycle"]["closed"]), ("ended", True))
        self.assertEqual(s["totals"]["source"], "history")
        self.assertIsNone(s["carry_in"])
        self.fetch_uncompleted_upon_close.assert_called_once_with("cycle-3-uuid")
        self.assertIn("warn: cycle 3 is closed in Linear; showing close data, --as-of affects selection only", err)

    def test_as_of_mid_cycle_on_a_closed_cycle_warns_on_stderr_and_keeps_json_clean(self):
        rc, out, err = self.run_main("--json", "--cycle", "3", "--as-of", "2026-08-12")
        self.assertEqual(rc, 0)
        s = json.loads(out)
        self.assertEqual(s["totals"]["issues"], {"done": 11, "scope": 37})
        self.assertIn("closed in Linear", err)

    # NEGATIVE: the warning is about --as-of; a plain run on a closed cycle is quiet.
    def test_no_as_of_warning_without_as_of(self):
        rc, _, err = self.run_main("--json", "--cycle", "3")
        self.assertEqual(rc, 0)
        self.assertNotIn("closed in Linear", err)

    # NEGATIVE: an --as-of at or after endsAt is legitimately after the cycle
    # ended. Cycle 3 closed at 04:00:28Z; the boundary instant 04:00:00Z is
    # before that, and the close job's lag must not trigger the warning.
    def test_as_of_at_or_after_the_end_of_a_closed_cycle_does_not_warn(self):
        for as_of in (BOUNDARY, "2026-08-16T04:00:10+00:00", MID_DAY_AFTER):
            with self.subTest(as_of=as_of):
                rc, out, err = self.run_main("--previous", "--json", "--as-of", as_of)
                self.assertEqual(rc, 0)
                self.assertEqual(json.loads(out)["cycle"]["number"], 3)
                self.assertNotIn("closed in Linear", err)

    # Ended by the clock, not yet closed by Linear: its own uncompleted set is
    # still [] and must not be fetched; the closed predecessor's is, for carry-in.
    def test_ended_but_not_closed_fetches_only_the_predecessors_set(self):
        rc, out, err = self.run_main("--json", "--cycle", "4", "--as-of", "2026-08-24T00:00:00+00:00")
        self.assertEqual(rc, 0)
        s = json.loads(out)
        self.assertEqual((s["cycle"]["state"], s["cycle"]["closed"]), ("ended", False))
        self.assertEqual(s["totals"]["source"], "membership")
        self.assertIsNone(s["carry_out"])
        self.assertEqual(s["carry_in"]["from_cycle"], 3)
        self.fetch_uncompleted_upon_close.assert_called_once_with("cycle-3-uuid")
        self.assertNotIn("closed in Linear", err)

    def test_future_cycle_fetches_no_uncompleted_set(self):
        rc, out, _ = self.run_main("--json", "--cycle", "4", "--as-of", "2026-08-10T00:00:00+00:00")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["cycle"]["state"], "future")
        self.fetch_uncompleted_upon_close.assert_not_called()


class TextReport(_Stubbed):
    def test_ended_cycle_report_says_totals_are_from_history(self):
        rc, out, _ = self.run_main("--cycle", "3", "--as-of", MID_DAY_AFTER)
        self.assertEqual(rc, 0)
        self.assertIn("Cycle 3", out)
        self.assertIn("(ended)", out)
        self.assertIn("totals from Linear history at close", out)
        self.assertIn("carry-out: 26 issues / 81 pts unfinished at close", out)
        self.assertIn("71 pts today vs 81 at close", out)

    def test_active_cycle_report_shows_carry_in_and_the_skipped_stamp(self):
        rc, out, _ = self.run_main("--as-of", MID_DAY_AFTER)
        self.assertEqual(rc, 0)
        self.assertIn("cycle 4 was not planned: focus set ad hoc 2026-08-16", out)
        self.assertIn("carry-in from cycle 3: 26 issues / 71 pts", out)
        self.assertIn("totals from current membership", out)

    def test_ended_but_not_closed_report_says_totals_are_membership(self):
        rc, out, _ = self.run_main("--cycle", "4", "--as-of", "2026-08-24T00:00:00+00:00")
        self.assertEqual(rc, 0)
        self.assertIn("(ended)", out)
        self.assertIn("ended but not yet closed by Linear — totals are current membership", out)
        self.assertNotIn("totals from Linear history", out)
        self.assertNotIn("carry-out:", out)

    def test_future_unstamped_cycle_report_renders_and_warns(self):
        self.cycles[2]["description"] = None
        rc, out, _ = self.run_main("--cycle", "4", "--as-of", "2026-08-10T00:00:00+00:00")
        self.assertEqual(rc, 0)
        self.assertIn("starts in", out)
        self.assertIn("cycle 4 has no planning stamp", out)


if __name__ == "__main__":
    unittest.main()
