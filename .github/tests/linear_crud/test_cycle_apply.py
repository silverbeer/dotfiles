"""cycle_apply.py in po-agent/scripts — the only /cycle writer (SB-1087).

The contract the PO agent leans on: nothing is written without --confirm, a
second run is a no-op, dropping carry-over sends an explicit null cycle,
canceling zeroes the estimate, and a planning stamp past Linear's 255-char cap
refuses before ANY write. main() is driven against a stateful fake Linear that
applies the mutations it receives, so "run twice" is tested end to end.
"""

import contextlib
import copy
import io
import json
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import _cycle_fixtures as fx
from _load import load_module

ca = load_module("cycle_apply", "po-agent", "cycle_apply.py")


def _tripwire(*args, **kwargs):
    raise AssertionError(f"a real gql call was attempted during an offline test: {args!r}")


def setUpModule():
    for mod in (ca, ca.cycles, sys.modules["linear_api"]):
        p = mock.patch.object(mod, "gql", side_effect=_tripwire)
        p.start()
        unittest.addModuleCleanup(p.stop)


# Cycle 4 closed by Linear, cycle 5 running: the week SB-1 moves 4 -> 5 in.
NOW = fx.at("2026-08-24T12:00:00Z")


def _cycles():
    cs = {c["number"]: c for c in fx.cycles() + [fx.c5()]}
    cs[4]["completedAt"] = "2026-08-23T04:00:30.000Z"
    return cs


def _live():
    c4 = {"id": "cycle-4-uuid", "number": 4}
    todo = {"id": "state-todo", "name": "Todo", "type": "unstarted"}
    return {
        "SB-1": {"id": "u1", "identifier": "SB-1", "title": "one", "priority": 3, "estimate": 2, "cycle": c4,
                 "state": todo},
        "SB-2": {"id": "u2", "identifier": "SB-2", "title": "two", "priority": 2, "estimate": 5, "cycle": c4,
                 "state": todo},
        "SB-3": {"id": "u3", "identifier": "SB-3", "title": "three", "priority": 0, "estimate": None, "cycle": None,
                 "state": todo},
        "SB-4": {"id": "u4", "identifier": "SB-4", "title": "four", "priority": 2, "estimate": 3, "cycle": c4,
                 "state": todo},
    }


def _changes():
    return [
        {"identifier": "SB-1", "cycle": 5},                        # move forward
        {"identifier": "SB-2", "cycle": None},                     # drop carry-over
        {"identifier": "SB-3", "priority": 2, "estimate": 3},
        {"identifier": "SB-4", "cancel": True},
    ]


class Validate(unittest.TestCase):
    def test_a_well_formed_change_set_passes(self):
        ca.validate({"cycle": {"number": 9, "planning": "planned", "note": None}, "issues": _changes()})

    # NEGATIVE: each malformed shape is refused before anything is read.
    def test_malformed_changes_are_refused(self):
        bad = [
            {"issues": [{"identifier": "sb-1"}]},
            {"issues": [{"identifier": "SB-1"}, {"identifier": "SB-1"}]},
            {"issues": [{"identifier": "SB-1", "state": "Done"}]},
            {"issues": [{"identifier": "SB-1", "cycle": True}]},
            {"issues": [{"identifier": "SB-1", "cycle": "9"}]},
            {"issues": [{"identifier": "SB-1", "priority": 5}]},
            {"issues": [{"identifier": "SB-1", "estimate": -1}]},
            {"issues": [{"identifier": "SB-1", "cancel": "yes"}]},
            {"issues": [{"identifier": "SB-1", "cancel": True, "estimate": 3}]},
            {"cycle": {"number": 9, "planning": "done"}},
            {"cycle": {"planning": "planned"}},
            # SB-1089: the chat hands this file whatever the model produced.
            {"issues": [{"identifier": "SB-1"}], "stamp": {"number": 9}},
            {"cycle": {"number": 9, "planning": "planned", "state": "active"}},
            {"issues": ["SB-1"]},
            {},
            {"issues": []},
            {"cycle": None, "issues": []},
            [],
        ]
        for changes in bad:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                ca.validate(changes)


