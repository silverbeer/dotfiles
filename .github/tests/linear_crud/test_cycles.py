"""cycles.py in linear-crud/scripts — the arithmetic behind cycle-report (SB-626).

Two Linear behaviours make this worth pinning. At close Linear rolls every
unfinished issue into the next cycle, so an ended cycle's current membership
reads as ~100% done and drifts afterwards; the true numbers are the history
arrays and uncompletedIssuesUponClose. And endsAt is exclusive: at
2026-08-16T04:00Z cycle 3 is over and cycle 4 is active, never both — the
report that motivated SB-626 read cycle 3 at exactly that instant.

Everything here is offline: gql is replaced by a tripwire for the whole
module, and the only write (mark_planning) is exercised against a recording
stub. Fixture shapes are copied from live Linear; see _cycle_fixtures.py.
"""

import datetime
import json
import unittest
from unittest import mock

import _cycle_fixtures as fx
from _load import load_module

cycles = load_module("cycles", "linear-crud", "cycles.py")

BOUNDARY = fx.at("2026-08-16T04:00:00Z")          # cycle 3 end == cycle 4 start
ONE_SECOND_BEFORE = fx.at("2026-08-16T03:59:59Z")
MID_DAY_AFTER = fx.at("2026-08-16T12:00:00Z")


def _no_network(*args, **kwargs):
    raise AssertionError(f"cycles.gql was called during an offline test: {args!r}")


def setUpModule():
    patcher = mock.patch.object(cycles, "gql", side_effect=_no_network)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


# ---------------------------------------------------------------- time

class ParseTs(unittest.TestCase):
    def test_linear_z_timestamp_is_aware_utc(self):
        ts = cycles.parse_ts("2026-08-16T04:00:00.000Z")
        self.assertIsNotNone(ts.tzinfo)
        self.assertEqual(ts, datetime.datetime(2026, 8, 16, 4, 0, tzinfo=datetime.timezone.utc))


class CyclePhase(unittest.TestCase):
    def setUp(self):
        cs = fx.cycles()
        self.c3, self.c4 = fx.by_number(cs, 3), fx.by_number(cs, 4)

    # The bash harness breaks this comparison (`<` -> `<=`) and asserts THIS
    # test is named in the failure.
    def test_end_is_exclusive_at_the_boundary_instant(self):
        self.assertEqual(cycles.cycle_phase(self.c3, BOUNDARY), "ended")
        self.assertEqual(cycles.cycle_phase(self.c4, BOUNDARY), "active")

    def test_start_is_inclusive_at_the_boundary_instant(self):
        self.assertEqual(cycles.cycle_phase(self.c4, BOUNDARY), "active")

    # NEGATIVE: one second earlier, cycle 3 is still running and 4 has not begun.
    def test_one_second_before_the_boundary_old_cycle_is_still_active(self):
        self.assertEqual(cycles.cycle_phase(self.c3, ONE_SECOND_BEFORE), "active")
        self.assertEqual(cycles.cycle_phase(self.c4, ONE_SECOND_BEFORE), "future")

    def test_mid_day_after_the_boundary_matches_the_boundary(self):
        self.assertEqual(cycles.cycle_phase(self.c3, MID_DAY_AFTER), "ended")
        self.assertEqual(cycles.cycle_phase(self.c4, MID_DAY_AFTER), "active")

    def test_exactly_one_cycle_is_active_around_the_boundary(self):
        for now in (ONE_SECOND_BEFORE, BOUNDARY, MID_DAY_AFTER):
            with self.subTest(now=now.isoformat()):
                active = [c["number"] for c in fx.cycles() if cycles.cycle_phase(c, now) == "active"]
                self.assertEqual(len(active), 1, active)

    def test_the_boundary_in_a_local_timezone_is_the_same_instant(self):
        edt = datetime.timezone(datetime.timedelta(hours=-4))
        local_midnight = datetime.datetime(2026, 8, 16, 0, 0, tzinfo=edt)
        self.assertEqual(cycles.cycle_phase(self.c3, local_midnight), "ended")
        self.assertEqual(cycles.cycle_phase(self.c4, local_midnight), "active")


# ------------------------------------------------------------ selection

