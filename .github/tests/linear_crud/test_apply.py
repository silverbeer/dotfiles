"""plan() in backlog-groom/scripts/apply.py — the idempotence step.

apply.py's contract is that running it twice is a no-op. plan() is the pure
part of that: given the approved changes and the live state, it returns only
the fields that differ. Applying its own output and planning again must yield
nothing.
"""

import copy
import unittest

from _load import SKILLS_DIR, load_module

apply = load_module("apply", "backlog-groom", "apply.py")

PROJECTS = {"Quality & CI Automation": "proj-qa", "Paper AI Proof of Concept": "proj-paper"}


def _changes():
    return [
        {"id": "SB-1", "title": "one", "priority": 2, "estimate": 3, "project": "Quality & CI Automation"},
        {"id": "SB-2", "title": "two", "priority": 1},
        {"id": "SB-3", "title": "three", "estimate": 5, "project": "Paper AI Proof of Concept"},
        {"id": "SB-4", "title": "four", "priority": 3, "estimate": 1},
    ]


def _live():
    return {
        "SB-1": {"id": "u1", "identifier": "SB-1", "priority": 4, "estimate": None, "project": None},
        "SB-2": {"id": "u2", "identifier": "SB-2", "priority": 1, "estimate": 2, "project": None},
        "SB-3": {"id": "u3", "identifier": "SB-3", "priority": 0, "estimate": 5,
                 "project": {"id": "proj-qa", "name": "Quality & CI Automation"}},
        # Already exactly right: must be skipped, not planned.
        "SB-4": {"id": "u4", "identifier": "SB-4", "priority": 3, "estimate": 1, "project": None},
    }


def _simulate_apply(live, planned):
    """What Linear would look like after the issueUpdate mutations ran."""
    after = copy.deepcopy(live)
    by_id = {v: k for k, v in PROJECTS.items()}
    for c, _, fields in planned:
        cur = after[c["id"]]
        for k, v in fields.items():
            if k == "projectId":
                cur["project"] = {"id": v, "name": by_id[v]}
            else:
                cur[k] = v
    return after


class Plan(unittest.TestCase):
    # apply.py used to import the DEPLOYED ~/.claude linear_api even under
    # test; the scratch copy must be the one that answered.
    def test_linear_api_is_the_scratch_copy(self):
        import linear_api
        self.assertTrue(linear_api.__file__.startswith(str(SKILLS_DIR)), linear_api.__file__)

    def test_only_differing_fields_are_planned(self):
        planned, skipped = apply.plan(_changes(), _live(), PROJECTS)
        got = {c["id"]: fields for c, _, fields in planned}
        self.assertEqual(got, {
            "SB-1": {"priority": 2, "estimate": 3, "projectId": "proj-qa"},
            "SB-3": {"projectId": "proj-paper"},
        })
        self.assertEqual(skipped, 2)

    def test_is_deterministic_on_the_same_input(self):
        first = apply.plan(_changes(), _live(), PROJECTS)
        second = apply.plan(_changes(), _live(), PROJECTS)
        self.assertEqual(first, second)

    def test_second_run_after_apply_plans_nothing(self):
        changes = _changes()
        planned, _ = apply.plan(changes, _live(), PROJECTS)
        after = _simulate_apply(_live(), planned)
        planned2, skipped2 = apply.plan(changes, after, PROJECTS)
        self.assertEqual(planned2, [])
        self.assertEqual(skipped2, len(changes))

    # NEGATIVE: a field missing from the change set is not "differs from
    # None" — it is untouched. SB-2 sets only priority; its estimate stays.
    def test_absent_fields_are_not_planned(self):
        planned, _ = apply.plan(_changes(), _live(), PROJECTS)
        self.assertNotIn("SB-2", {c["id"] for c, _, _ in planned})

    # NEGATIVE: a change that does differ must NOT be skipped.
    def test_a_real_difference_is_never_skipped(self):
        live = _live()
        live["SB-4"]["priority"] = 0
        planned, skipped = apply.plan(_changes(), live, PROJECTS)
        self.assertIn("SB-4", {c["id"] for c, _, _ in planned})
        self.assertEqual(skipped, 1)


if __name__ == "__main__":
    unittest.main()