class Plan(unittest.TestCase):
    def _plan(self, changes=None, live=None):
        return ca.plan(changes or _changes(), live or _live(), _cycles(), "state-canceled", now=NOW)

    def test_only_differing_fields_are_planned(self):
        planned, skipped = self._plan()
        self.assertEqual({c["identifier"]: fields for c, _, fields in planned}, {
            "SB-1": {"cycleId": "cycle-5-uuid"},
            "SB-2": {"cycleId": None},
            "SB-3": {"priority": 2, "estimate": 3},
            "SB-4": {"estimate": 0, "stateId": "state-canceled"},
        })
        self.assertEqual(skipped, 0)

    def test_cycle_null_emits_an_explicit_null_cycle_id(self):
        planned, _ = self._plan([{"identifier": "SB-2", "cycle": None}])
        fields = planned[0][2]
        self.assertIn("cycleId", fields)
        self.assertEqual(json.dumps(fields), '{"cycleId": null}')

    # NEGATIVE: an absent key is untouched, not "set to null".
    def test_absent_cycle_key_is_not_a_removal(self):
        planned, skipped = self._plan([{"identifier": "SB-2", "priority": 2}])
        self.assertEqual((planned, skipped), ([], 1))

    def test_cancel_sets_the_canceled_state_and_estimate_zero(self):
        planned, _ = self._plan([{"identifier": "SB-3", "cancel": True}])
        self.assertEqual(planned[0][2], {"estimate": 0, "stateId": "state-canceled"})

    def test_second_run_after_apply_plans_nothing(self):
        live = _live()
        planned, _ = self._plan(live=live)
        after = FakeLinear.apply_all(live, planned)
        planned2, skipped2 = self._plan(live=after)
        self.assertEqual(planned2, [])
        self.assertEqual(skipped2, len(_changes()))

    # NEGATIVE: unknown issue or cycle is refused, not skipped.
    def test_unknown_issue_or_cycle_raises(self):
        with self.assertRaisesRegex(LookupError, "SB-99 not found"):
            self._plan([{"identifier": "SB-99", "priority": 1}])
        with self.assertRaisesRegex(LookupError, "cycle 42 not found"):
            self._plan([{"identifier": "SB-1", "cycle": 42}])

    def test_no_canceled_state_raises_only_when_canceling(self):
        with self.assertRaisesRegex(LookupError, "Canceled"):
            ca.plan([{"identifier": "SB-4", "cancel": True}], _live(), _cycles(), None, now=NOW)
        ca.plan([{"identifier": "SB-4", "priority": 1}], _live(), _cycles(), None, now=NOW)


class RunningCycleGuard(unittest.TestCase):
    """Moving an issue out of a cycle that is still running pulls live work out
    early and rewrites that cycle's history; Linear rolls unfinished work at
    close anyway. Refused deterministically, not left to prose."""

    C5 = {"id": "cycle-5-uuid", "number": 5}

    def _live(self):
        live = _live()
        for k in ("SB-1", "SB-2"):
            live[k]["cycle"] = dict(self.C5)
        return live

    def _plan(self, changes, *, now=NOW, cycles=None, **kw):
        return ca.plan(changes, self._live(), cycles or _cycles(), "state-canceled", now=now, **kw)

    # NEGATIVE: out of the running cycle, to another cycle or to none.
    def test_moving_out_of_a_running_cycle_is_refused_listing_every_offender(self):
        with self.assertRaises(ca.ActiveCycleMove) as cm:
            self._plan([{"identifier": "SB-1", "cycle": 4}, {"identifier": "SB-2", "cycle": None},
                        {"identifier": "SB-3", "priority": 1}])
        msg = str(cm.exception)
        self.assertIn("SB-1 is in active cycle 5 (ends 2026-08-30): Linear rolls unfinished work at close"
                      " — apply this cycle change after it closes", msg)
        self.assertIn("SB-2 is in active cycle 5", msg)
        self.assertNotIn("SB-3", msg)

    def test_estimate_priority_and_cancel_on_a_running_cycle_issue_are_allowed(self):
        planned, _ = self._plan([{"identifier": "SB-1", "estimate": 8, "priority": 1},
                                 {"identifier": "SB-2", "cancel": True}])
        self.assertEqual({c["identifier"]: f for c, _, f in planned}, {
            "SB-1": {"priority": 1, "estimate": 8},
            "SB-2": {"estimate": 0, "stateId": "state-canceled"},
        })

    def test_restating_the_current_running_cycle_is_not_a_move(self):
        self.assertEqual(self._plan([{"identifier": "SB-1", "cycle": 5}]), ([], 1))

    def test_moving_from_no_cycle_or_a_closed_cycle_is_allowed(self):
        planned, _ = self._plan([{"identifier": "SB-3", "cycle": 5}, {"identifier": "SB-4", "cycle": None}])
        self.assertEqual({c["identifier"]: f for c, _, f in planned},
                         {"SB-3": {"cycleId": "cycle-5-uuid"}, "SB-4": {"cycleId": None}})

    def test_moving_out_of_a_future_cycle_is_allowed(self):
        planned, _ = self._plan([{"identifier": "SB-1", "cycle": None}], now=fx.at("2026-08-20T12:00:00Z"))
        self.assertEqual(planned[0][2], {"cycleId": None})

    # NEGATIVE: past endsAt but not yet closed by Linear, the rollover is imminent.
    def test_an_ended_but_unclosed_cycle_still_counts_as_running(self):
        cycles = _cycles()
        cycles[4]["completedAt"] = None
        with self.assertRaisesRegex(ca.ActiveCycleMove, "SB-4 is in ended but unclosed cycle 4"):
            self._plan([{"identifier": "SB-4", "cycle": 5}], cycles=cycles)

    def test_the_escape_hatch_allows_the_move(self):
        planned, _ = self._plan([{"identifier": "SB-2", "cycle": None}], allow_active_move=True)
        self.assertEqual(planned[0][2], {"cycleId": None})