class SelectCycle(unittest.TestCase):
    def setUp(self):
        self.cs = fx.cycles()

    def _pick(self, now, **kw):
        return cycles.select_cycle(self.cs, now=now, **kw)["number"]

    def test_at_the_boundary_previous_is_the_cycle_that_just_ended(self):
        self.assertEqual(self._pick(BOUNDARY, previous=True), 3)

    def test_at_the_boundary_active_is_the_cycle_that_just_started(self):
        self.assertEqual(self._pick(BOUNDARY), 4)

    # NEGATIVE: one second earlier nothing has rolled over yet.
    def test_one_second_before_the_boundary_previous_is_two_and_active_is_three(self):
        self.assertEqual(self._pick(ONE_SECOND_BEFORE, previous=True), 2)
        self.assertEqual(self._pick(ONE_SECOND_BEFORE), 3)

    def test_mid_day_after_the_boundary_matches_the_boundary(self):
        self.assertEqual(self._pick(MID_DAY_AFTER, previous=True), 3)
        self.assertEqual(self._pick(MID_DAY_AFTER), 4)

    def test_order_of_the_input_list_does_not_matter(self):
        self.cs.reverse()
        self.assertEqual(self._pick(BOUNDARY, previous=True), 3)
        self.assertEqual(self._pick(BOUNDARY), 4)

    def test_by_number(self):
        self.assertEqual(self._pick(BOUNDARY, number=2), 2)

    def test_number_wins_over_previous(self):
        self.assertEqual(self._pick(BOUNDARY, number=4, previous=True), 4)

    # NEGATIVE: an unknown number is an error, not the nearest cycle.
    def test_unknown_number_raises_lookup_error(self):
        with self.assertRaisesRegex(LookupError, "cycle 99 not found"):
            cycles.select_cycle(self.cs, number=99, now=BOUNDARY)

    # NEGATIVE: before any cycle has ended there is no "previous".
    def test_previous_with_no_ended_cycle_raises_lookup_error(self):
        with self.assertRaisesRegex(LookupError, "no ended cycle"):
            cycles.select_cycle(self.cs, previous=True, now=fx.at("2026-08-09T03:59:59Z"))

    def test_previous_on_the_first_boundary_is_the_first_cycle(self):
        self.assertEqual(self._pick(fx.at("2026-08-09T04:00:00Z"), previous=True), 2)

    # NEGATIVE: no cycles at all.
    def test_no_cycles_raises_lookup_error(self):
        with self.assertRaisesRegex(LookupError, "no cycles found"):
            cycles.select_cycle([], now=BOUNDARY)
        with self.assertRaisesRegex(LookupError, "no ended cycle"):
            cycles.select_cycle([], previous=True, now=BOUNDARY)

    def test_with_no_active_cycle_falls_back_to_the_latest_ending(self):
        # After the schedule runs out: documented fallback, not an error.
        self.assertEqual(self._pick(fx.at("2026-09-01T00:00:00Z")), 4)


class PreviousCycle(unittest.TestCase):
    def setUp(self):
        self.cs = fx.cycles()

    def test_previous_of_four_is_three(self):
        self.assertEqual(cycles.previous_cycle(self.cs, fx.by_number(self.cs, 4))["number"], 3)

    # NEGATIVE: the first cycle has no predecessor.
    def test_first_cycle_has_none(self):
        self.assertIsNone(cycles.previous_cycle(self.cs, fx.by_number(self.cs, 2)))

    def test_a_gap_in_the_schedule_returns_the_nearest_earlier(self):
        gapped = [c for c in self.cs if c["number"] != 3]
        self.assertEqual(cycles.previous_cycle(gapped, fx.by_number(gapped, 4))["number"], 2)


class DaysUntil(unittest.TestCase):
    def test_counts_calendar_days_in_nows_timezone(self):
        self.assertEqual(cycles.days_until("2026-08-23T04:00:00.000Z", MID_DAY_AFTER), 7)

    def test_a_timezone_west_of_utc_can_see_an_earlier_calendar_day(self):
        edt = datetime.timezone(datetime.timedelta(hours=-4))
        # 03:00Z on the 23rd is 23:00 on the 22nd in EDT.
        now = datetime.datetime(2026, 8, 16, 12, 0, tzinfo=edt)
        self.assertEqual(cycles.days_until("2026-08-23T03:00:00.000Z", now), 6)


# ------------------------------------------------------------ the stamp

class ParseStamp(unittest.TestCase):
    def test_cycle_3_reads_as_planned_without_a_note(self):
        s = cycles.parse_stamp(fx.C3_DESCRIPTION)
        self.assertEqual(s["planning"], "planned")
        self.assertIsNone(s["planning_note"])
        self.assertEqual(
            list(s["fields"]), ["Planning", "Shipped", "Carry-out", "Adhoc share of completed", "Note"]
        )
        self.assertEqual(s["fields"]["Carry-out"], "26 issues / 81 pts (19 planned, 7 adhoc) -> cycle 4")

    def test_cycle_4_reads_as_skipped_with_its_note(self):
        s = cycles.parse_stamp(fx.C4_DESCRIPTION)
        self.assertEqual(s["planning"], "skipped")
        self.assertEqual(s["planning_note"], "focus set ad hoc 2026-08-16")
        self.assertEqual(s["fields"]["Intent"], "~32 of 81 pts; rest parked, not committed.")
        # A value containing further colons is kept whole.
        self.assertEqual(s["fields"]["Focus"], "agentic delivery (507,506,437,624,625) + MTA match weekend (593-597)")

    def test_none_and_empty_are_unstamped(self):
        for d in (None, ""):
            with self.subTest(description=d):
                self.assertEqual(cycles.parse_stamp(d), {"planning": None, "planning_note": None, "fields": {}})

    # NEGATIVE: prose and unrelated keys are not a planning stamp.
    def test_unknown_lines_are_ignored_and_not_a_stamp(self):
        s = cycles.parse_stamp("just some prose\nOwner: tom\n: no key here")
        self.assertIsNone(s["planning"])
        self.assertIsNone(s["planning_note"])
        self.assertEqual(s["fields"], {"Owner": "tom"})

    # NEGATIVE: a Planning value that is not planned/skipped is not guessed at.
    def test_unrecognised_planning_values_read_as_unstamped(self):
        for raw in ("Planning: maybe", "Planning: planned-ish", "Planning:", "Planning: skipped - one dash"):
            with self.subTest(raw=raw):
                s = cycles.parse_stamp(raw)
                self.assertIsNone(s["planning"])
                self.assertIsNone(s["planning_note"])

    def test_key_and_status_are_case_insensitive(self):
        self.assertEqual(cycles.parse_stamp("planning: PLANNED")["planning"], "planned")

    def test_first_planning_line_wins(self):
        self.assertEqual(cycles.parse_stamp("Planning: skipped\nPlanning: planned")["planning"], "skipped")

    def test_empty_note_after_the_separator_is_none(self):
        s = cycles.parse_stamp("Planning: skipped --   ")
        self.assertEqual(s["planning"], "skipped")
        self.assertIsNone(s["planning_note"])


class SetPlanning(unittest.TestCase):
    def test_replaces_the_stamp_in_cycle_3_and_keeps_the_other_four_lines_in_order(self):
        out = cycles.set_planning(fx.C3_DESCRIPTION, "skipped", "focus set ad hoc")
        lines = out.split("\n")
        self.assertEqual(lines[0], "Planning: skipped -- focus set ad hoc")
        self.assertEqual(lines[1:], fx.C3_DESCRIPTION.split("\n")[1:])
        self.assertEqual(len(lines), 5)

    def test_replaces_a_stamp_that_is_not_the_first_line_in_place(self):
        out = cycles.set_planning("Shipped: 1/2\nplanning: bogus\nNote: x", "planned")
        self.assertEqual(out, "Shipped: 1/2\nPlanning: planned\nNote: x")

    def test_prepends_when_there_is_no_stamp(self):
        self.assertEqual(cycles.set_planning("Note: x\nOwner: tom", "planned"),
                         "Planning: planned\nNote: x\nOwner: tom")

    def test_none_description_becomes_just_the_stamp(self):
        self.assertEqual(cycles.set_planning(None, "skipped", "x"), "Planning: skipped -- x")

    def test_empty_description_becomes_just_the_stamp(self):
        self.assertEqual(cycles.set_planning("", "planned"), "Planning: planned")

    def test_restamping_cycle_4_with_its_own_values_is_a_no_op(self):
        self.assertEqual(cycles.set_planning(fx.C4_DESCRIPTION, "skipped", "focus set ad hoc 2026-08-16"),
                         fx.C4_DESCRIPTION)

    def test_output_parses_back_to_what_was_set(self):
        s = cycles.parse_stamp(cycles.set_planning(fx.C3_DESCRIPTION, "skipped", "  firefighting  "))
        self.assertEqual((s["planning"], s["planning_note"]), ("skipped", "firefighting"))

    # NEGATIVE: a blank note must not leave a dangling " -- ".
    def test_whitespace_only_note_adds_no_separator(self):
        self.assertEqual(cycles.set_planning(None, "planned", "   "), "Planning: planned")

    def test_exactly_255_chars_is_accepted(self):
        desc = "Planning: planned\n" + "x" * (255 - len("Planning: planned\n"))
        self.assertEqual(len(cycles.set_planning(desc, "planned")), 255)

    # NEGATIVE: one char over the limit raises; it is never truncated.
    def test_256_chars_raises_rather_than_truncating(self):
        desc = "Planning: planned\n" + "x" * (256 - len("Planning: planned\n"))
        with self.assertRaisesRegex(ValueError, "256 chars.*255"):
            cycles.set_planning(desc, "planned")

    # NEGATIVE: cycle 4 is 241 chars; a note that pushes it to 256 must raise.
    def test_a_long_note_on_cycle_4_raises(self):
        self.assertEqual(len(fx.C4_DESCRIPTION), 241)
        fits = "n" * 41   # stamp line 21 + 41 = 62 chars, total 255
        self.assertEqual(len(cycles.set_planning(fx.C4_DESCRIPTION, "skipped", fits)), 255)
        with self.assertRaises(ValueError):
            cycles.set_planning(fx.C4_DESCRIPTION, "skipped", fits + "n")

    # NEGATIVE: a multi-line note would forge extra `Key: value` lines.
    def test_a_note_with_a_newline_raises(self):
        for note in ("two\nlines", "carriage\rreturn", "Planning: planned\n"):
            with self.subTest(note=note), self.assertRaisesRegex(ValueError, "single line"):
                cycles.set_planning(fx.C3_DESCRIPTION, "skipped", note)

    # KNOWN BUG (SB-626 review), NEGATIVE: the single-line guard only rejects
    # \n and \r, but parse_stamp splits with str.splitlines(), which also breaks
    # on \v \f \x1c-\x1e \x85    . A note carrying one of those
    # forges extra `Key: value` fields on read-back. expectedFailure keeps the
    # suite green while the source is unfixed; once cycles.py rejects them this
    # becomes an "unexpected success", which fails the run — remove the
    # decorator then.
    # NEGATIVE: splitlines() breaks on more than \n and \r; any of them would let a
    # note forge a second stamp line.
    def test_a_note_with_any_splitlines_separator_raises(self):
        for sep in ("\v", "\f", "\x1c", "\x85", " ", " "):
            with self.subTest(sep=repr(sep)), self.assertRaisesRegex(ValueError, "single line"):
                cycles.set_planning(None, "skipped", f"ok{sep}Shipped: forged")

    # NEGATIVE: only the two statuses, spelled exactly.
    def test_a_trailing_newline_is_kept(self):
        self.assertEqual(cycles.set_planning("Planning: planned\nNote: x\n", "skipped"),
                         "Planning: skipped\nNote: x\n")

    def test_crlf_line_endings_are_kept_on_every_line(self):
        self.assertEqual(cycles.set_planning("Note: x\r\nPlanning: planned\r\nOwner: tom\r\n", "skipped", "y"),
                         "Note: x\r\nPlanning: skipped -- y\r\nOwner: tom\r\n")

    def test_a_prepended_stamp_uses_the_descriptions_own_line_ending(self):
        self.assertEqual(cycles.set_planning("Note: x\r\nOwner: tom", "planned"),
                         "Planning: planned\r\nNote: x\r\nOwner: tom")

    # NEGATIVE: re-stamping the same value must read as unchanged, not as a
    # rewrite that silently drops the trailing newline.
    def test_restamping_the_same_value_with_a_trailing_newline_is_a_no_op(self):
        for desc in ("Planning: planned\n", "Planning: planned\r\n", "Shipped: 1/2\nPlanning: planned\n\n"):
            with self.subTest(desc=desc):
                self.assertEqual(cycles.set_planning(desc, "planned"), desc)

    def test_only_planned_or_skipped_are_accepted(self):
        for status in ("unplanned", "Planned", "", "done"):
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "planned, skipped"):
                cycles.set_planning(fx.C3_DESCRIPTION, status)