class PlanStamp(unittest.TestCase):
    def test_stamps_an_unstamped_cycle(self):
        cycle, after = ca.plan_stamp({"number": 5, "planning": "planned"}, _cycles())
        self.assertEqual((cycle["number"], after), (5, "Planning: planned"))

    def test_restamping_the_same_value_is_nothing_to_write(self):
        self.assertIsNone(ca.plan_stamp({"number": 4, "planning": "skipped", "note": "focus set ad hoc 2026-08-16"},
                                        _cycles()))

    def test_no_planning_value_is_nothing_to_write(self):
        self.assertIsNone(ca.plan_stamp({"number": 5, "planning": None}, _cycles()))
        self.assertIsNone(ca.plan_stamp(None, _cycles()))

    # NEGATIVE: past 255 chars it raises; it never truncates.
    def test_over_255_chars_raises(self):
        with self.assertRaisesRegex(ValueError, "255"):
            ca.plan_stamp({"number": 4, "planning": "skipped", "note": "n" * 60}, _cycles())


class FakeLinear:
    """Just enough of Linear for cycle_apply: reads answer from `live`, and
    mutations are applied to it and recorded."""

    def __init__(self, *, fail_identifier=None):
        self.live = _live()
        self.cycles = _cycles()
        self.mutations = []
        # Simulates Linear rejecting one issueUpdate mid-batch: the mutation is
        # attempted (and recorded) but not applied, and success is False.
        self.fail_identifier = fail_identifier

    @staticmethod
    def apply_all(live, planned):
        after = copy.deepcopy(live)
        for c, cur, fields in planned:
            FakeLinear._apply(after[c["identifier"]], fields)
        return after

    @staticmethod
    def _apply(issue, fields):
        for k, v in fields.items():
            if k == "cycleId":
                issue["cycle"] = None if v is None else {"id": v, "number": int(v.split("-")[1])}
            elif k == "stateId":
                issue["state"] = {"id": v, "name": "Canceled", "type": "canceled"}
            else:
                issue[k] = v

    def __call__(self, query, variables=None):
        if "issueUpdate" in query:
            self.mutations.append(("issueUpdate", variables))
            issue = next(i for i in self.live.values() if i["id"] == variables["id"])
            if issue["identifier"] == self.fail_identifier:
                return {"issueUpdate": {"success": False}}
            self._apply(issue, variables["input"])
            return {"issueUpdate": {"success": True}}
        if "cycleUpdate" in query:
            self.mutations.append(("cycleUpdate", variables))
            cycle = next(c for c in self.cycles.values() if c["id"] == variables["id"])
            cycle["description"] = variables["d"]
            written = {"number": cycle["number"], "description": variables["d"]}
            return {"cycleUpdate": {"success": True, "cycle": written}}
        if "mutation" in query:
            raise AssertionError(f"unexpected mutation: {query}")
        if "cycles(" in query:
            return {"cycles": {"nodes": list(self.cycles.values())}}
        if "workflowStates" in query:
            return {"workflowStates": {"nodes": [{"id": "state-canceled", "name": "Canceled"}]}}
        if "number:{in:$n}" in query:
            return {"issues": {"nodes": [copy.deepcopy(i) for i in self.live.values()
                                         if int(i["identifier"].split("-")[1]) in variables["n"]]}}
        raise AssertionError(f"unexpected query: {query}")