class MarkPlanning(unittest.TestCase):
    def test_sends_the_cycle_id_and_description_and_returns_the_payload(self):
        payload = {"success": True, "cycle": {"number": 4, "description": "Planning: planned"}}
        with mock.patch.object(cycles, "gql", return_value={"cycleUpdate": payload}) as gql:
            self.assertEqual(cycles.mark_planning("cycle-4-uuid", "Planning: planned"), payload)
        gql.assert_called_once()
        self.assertEqual(gql.call_args.args[1], {"id": "cycle-4-uuid", "d": "Planning: planned"})
        self.assertIn("cycleUpdate", gql.call_args.args[0])

    # NEGATIVE: an over-long description is refused before any call is made.
    def test_over_255_chars_raises_without_calling_linear(self):
        with mock.patch.object(cycles, "gql") as gql:
            with self.assertRaises(ValueError):
                cycles.mark_planning("cycle-4-uuid", "x" * 256)
        gql.assert_not_called()


class FetchMembers(unittest.TestCase):
    """extra_fields (SB-1087): po-agent's cycle_state.py is the only caller
    that passes it, splicing fields onto Q_MEMBERS's nodes selection. A bad
    splice point would silently drop the `labels` field summarize() needs, or
    double it, or leak into the shared module-level query string."""

    def test_no_extra_fields_sends_the_base_query_unchanged(self):
        with mock.patch.object(cycles, "gql", return_value={"issues": {"nodes": []}}) as gql:
            cycles.fetch_members("cycle-4-uuid")
        self.assertEqual(gql.call_args.args[0], cycles.Q_MEMBERS)
        self.assertEqual(gql.call_args.args[1], {"id": "cycle-4-uuid"})

    def test_extra_fields_are_spliced_in_once_after_labels(self):
        with mock.patch.object(cycles, "gql", return_value={"issues": {"nodes": []}}) as gql:
            cycles.fetch_members("cycle-4-uuid", "url priority updatedAt")
        query = gql.call_args.args[0]
        self.assertEqual(query.count("labels{nodes{name}}"), 1)
        self.assertIn("labels{nodes{name}} url priority updatedAt }", query)
        self.assertNotEqual(query, cycles.Q_MEMBERS)

    # NEGATIVE: splicing must not mutate the shared query constant other
    # cycles reuse — a str.replace on the module global would leak across calls.
    def test_extra_fields_do_not_mutate_the_module_level_query_constant(self):
        with mock.patch.object(cycles, "gql", return_value={"issues": {"nodes": []}}):
            cycles.fetch_members("cycle-4-uuid", "url")
        self.assertNotIn("url", cycles.Q_MEMBERS)
        with mock.patch.object(cycles, "gql", return_value={"issues": {"nodes": []}}) as gql:
            cycles.fetch_members("cycle-4-uuid")
        self.assertEqual(gql.call_args.args[0], cycles.Q_MEMBERS)

    def test_result_nodes_pass_through_unchanged(self):
        nodes = [fx.issue(1, 3)]
        with mock.patch.object(cycles, "gql", return_value={"issues": {"nodes": nodes}}):
            self.assertEqual(cycles.fetch_members("cycle-4-uuid", "url"), nodes)


# ------------------------------------------------------------ summarize

class SummarizeClosedCycle(unittest.TestCase):
    """Cycle 3 read after close: members have drifted, history has not."""

    def setUp(self):
        self.c3 = fx.by_number(fx.cycles(), 3)
        self.s = cycles.summarize(self.c3, fx.c3_members(), fx.c3_uncompleted(), now=MID_DAY_AFTER)

    def test_state_is_ended_with_no_days_left(self):
        self.assertEqual(self.s["cycle"]["state"], "ended")
        self.assertTrue(self.s["cycle"]["closed"])
        self.assertIsNone(self.s["cycle"]["days_left"])
        self.assertEqual(self.s["schema"], 1)

    def test_totals_come_from_the_last_history_sample(self):
        self.assertEqual(self.s["totals"], {
            "source": "history",
            "issues": {"done": 11, "scope": 37},
            "points": {"done": 48, "scope": 129},
        })

    # NEGATIVE: the drifted membership (10 done / 46 pts) must not leak into totals.
    def test_totals_do_not_follow_drifted_membership(self):
        members_done = [i for i in fx.c3_members() if i["state"]["type"] == "completed"]
        self.assertEqual((len(members_done), sum(i["estimate"] for i in members_done)), (10, 46))
        self.assertNotEqual(self.s["totals"]["issues"]["done"], 10)
        self.assertNotEqual(self.s["totals"]["points"]["done"], 46)

    def test_carry_out_points_are_the_history_shortfall(self):
        co = self.s["carry_out"]
        self.assertEqual(co["issues"], 26)
        self.assertEqual(co["points"], 129 - 48)
        self.assertEqual(co["points_current_estimates"], 71)

    def test_carry_out_splits_planned_and_adhoc_by_current_labels(self):
        co = self.s["carry_out"]
        self.assertEqual(co["planned"], {"issues": 19, "points": 52})
        self.assertEqual(co["adhoc"], {"issues": 7, "points": 19})
        self.assertEqual(co["identifiers"], [i["identifier"] for i in fx.c3_uncompleted()])

    def test_carry_in_is_none_for_an_ended_cycle(self):
        self.assertIsNone(self.s["carry_in"])
        # Even when handed a predecessor's set: carry-in is an active-cycle view.
        s = cycles.summarize(self.c3, fx.c3_members(), fx.c3_uncompleted(), fx.c3_uncompleted(), now=MID_DAY_AFTER)
        self.assertIsNone(s["carry_in"])

    def test_split_is_completed_members_plus_carry_out(self):
        self.assertEqual(self.s["split"]["source"], "completed membership + carry-out, current labels")
        self.assertEqual(self.s["split"]["planned"], {
            "issues": {"done": 6, "scope": 25}, "points": {"done": 32, "scope": 84},
        })
        self.assertEqual(self.s["split"]["adhoc"], {
            "issues": {"done": 4, "scope": 11}, "points": {"done": 14, "scope": 33},
        })

    # NEGATIVE: a canceled member is neither done nor carried.
    def test_canceled_member_is_not_in_the_population(self):
        scope = self.s["split"]["planned"]["issues"]["scope"] + self.s["split"]["adhoc"]["issues"]["scope"]
        self.assertEqual(scope, 10 + 26)

    def test_adhoc_share_is_over_the_rebuilt_population(self):
        self.assertEqual(self.s["adhoc_share"], {"issues": round(100 * 11 / 36), "points": round(100 * 33 / 117)})

    def test_planning_stamp_is_read_from_the_description(self):
        self.assertEqual(self.s["planning"], {"status": "planned", "note": None, "stamped": True})

    def test_delivered_by_counts_completed_work_only_in_autonomy_order(self):
        self.assertEqual(list(self.s["delivered_by"]), ["human", "agent-supervised", "agent-auto", "unlabelled"])
        self.assertEqual(self.s["delivered_by"]["human"], {"issues": 5, "points": 25, "pct": round(2500 / 46)})
        self.assertEqual(self.s["delivered_by"]["agent-auto"], {"issues": 3, "points": 13, "pct": round(1300 / 46)})
        # NEGATIVE: the 26 unfinished, unlabelled carried issues are not "delivered".
        self.assertEqual(self.s["delivered_by"]["unlabelled"]["issues"], 1)

    def test_created_mid_cycle_counts_only_issues_created_inside_the_window(self):
        self.assertEqual(self.s["created_mid_cycle"], 1)

    def test_issue_both_completed_in_members_and_carried_counts_as_carried(self):
        members = fx.c3_members() + [fx.issue(200, 5, labels=("DOT",))]   # SB-200 is also in the carried set
        s = cycles.summarize(self.c3, members, fx.c3_uncompleted(), now=MID_DAY_AFTER)
        self.assertEqual(s["split"]["planned"]["issues"], {"done": 6, "scope": 25})

    def test_without_the_uncompleted_set_totals_still_come_from_history(self):
        s = cycles.summarize(self.c3, fx.c3_members(), None, now=MID_DAY_AFTER)
        self.assertEqual(s["totals"]["source"], "history")
        self.assertIsNone(s["carry_out"])
        self.assertEqual(s["split"]["source"], "membership, current labels")