class Main(unittest.TestCase):
    def setUp(self):
        self.fake = FakeLinear()
        for mod in (ca, ca.cycles):
            p = mock.patch.object(mod, "gql", side_effect=self.fake)
            p.start()
            self.addCleanup(p.stop)
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def _run(self, changes, *flags):
        path = f"{self.dir}/changes.json"
        with open(path, "w") as f:
            json.dump(changes, f)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                rc = ca.main(["--changes", path, *flags])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue()

    def _full(self):
        return {"cycle": {"number": 5, "planning": "planned", "note": None}, "issues": _changes()}

    def test_without_confirm_nothing_is_written(self):
        rc, out = self._run(self._full())
        self.assertEqual(rc, 0)
        self.assertEqual(self.fake.mutations, [])
        self.assertIn("dry run — nothing written", out)
        self.assertIn("4 issue(s) to update, 0 already correct; cycle stamp: to write", out)
        self.assertIn("cycle: cycle 4 -> no cycle", out)
        self.assertIn('issueUpdate {"cycleId": null}', out)
        self.assertIn("state: Todo -> Canceled", out)
        self.assertIn("after:  'Planning: planned'", out)

    def test_confirm_writes_issues_then_the_stamp_and_a_second_run_is_a_no_op(self):
        rc, out = self._run(self._full(), "--confirm")
        self.assertEqual(rc, 0)
        self.assertEqual([m[0] for m in self.fake.mutations], ["issueUpdate"] * 4 + ["cycleUpdate"])
        self.assertIn({"id": "u2", "input": {"cycleId": None}}, [m[1] for m in self.fake.mutations])
        self.assertEqual(self.fake.live["SB-4"]["estimate"], 0)
        self.assertIn("4 issue(s) updated, cycle stamped", out)

        self.fake.mutations.clear()
        rc, out = self._run(self._full(), "--confirm")
        self.assertEqual(rc, 0)
        self.assertEqual(self.fake.mutations, [])
        self.assertIn("0 issue(s) to update, 4 already correct; cycle stamp: nothing to write", out)

    # NEGATIVE: an over-long stamp refuses the whole batch, issues included.
    def test_an_over_long_stamp_refuses_before_any_write(self):
        changes = {"cycle": {"number": 4, "planning": "skipped", "note": "n" * 60}, "issues": _changes()}
        rc, _ = self._run(changes, "--confirm")
        self.assertIsInstance(rc, str)
        self.assertIn("REFUSING", rc)
        self.assertIn("255", rc)
        self.assertEqual(self.fake.mutations, [])

    # NEGATIVE: an unknown key refuses the batch, with a message naming it,
    # before Linear is read at all (SB-1089).
    def test_an_unknown_top_level_key_refuses_before_any_read(self):
        rc, _ = self._run({"issues": _changes(), "allow_active_cycle_move": True}, "--confirm")
        self.assertIn("REFUSING", rc)
        self.assertIn("unknown top-level key(s) ['allow_active_cycle_move']", rc)
        self.assertEqual(self.fake.mutations, [])

    def test_an_empty_change_set_is_refused_not_a_silent_no_op(self):
        rc, _ = self._run({"issues": []})
        self.assertIn("REFUSING", rc)
        self.assertIn("the change set is empty", rc)

    def test_a_change_file_that_is_not_json_is_refused(self):
        path = f"{self.dir}/changes.json"
        with open(path, "w") as f:
            f.write("{not json")
        with self.assertRaises(SystemExit) as ctx:
            ca.main(["--changes", path])
        self.assertIn("REFUSING", str(ctx.exception.code))

    # NEGATIVE: an issue Linear does not have refuses the batch.
    def test_an_unknown_issue_refuses_before_any_write(self):
        changes = {"issues": _changes() + [{"identifier": "SB-99", "priority": 1}]}
        rc, _ = self._run(changes, "--confirm")
        self.assertIn("SB-99 not found", rc)
        self.assertEqual(self.fake.mutations, [])

    def _in_running_cycle_5(self):
        for k in ("SB-1", "SB-2"):
            self.fake.live[k]["cycle"] = {"id": "cycle-5-uuid", "number": 5}

    # NEGATIVE: out of the running cycle (to a cycle and to null) refuses the
    # batch under --confirm, before any mutation; the dry run shows why.
    def test_moving_out_of_a_running_cycle_refuses_the_batch_with_zero_mutations(self):
        self._in_running_cycle_5()
        changes = {"cycle": {"number": 5, "planning": "planned"},
                   "issues": [{"identifier": "SB-1", "cycle": 4}, {"identifier": "SB-2", "cycle": None},
                              {"identifier": "SB-3", "estimate": 3}]}
        for flags in ((), ("--confirm",)):
            with self.subTest(flags=flags):
                rc, _ = self._run(changes, *flags)
                self.assertIsInstance(rc, str)
                self.assertIn("REFUSING", rc)
                # main() reads the real clock, so cycle 5 reads as ended-but-unclosed
                # there; the wording for each state is pinned in RunningCycleGuard.
                self.assertRegex(rc, r"SB-1 is in .*cycle 5 \(ends ")
                self.assertRegex(rc, r"SB-2 is in .*cycle 5 \(ends ")
                self.assertIn("nothing written", rc)
                self.assertEqual(self.fake.mutations, [])

    def test_estimate_change_on_a_running_cycle_issue_writes(self):
        self._in_running_cycle_5()
        rc, _ = self._run({"issues": [{"identifier": "SB-1", "estimate": 8}]}, "--confirm")
        self.assertEqual(rc, 0)
        self.assertEqual(self.fake.mutations, [("issueUpdate", {"id": "u1", "input": {"estimate": 8}})])

    def test_allow_active_cycle_move_writes_the_move(self):
        self._in_running_cycle_5()
        rc, _ = self._run({"issues": [{"identifier": "SB-2", "cycle": None}]}, "--confirm",
                          "--allow-active-cycle-move")
        self.assertEqual(rc, 0)
        self.assertEqual(self.fake.mutations, [("issueUpdate", {"id": "u2", "input": {"cycleId": None}})])

    # A rejected issueUpdate mid-batch (SB-1 already-written) must stop before
    # SB-3, SB-4 or the cycle stamp are ever attempted, report exactly how many
    # succeeded, and leave what DID write in place rather than roll it back —
    # re-running is how a partial batch is meant to be finished.
    def test_confirm_stops_on_the_first_failed_update_and_reports_what_was_written(self):
        self.fake.fail_identifier = "SB-2"
        rc, out = self._run(self._full(), "--confirm")
        self.assertIsInstance(rc, str)
        self.assertIn("issueUpdate did not succeed for SB-2 after 1 update(s)", rc)
        self.assertIn("re-run to continue", rc)
        self.assertIn("updated SB-1", out)
        self.assertNotIn("updated SB-3", out)
        self.assertNotIn("updated SB-4", out)

        # SB-1 (before the failure) is actually live; SB-2 (the failure) is
        # not; SB-3/SB-4 (after the failure) were never attempted at all.
        self.assertEqual(self.fake.live["SB-1"]["cycle"]["number"], 5)
        self.assertEqual((self.fake.live["SB-2"]["cycle"] or {}).get("number"), 4)
        self.assertEqual(self.fake.live["SB-3"]["priority"], 0)
        self.assertEqual(self.fake.live["SB-4"]["state"]["type"], "unstarted")
        self.assertEqual([m[0] for m in self.fake.mutations], ["issueUpdate", "issueUpdate"])
        self.assertEqual([v["id"] for _, v in self.fake.mutations], ["u1", "u2"])

        # No stamp: writing it after a failed issue update would record a plan
        # as settled when it is only half-applied.
        self.assertNotIn("cycleUpdate", [m[0] for m in self.fake.mutations])

        # Re-running unmodified is the documented recovery: SB-1 is skipped as
        # already correct, and the run finishes SB-2 onward plus the stamp.
        self.fake.fail_identifier = None
        self.fake.mutations.clear()
        rc, out = self._run(self._full(), "--confirm")
        self.assertEqual(rc, 0)
        self.assertEqual([m[0] for m in self.fake.mutations], ["issueUpdate"] * 3 + ["cycleUpdate"])
        self.assertEqual([v["id"] for _, v in self.fake.mutations[:3]], ["u2", "u3", "u4"])
        self.assertIn("3 issue(s) updated, cycle stamped", out)
        self.assertEqual(self.fake.live["SB-4"]["estimate"], 0)


if __name__ == "__main__":
    unittest.main()