class SummarizeClosedCycleReadAsOfEarlier(unittest.TestCase):
    """--as-of before a cycle's close cannot rebuild the membership it had then:
    after close it is only the completed work, so reading it as "active" would
    report ~100%. A closed cycle always reads as ended, with close data."""

    def setUp(self):
        cs = fx.cycles()
        self.c2, self.c3 = fx.by_number(cs, 2), fx.by_number(cs, 3)

    def test_mid_cycle_as_of_still_reads_close_data(self):
        for now in (fx.at("2026-08-12T12:00:00Z"), ONE_SECOND_BEFORE):
            with self.subTest(now=now.isoformat()):
                s = cycles.summarize(self.c3, fx.c3_members(), fx.c3_uncompleted(), fx.c3_uncompleted(),
                                     prev_cycle=self.c2, now=now)
                self.assertEqual((s["cycle"]["state"], s["cycle"]["closed"]), ("ended", True))
                self.assertIsNone(s["cycle"]["days_left"])
                self.assertEqual(s["totals"]["source"], "history")
                self.assertEqual(s["totals"]["issues"], {"done": 11, "scope": 37})
                self.assertEqual(s["carry_out"]["issues"], 26)
                # NEGATIVE: no carry-in from today's data on a closed cycle.
                self.assertIsNone(s["carry_in"])

class SummarizeEndedButNotClosed(unittest.TestCase):
    """Cycle 4 after its endsAt but before Linear's close job has run (or an
    --as-of past the current cycle's end). History is a running sample and
    uncompletedIssuesUponClose is [], so neither is the carry-out yet."""

    AFTER_END = fx.at("2026-08-24T00:00:00Z")

    def setUp(self):
        cs = fx.cycles()
        self.c3, self.c4 = fx.by_number(cs, 3), fx.by_number(cs, 4)
        self.c4.update(issueCountHistory=[30, 31], completedIssueCountHistory=[5, 6],
                       scopeHistory=[90, 95], completedScopeHistory=[15, 20])
        self.members = [fx.issue(1, 3), fx.issue(2, 2, state="unstarted"), fx.issue(3, 5, state="started")]

    def _summarize(self, uncompleted=None, **kw):
        return cycles.summarize(self.c4, self.members, uncompleted, now=self.AFTER_END, **kw)

    def test_state_is_ended_but_flagged_not_closed(self):
        s = self._summarize([])
        self.assertEqual((s["cycle"]["state"], s["cycle"]["closed"]), ("ended", False))
        self.assertIsNone(s["cycle"]["days_left"])

    # NEGATIVE: the partial running history must not be quoted as the result.
    def test_totals_are_membership_not_the_running_history(self):
        s = self._summarize([])
        self.assertEqual(s["totals"], {
            "source": "membership",
            "issues": {"done": 1, "scope": 3},
            "points": {"done": 3, "scope": 10},
        })

    # NEGATIVE: Linear's [] before close is not "nothing carried out", and must
    # not shrink the rows to the completed members (~100%).
    def test_empty_uncompleted_before_close_gives_no_carry_out_and_full_rows(self):
        s = self._summarize([])
        self.assertIsNone(s["carry_out"])
        self.assertEqual(s["split"]["source"], "membership, current labels")
        self.assertEqual(s["split"]["planned"]["issues"], {"done": 1, "scope": 3})

    def test_empty_history_before_close_is_membership_too(self):
        for key in ("issueCountHistory", "completedIssueCountHistory", "scopeHistory", "completedScopeHistory"):
            self.c4[key] = []
        s = self._summarize()
        self.assertEqual(s["totals"]["source"], "membership")
        self.assertIsNone(s["carry_out"])

    def test_carry_in_from_a_closed_predecessor_still_applies(self):
        members = self.members + fx.c3_uncompleted()[:2]
        s = cycles.summarize(self.c4, members, None, fx.c3_uncompleted(), prev_cycle=self.c3, now=self.AFTER_END)
        self.assertEqual((s["carry_in"]["from_cycle"], s["carry_in"]["issues"]), (3, 2))

class SummarizeCreatedMidCycleBoundaries(unittest.TestCase):
    def _count(self, created):
        c3 = fx.by_number(fx.cycles(), 3)
        return cycles.summarize(c3, [fx.issue(1, 1, created=created)], [], now=MID_DAY_AFTER)["created_mid_cycle"]

    def test_created_at_the_start_instant_counts(self):
        self.assertEqual(self._count("2026-08-09T04:00:00.000Z"), 1)

    # NEGATIVE: the end instant belongs to the next cycle.
    def test_created_at_the_end_instant_does_not_count(self):
        self.assertEqual(self._count("2026-08-16T04:00:00.000Z"), 0)

    # NEGATIVE: one second before start is pre-cycle work.
    def test_created_before_the_start_does_not_count(self):
        self.assertEqual(self._count("2026-08-09T03:59:59.000Z"), 0)


class SummarizeActiveCycle(unittest.TestCase):
    """Cycle 4 on the day it opened, holding cycle 3's rolled-over work."""

    def setUp(self):
        cs = fx.cycles()
        self.c3, self.c4 = fx.by_number(cs, 3), fx.by_number(cs, 4)
        new = [
            fx.issue(500, 3, labels=("DOT", "driven:agent-auto"), created="2026-08-16T09:00:00.000Z"),
            fx.issue(501, 2, state="unstarted", labels=("DOT",), created="2026-08-16T09:00:00.000Z"),
            fx.issue(502, 5, state="unstarted", labels=("MT",)),
            fx.issue(503, 1, state="unstarted", labels=("MTA", "adhoc")),
            fx.issue(504, 2, state="unstarted", labels=("MTA", "adhoc")),
            fx.issue(505, 3, state="backlog", labels=("DOT",)),
        ]
        self.members = fx.c3_uncompleted() + new
        # SB-400 was carried out of cycle 3 but has since left cycle 4.
        self.prev_uncompleted = fx.c3_uncompleted() + [fx.issue(400, 8, state="backlog", labels=("DOT",))]
        self.s = cycles.summarize(
            self.c4, self.members, None, self.prev_uncompleted, prev_cycle=self.c3, now=MID_DAY_AFTER
        )

    def test_state_is_active_with_days_left(self):
        self.assertEqual(self.s["cycle"]["state"], "active")
        self.assertEqual(self.s["cycle"]["days_left"], 7)

    def test_carry_in_is_the_predecessors_uncompleted_still_in_this_cycle(self):
        ci = self.s["carry_in"]
        self.assertEqual(ci["from_cycle"], 3)
        self.assertEqual((ci["issues"], ci["points"]), (26, 71))
        self.assertEqual(ci["planned"], {"issues": 19, "points": 52})
        self.assertEqual(ci["adhoc"], {"issues": 7, "points": 19})
        self.assertEqual(ci["share_of_members"], round(100 * 26 / 32))

    # NEGATIVE: an issue carried out of cycle 3 that is no longer a member is excluded.
    def test_carry_in_excludes_identifiers_not_in_members(self):
        self.assertNotIn("SB-400", self.s["carry_in"]["identifiers"])
        self.assertEqual(len(self.s["carry_in"]["identifiers"]), 26)

    def test_carry_out_is_none_for_an_active_cycle(self):
        self.assertIsNone(self.s["carry_out"])
        # Even if an uncompleted set is handed in: nothing has closed yet.
        s = cycles.summarize(self.c4, self.members, fx.c3_uncompleted(), now=MID_DAY_AFTER)
        self.assertIsNone(s["carry_out"])

    def test_totals_come_from_membership(self):
        self.assertEqual(self.s["totals"], {
            "source": "membership",
            "issues": {"done": 1, "scope": 32},
            "points": {"done": 3, "scope": 71 + 16},
        })

    def test_planning_stamp_is_skipped_with_its_note(self):
        self.assertEqual(self.s["planning"],
                         {"status": "skipped", "note": "focus set ad hoc 2026-08-16", "stamped": True})

    def test_state_is_active_and_not_closed(self):
        self.assertFalse(self.s["cycle"]["closed"])

    # NEGATIVE: without the predecessor there is no way to know it closed.
    def test_no_prev_cycle_means_no_carry_in(self):
        s = cycles.summarize(self.c4, self.members, None, self.prev_uncompleted, now=MID_DAY_AFTER)
        self.assertIsNone(s["carry_in"])

    # NEGATIVE: a predecessor Linear has not closed has no meaningful carry-out.
    def test_a_predecessor_not_yet_closed_gives_no_carry_in(self):
        self.c3["completedAt"] = None
        s = cycles.summarize(self.c4, self.members, None, self.prev_uncompleted, prev_cycle=self.c3,
                             now=MID_DAY_AFTER)
        self.assertIsNone(s["carry_in"])

    # NEGATIVE: no predecessor set, no carry-in section.
    def test_no_prev_uncompleted_means_no_carry_in(self):
        s = cycles.summarize(self.c4, self.members, now=MID_DAY_AFTER)
        self.assertIsNone(s["carry_in"])

    def test_an_active_cycle_with_no_members_does_not_divide_by_zero(self):
        s = cycles.summarize(self.c4, [], None, self.prev_uncompleted, prev_cycle=self.c3, now=MID_DAY_AFTER)
        self.assertEqual(s["carry_in"]["issues"], 0)
        self.assertIsNone(s["carry_in"]["share_of_members"])
        self.assertEqual(s["adhoc_share"], {"issues": None, "points": None})


class SummarizeFutureCycle(unittest.TestCase):
    def setUp(self):
        self.c4 = fx.by_number(fx.cycles(), 4)
        self.now = fx.at("2026-08-10T00:00:00Z")

    def test_empty_history_does_not_crash_and_has_no_carry(self):
        s = cycles.summarize(self.c4, [], [], fx.c3_uncompleted(), prev_cycle=fx.by_number(fx.cycles(), 3),
                             now=self.now)
        self.assertEqual(s["cycle"]["state"], "future")
        self.assertIsNone(s["cycle"]["days_left"])
        self.assertIsNone(s["carry_in"])
        self.assertIsNone(s["carry_out"])
        self.assertEqual(s["totals"], {"source": "membership",
                                       "issues": {"done": 0, "scope": 0}, "points": {"done": 0, "scope": 0}})
        self.assertEqual(s["adhoc_share"], {"issues": None, "points": None})
        self.assertEqual(s["delivered_by"], {})
        self.assertEqual(s["unestimated"], [])

    def test_future_cycle_with_planned_members(self):
        s = cycles.summarize(self.c4, [fx.issue(1, 3, state="unstarted"), fx.issue(2, None, state="unstarted",
                                                                                   labels=("adhoc",))],
                             now=self.now)
        self.assertEqual(s["totals"]["issues"], {"done": 0, "scope": 2})
        self.assertEqual(s["adhoc_share"], {"issues": 50, "points": 0})


class SummarizeUnestimated(unittest.TestCase):
    def setUp(self):
        c4 = fx.by_number(fx.cycles(), 4)
        self.members = [
            fx.issue(1, None, state="unstarted", title="no estimate yet"),
            fx.issue(2, None, labels=("adhoc",), title="adhoc, no estimate"),
            fx.issue(3, 0, labels=("adhoc",), title="closed as duplicate"),
            fx.issue(4, 5),
        ]
        self.s = cycles.summarize(c4, self.members, now=MID_DAY_AFTER)

    def test_estimate_none_is_flagged_with_its_adhoc_tag(self):
        self.assertEqual(self.s["unestimated"], [
            {"identifier": "SB-1", "title": "no estimate yet", "adhoc": False},
            {"identifier": "SB-2", "title": "adhoc, no estimate", "adhoc": True},
        ])

    # NEGATIVE: 0 is a deliberate estimate (superseded/duplicate), not missing.
    def test_estimate_zero_is_not_flagged(self):
        self.assertNotIn("SB-3", [u["identifier"] for u in self.s["unestimated"]])

    def test_unestimated_issues_count_as_zero_points(self):
        self.assertEqual(self.s["totals"]["points"], {"done": 5, "scope": 5})
        self.assertEqual(self.s["totals"]["issues"], {"done": 3, "scope": 4})


class SummarizeIsJsonSafe(unittest.TestCase):
    def test_every_phase_round_trips_through_json(self):
        cs = fx.cycles()
        c2, c3, c4 = (fx.by_number(cs, n) for n in (2, 3, 4))
        cases = {
            "ended": cycles.summarize(c3, fx.c3_members(), fx.c3_uncompleted(), now=MID_DAY_AFTER),
            "active": cycles.summarize(c4, fx.c3_uncompleted(), None, fx.c3_uncompleted(), prev_cycle=c3,
                                       now=MID_DAY_AFTER),
            "future": cycles.summarize(c4, [], now=fx.at("2026-08-01T00:00:00Z")),
            "ended-no-uncompleted": cycles.summarize(c2, [], None, now=MID_DAY_AFTER),
        }
        for name, s in cases.items():
            with self.subTest(phase=name):
                # Equality after a round trip catches tuples, sets and datetimes,
                # which json.dumps would either reject or silently reshape.
                self.assertEqual(json.loads(json.dumps(s)), s)


class Helpers(unittest.TestCase):
    def test_driven_reads_the_first_driven_label(self):
        self.assertEqual(cycles.driven(fx.issue(1, 1, labels=("DOT", "driven:agent-auto"))), "agent-auto")

    # NEGATIVE: no driven:* label is "unlabelled", not a guess.
    def test_driven_without_a_label_is_unlabelled(self):
        self.assertEqual(cycles.driven(fx.issue(1, 1, labels=("adhoc",))), "unlabelled")

    def test_pct_of_zero_whole_is_none(self):
        self.assertIsNone(cycles.pct(0, 0))
        self.assertEqual(cycles.pct(1, 3), 33)


if __name__ == "__main__":
    unittest.main()
