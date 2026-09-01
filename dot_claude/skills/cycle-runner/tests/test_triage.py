"""triage.py: the triage-trigger predicate (needs_triage), the type/estimate/
driven drafting logic (draft_changes/choose_driven/guess_type — especially
that driven:agent-auto is only proposed off REAL estimate/labels, never a
freshly-guessed one), idempotency of `propose` on unchanged input, and
`apply`'s pure drift-refusal step (plan()) plus its wiring in cmd_apply.

All Linear access goes through `triage.gql`, monkeypatched to FakeGql below —
no subprocess, no `linear` CLI, no network (same monkeypatch shape pick.py's
and gate.py's tests use for their own `gql`).
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import triage  # noqa: E402


def fetch_issue(
    identifier="SB-1",
    title="Some title",
    estimate=None,
    state_type="unstarted",
    state_name="Todo",
    labels=(),
):
    """One Q_TRIAGE-shaped node — the shape `fetch()`/`needs_triage()`/
    `draft_changes()` all consume."""
    return {
        "identifier": identifier,
        "title": title,
        "url": f"https://linear.app/silverbeer/issue/{identifier}",
        "estimate": estimate,
        "createdAt": "2026-01-01T00:00:00.000Z",
        "state": {"name": state_name, "type": state_type},
        "labels": {"nodes": [{"name": n} for n in labels]},
    }


def live_node(identifier="SB-1", linear_id="id-1", estimate=None, labels=()):
    """One current_state()-shaped node — includes label ids, which propose's
    fetch-shape does not need and cmd_apply's mutation does."""
    return {
        "id": linear_id,
        "identifier": identifier,
        "estimate": estimate,
        "labels": {"nodes": [{"id": f"lblid-{n}", "name": n} for n in labels]},
    }


class FakeGql:
    """Records every query/mutation string it's called with and answers the
    three read queries + the mutation triage.py issues, distinguished by the
    substrings unique to each (no query language parsing needed)."""

    def __init__(self, fetch_nodes=None, live_by_id=None, label_map=None, fail_linear_ids=()):
        self.fetch_nodes = fetch_nodes or []
        self.live_by_id = live_by_id or {}
        self.label_map = label_map or {}
        # cur["id"] values (the Linear internal id, e.g. "lid-1") whose
        # issueUpdate mutation should come back success:false — for testing
        # cmd_apply's per-issue failure handling.
        self.fail_linear_ids = set(fail_linear_ids)
        self.calls: list[str] = []
        self.mutations: list[str] = []

    def __call__(self, query, variables=None):
        self.calls.append(query)
        if query.strip().startswith("mutation"):
            self.mutations.append(query)
            failed = any(f'"{lid}"' in query for lid in self.fail_linear_ids)
            return {"issueUpdate": {"success": not failed}}
        if "issueLabels(first: 250)" in query:
            return {"issueLabels": {"nodes": [{"id": lid, "name": n} for n, lid in self.label_map.items()]}}
        if "number:{in:[" in query:
            return {"issues": {"nodes": list(self.live_by_id.values())}}
        if 'team:{key:{eq:"' in query:
            return {"issues": {"nodes": self.fetch_nodes}}
        raise AssertionError(f"FakeGql: unexpected query:\n{query}")


class GqlPatchMixin:
    """Monkeypatches triage.gql for the duration of one test — same
    addCleanup/restore idiom test_pick.py uses for pick.gql."""

    def patch_gql(self, fake):
        old = triage.gql
        triage.gql = fake
        self.addCleanup(lambda: setattr(triage, "gql", old))
        return fake


# --------------------------------------------------------------- guess_type


class GuessTypeTests(unittest.TestCase):
    def test_security_keyword_wins(self):
        self.assertEqual(triage.guess_type("Rotate the leaked credential"), "security")

    def test_bug_keyword(self):
        self.assertEqual(triage.guess_type("Fix the crash on startup"), "bug")

    def test_docs_keyword(self):
        self.assertEqual(triage.guess_type("Update the README"), "docs")

    def test_infra_keyword(self):
        self.assertEqual(triage.guess_type("launchd plist for the new agent"), "infra")

    def test_feature_keyword(self):
        self.assertEqual(triage.guess_type("Add support for dark mode"), "feature")

    def test_no_keyword_match_falls_back_to_chore(self):
        self.assertEqual(triage.guess_type("Rearrange the dashboard widgets"), "chore")

    # Ordering: security/bug are checked before the generic feature/chore, so
    # an urgent-sounding title is never swallowed by a looser later match.
    def test_security_beats_feature_when_title_matches_both(self):
        self.assertEqual(triage.guess_type("Add a fix for the leaked secret"), "security")


# ------------------------------------------------------------- needs_triage


class NeedsTriageTests(unittest.TestCase):
    def test_triage_state_always_needs_triage_even_if_fully_labeled(self):
        i = fetch_issue(state_type="triage", estimate=2, labels=["chore", "driven:agent-supervised"])
        self.assertTrue(triage.needs_triage(i))

    def test_missing_type_label_needs_triage(self):
        i = fetch_issue(estimate=2, labels=["driven:agent-supervised"])
        self.assertTrue(triage.needs_triage(i))

    def test_missing_estimate_needs_triage(self):
        i = fetch_issue(estimate=None, labels=["chore", "driven:agent-supervised"])
        self.assertTrue(triage.needs_triage(i))

    def test_missing_driven_label_needs_triage(self):
        i = fetch_issue(estimate=2, labels=["chore"])
        self.assertTrue(triage.needs_triage(i))

    def test_fully_triaged_issue_does_not_need_triage(self):
        i = fetch_issue(state_type="unstarted", estimate=2, labels=["chore", "driven:agent-supervised"])
        self.assertFalse(triage.needs_triage(i))

    # driven:human counts as a real driven:* label — it just means "never
    # picked", not "needs triage".
    def test_driven_human_satisfies_the_driven_requirement(self):
        i = fetch_issue(estimate=2, labels=["chore", "driven:human"])
        self.assertFalse(triage.needs_triage(i))

    def test_multiple_gaps_still_just_true(self):
        i = fetch_issue(estimate=None, labels=[])
        self.assertTrue(triage.needs_triage(i))


# -------------------------------------------------------------- choose_driven


class ChooseDrivenTests(unittest.TestCase):
    """Reuses pick.policy_ok — this is the core "no guess unlocks autonomy"
    guarantee, tested directly against the function that makes the call."""

    def test_real_low_estimate_with_adhoc_is_agent_auto(self):
        self.assertEqual(triage.choose_driven(2, {"adhoc"}), "agent-auto")

    def test_real_low_estimate_with_chore_is_agent_auto(self):
        self.assertEqual(triage.choose_driven(1, {"chore"}), "agent-auto")

    def test_real_low_estimate_without_adhoc_or_chore_is_supervised(self):
        self.assertEqual(triage.choose_driven(1, {"feature"}), "agent-supervised")

    def test_real_high_estimate_is_supervised_even_with_adhoc(self):
        self.assertEqual(triage.choose_driven(5, {"adhoc"}), "agent-supervised")

    # The critical guarantee: GUESS_ESTIMATE (3) is deliberately above the
    # <=2 threshold, so passing it through choose_driven — exactly what
    # draft_changes does when the estimate had to be guessed — can never
    # produce agent-auto, regardless of what labels are on the issue.
    def test_guess_estimate_constant_can_never_unlock_agent_auto(self):
        self.assertGreater(triage.GUESS_ESTIMATE, 2)
        self.assertEqual(triage.choose_driven(triage.GUESS_ESTIMATE, {"adhoc"}), "agent-supervised")
        self.assertEqual(triage.choose_driven(triage.GUESS_ESTIMATE, {"chore"}), "agent-supervised")

    def test_no_estimate_at_all_is_supervised(self):
        self.assertEqual(triage.choose_driven(None, {"adhoc"}), "agent-supervised")


# -------------------------------------------------------------- draft_changes


class DraftChangesTests(unittest.TestCase):
    def test_fully_triaged_issue_drafts_nothing_but_id_and_title(self):
        i = fetch_issue(estimate=2, labels=["chore", "driven:agent-supervised"])
        d = triage.draft_changes(i)
        self.assertEqual(d, {"id": i["identifier"], "title": i["title"]})

    def test_missing_type_drafts_a_guessed_type(self):
        i = fetch_issue(title="Fix the crash", estimate=2, labels=["driven:agent-supervised"])
        d = triage.draft_changes(i)
        self.assertEqual(d["type"], "bug")
        self.assertNotIn("estimate", d)
        self.assertNotIn("driven", d)

    def test_missing_estimate_drafts_the_guess_constant(self):
        i = fetch_issue(estimate=None, labels=["chore", "driven:agent-supervised"])
        d = triage.draft_changes(i)
        self.assertEqual(d["estimate"], triage.GUESS_ESTIMATE)
        self.assertNotIn("type", d)
        self.assertNotIn("driven", d)

    def test_missing_driven_with_real_eligible_estimate_and_labels_drafts_agent_auto(self):
        i = fetch_issue(estimate=2, labels=["chore"])  # real estimate, real adhoc/chore-equivalent label
        d = triage.draft_changes(i)
        self.assertEqual(d["driven"], "agent-auto")
        self.assertNotIn("type", d)  # "chore" already satisfies TYPE_GROUP
        self.assertNotIn("estimate", d)

    def test_missing_driven_with_real_ineligible_estimate_drafts_agent_supervised(self):
        i = fetch_issue(estimate=5, labels=["chore"])
        d = triage.draft_changes(i)
        self.assertEqual(d["driven"], "agent-supervised")

    # The load-bearing case: estimate is ALSO missing, so it gets guessed —
    # and that guess must never leak into an agent-auto proposal even though
    # the issue already carries adhoc/chore.
    def test_missing_estimate_and_driven_together_never_drafts_agent_auto(self):
        i = fetch_issue(estimate=None, labels=["adhoc", "chore"])
        d = triage.draft_changes(i)
        self.assertEqual(d["estimate"], triage.GUESS_ESTIMATE)
        self.assertEqual(d["driven"], "agent-supervised")

    def test_driven_eligibility_uses_real_labels_not_a_freshly_guessed_type(self):
        # No type label at all (so "type" gets guessed as a side effect), and
        # no adhoc/chore either -> agent-auto must NOT be unlocked by the
        # guessed type, however that guess turns out.
        i = fetch_issue(title="Add a new widget", estimate=2, labels=[])
        d = triage.draft_changes(i)
        self.assertIn("type", d)  # a type guess did happen
        self.assertEqual(d["driven"], "agent-supervised")

    def test_all_three_gaps_at_once(self):
        i = fetch_issue(title="broken pipeline", estimate=None, labels=[])
        d = triage.draft_changes(i)
        self.assertEqual(d["type"], "bug")
        self.assertEqual(d["estimate"], triage.GUESS_ESTIMATE)
        self.assertEqual(d["driven"], "agent-supervised")

    def test_never_touches_epic_or_priority(self):
        i = fetch_issue(title="broken pipeline", estimate=None, labels=[])
        d = triage.draft_changes(i)
        self.assertEqual(set(d) - {"id", "title", "type", "estimate", "driven"}, set())


# ------------------------------------------------------------------ render_review


class RenderReviewTests(unittest.TestCase):
    def test_changed_and_untouched_sections_split_correctly(self):
        changed_issue = fetch_issue(identifier="SB-1", title="Fix crash", estimate=None, labels=[])
        untouched_issue = fetch_issue(
            identifier="SB-2", title="Already triaged", state_type="triage",
            estimate=2, labels=["chore", "driven:agent-supervised"],
        )
        drafts = [triage.draft_changes(changed_issue), triage.draft_changes(untouched_issue)]
        issues_by_id = {"SB-1": changed_issue, "SB-2": untouched_issue}

        out = triage.render_review("2026-01-01", drafts, issues_by_id)

        self.assertIn("Proposed changes (1)", out)
        self.assertIn("SB-1", out)
        self.assertIn("guessed from title keywords", out)
        self.assertIn("Already fully triaged, still in Triage state (1)", out)
        self.assertIn("SB-2", out)
        self.assertIn("Nothing has been written to Linear", out)

    def test_no_issues_needing_triage_renders_zero_counts(self):
        out = triage.render_review("2026-01-01", [], {})
        self.assertIn("0 issue(s) need triage", out)
        self.assertIn("Proposed changes (0)", out)
        self.assertNotIn("Already fully triaged", out)


# -------------------------------------------------------------------- propose


class ProposeTests(unittest.TestCase, GqlPatchMixin):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _run(self, fetch_nodes, out_review, out_apply):
        self.patch_gql(FakeGql(fetch_nodes=fetch_nodes))
        args = SimpleNamespace(
            team="SB", out_review=str(out_review), out_apply=str(out_apply), today="2026-01-01",
        )
        rc = triage.cmd_propose(args)
        self.assertEqual(rc, 0)

    def test_only_issues_needing_triage_are_written(self):
        needs = fetch_issue(identifier="SB-1", estimate=None, labels=[])
        fine = fetch_issue(identifier="SB-2", estimate=2, labels=["chore", "driven:agent-supervised"])
        out_review = Path(self._tmp.name) / "review.md"
        out_apply = Path(self._tmp.name) / "apply.json"
        self._run([fine, needs], out_review, out_apply)

        payload = json.loads(out_apply.read_text())
        ids = [c["id"] for c in payload["changes"]]
        self.assertEqual(ids, ["SB-1"])  # SB-2 never needed triage at all

    def test_apply_json_shape_matches_what_apply_expects(self):
        needs = fetch_issue(identifier="SB-1", title="fix it", estimate=None, labels=[])
        out_review = Path(self._tmp.name) / "review.md"
        out_apply = Path(self._tmp.name) / "apply.json"
        self._run([needs], out_review, out_apply)

        payload = json.loads(out_apply.read_text())
        self.assertEqual(payload["generated_for"], "2026-01-01")
        [change] = payload["changes"]
        self.assertEqual(change["id"], "SB-1")
        self.assertIn("estimate", change)

    def test_propose_is_idempotent_on_unchanged_input(self):
        issues = [
            fetch_issue(identifier="SB-2", title="Add a widget", estimate=None, labels=[]),
            fetch_issue(identifier="SB-1", title="Fix a crash", estimate=2, labels=[]),
        ]
        review_a = Path(self._tmp.name) / "review_a.md"
        apply_a = Path(self._tmp.name) / "apply_a.json"
        self._run(list(issues), review_a, apply_a)

        review_b = Path(self._tmp.name) / "review_b.md"
        apply_b = Path(self._tmp.name) / "apply_b.json"
        self._run(list(issues), review_b, apply_b)

        self.assertEqual(review_a.read_text(), review_b.read_text())
        self.assertEqual(apply_a.read_text(), apply_b.read_text())

    def test_propose_records_team_in_apply_json(self):
        # apply-time current_state() must filter by this SAME team, not a
        # hardcoded "SB" — see the ApplyTests coverage below.
        needs = fetch_issue(identifier="OTHER-1", estimate=None, labels=[])
        out_review = Path(self._tmp.name) / "review.md"
        out_apply = Path(self._tmp.name) / "apply.json"
        self.patch_gql(FakeGql(fetch_nodes=[needs]))
        args = SimpleNamespace(team="OTHER", out_review=str(out_review), out_apply=str(out_apply), today="2026-01-01")
        rc = triage.cmd_propose(args)
        self.assertEqual(rc, 0)
        payload = json.loads(out_apply.read_text())
        self.assertEqual(payload["team"], "OTHER")

    def test_propose_output_ordering_is_stable_regardless_of_fetch_order(self):
        a = fetch_issue(identifier="SB-1", estimate=None, labels=[])
        b = fetch_issue(identifier="SB-2", estimate=None, labels=[])
        out_review1 = Path(self._tmp.name) / "r1.md"
        out_apply1 = Path(self._tmp.name) / "a1.json"
        self._run([a, b], out_review1, out_apply1)

        out_review2 = Path(self._tmp.name) / "r2.md"
        out_apply2 = Path(self._tmp.name) / "a2.json"
        self._run([b, a], out_review2, out_apply2)

        self.assertEqual(out_apply1.read_text(), out_apply2.read_text())


# ------------------------------------------------------------------------ plan


class PlanTests(unittest.TestCase):
    """The pure drift-refusal step — no gql involved at all."""

    def test_no_drift_everything_planned(self):
        changes = [{"id": "SB-1", "title": "t", "type": "bug", "estimate": 2, "driven": "agent-auto"}]
        live = {"SB-1": live_node("SB-1", estimate=None, labels=[])}
        planned, skipped, notes = triage.plan(changes, live)
        self.assertEqual(len(planned), 1)
        c, cur, applicable = planned[0]
        self.assertEqual(applicable, {"type": "bug", "estimate": 2, "driven": "agent-auto"})
        self.assertEqual(skipped, 0)
        self.assertEqual(notes, [])

    def test_issue_not_found_in_live_is_skipped_with_a_note(self):
        changes = [{"id": "SB-9", "title": "t", "estimate": 3}]
        planned, skipped, notes = triage.plan(changes, {})
        self.assertEqual(planned, [])
        self.assertEqual(skipped, 1)
        self.assertTrue(any("not found in Linear" in n for n in notes))

    def test_type_already_set_since_propose_is_dropped_not_overwritten(self):
        changes = [{"id": "SB-1", "title": "t", "type": "bug", "estimate": 2}]
        live = {"SB-1": live_node("SB-1", estimate=None, labels=["feature"])}  # drifted: type now set
        planned, skipped, notes = triage.plan(changes, live)
        [(c, cur, applicable)] = planned
        self.assertNotIn("type", applicable)
        self.assertEqual(applicable, {"estimate": 2})
        self.assertTrue(any("partially drifted" in n for n in notes))

    def test_estimate_already_set_since_propose_is_dropped(self):
        changes = [{"id": "SB-1", "title": "t", "estimate": 3}]
        live = {"SB-1": live_node("SB-1", estimate=5, labels=[])}
        planned, skipped, notes = triage.plan(changes, live)
        self.assertEqual(planned, [])
        self.assertEqual(skipped, 1)
        self.assertTrue(any("fully drifted" in n for n in notes))

    def test_driven_already_set_since_propose_is_dropped(self):
        changes = [{"id": "SB-1", "title": "t", "driven": "agent-auto"}]
        live = {"SB-1": live_node("SB-1", estimate=None, labels=["driven:human"])}
        planned, skipped, notes = triage.plan(changes, live)
        self.assertEqual(planned, [])
        self.assertEqual(skipped, 1)
        self.assertTrue(any("driven already set" in n for n in notes))

    def test_fully_drifted_on_every_field_is_skipped_and_noted(self):
        changes = [{"id": "SB-1", "title": "t", "type": "bug", "estimate": 2, "driven": "agent-auto"}]
        live = {"SB-1": live_node("SB-1", estimate=1, labels=["feature", "driven:agent-supervised"])}
        planned, skipped, notes = triage.plan(changes, live)
        self.assertEqual(planned, [])
        self.assertEqual(skipped, 1)
        self.assertTrue(any("fully drifted" in n for n in notes))

    def test_untouched_change_with_no_proposed_fields_is_skipped_quietly(self):
        changes = [{"id": "SB-1", "title": "t"}]
        live = {"SB-1": live_node("SB-1")}
        planned, skipped, notes = triage.plan(changes, live)
        self.assertEqual(planned, [])
        self.assertEqual(skipped, 1)
        self.assertEqual(notes, [])  # nothing was proposed, so nothing to explain


# ------------------------------------------------------------------------- apply


class ApplyTests(unittest.TestCase, GqlPatchMixin):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _changes_file(self, changes, team="SB"):
        p = Path(self._tmp.name) / "apply.json"
        p.write_text(json.dumps({"generated_for": "2026-01-01", "team": team, "changes": changes}))
        return p

    def test_nothing_to_apply_when_every_change_is_id_title_only(self):
        p = self._changes_file([{"id": "SB-1", "title": "t"}])
        fake = self.patch_gql(FakeGql())
        args = SimpleNamespace(changes=str(p), confirm=False)
        rc = triage.cmd_apply(args)
        self.assertEqual(rc, 0)
        self.assertEqual(fake.calls, [])  # never touches Linear at all

    def test_dry_run_plans_but_never_mutates(self):
        p = self._changes_file([{"id": "SB-1", "title": "t", "type": "bug", "estimate": 2}])
        live = {"SB-1": live_node("SB-1", linear_id="lid-1", estimate=None, labels=[])}
        fake = self.patch_gql(FakeGql(live_by_id=live, label_map={"bug": "x"}))
        args = SimpleNamespace(changes=str(p), confirm=False)
        rc = triage.cmd_apply(args)
        self.assertEqual(rc, 0)
        self.assertEqual(fake.mutations, [])

    def test_confirm_writes_labels_and_estimate_for_planned_changes(self):
        p = self._changes_file(
            [{"id": "SB-1", "title": "t", "type": "bug", "estimate": 2, "driven": "agent-auto"}]
        )
        live = {"SB-1": live_node("SB-1", linear_id="lid-1", estimate=None, labels=["adhoc"])}
        fake = self.patch_gql(
            FakeGql(live_by_id=live, label_map={"bug": "lbl-bug", "driven:agent-auto": "lbl-auto", "adhoc": "lbl-adhoc"})
        )
        args = SimpleNamespace(changes=str(p), confirm=True)
        rc = triage.cmd_apply(args)
        self.assertEqual(rc, 0)
        [mutation] = fake.mutations
        self.assertIn('"lid-1"', mutation)
        self.assertIn("lbl-bug", mutation)
        self.assertIn("lbl-auto", mutation)
        self.assertIn("lblid-adhoc", mutation)  # kept existing label id, not replaced
        self.assertIn("estimate: 2", mutation)
        self.assertNotIn("priority", mutation)
        self.assertNotIn("epic", mutation)

    def test_drifted_field_is_skipped_and_not_written_even_with_confirm(self):
        # propose thought "type" was missing; by apply time someone already
        # set it by hand — must not be overwritten, must not appear at all.
        p = self._changes_file([{"id": "SB-1", "title": "t", "type": "bug", "estimate": 2}])
        live = {"SB-1": live_node("SB-1", linear_id="lid-1", estimate=None, labels=["feature"])}
        fake = self.patch_gql(FakeGql(live_by_id=live, label_map={"bug": "lbl-bug"}))
        args = SimpleNamespace(changes=str(p), confirm=True)
        rc = triage.cmd_apply(args)
        self.assertEqual(rc, 0)
        [mutation] = fake.mutations
        self.assertNotIn("lbl-bug", mutation)
        self.assertIn("estimate: 2", mutation)

    def test_fully_drifted_issue_gets_no_mutation_at_all(self):
        p = self._changes_file([{"id": "SB-1", "title": "t", "estimate": 2}])
        live = {"SB-1": live_node("SB-1", linear_id="lid-1", estimate=5, labels=[])}
        fake = self.patch_gql(FakeGql(live_by_id=live, label_map={}))
        args = SimpleNamespace(changes=str(p), confirm=True)
        rc = triage.cmd_apply(args)
        self.assertEqual(rc, 0)
        self.assertEqual(fake.mutations, [])

    def test_issue_missing_from_linear_at_apply_time_is_skipped_not_crashed(self):
        p = self._changes_file([{"id": "SB-9", "title": "t", "estimate": 2}])
        fake = self.patch_gql(FakeGql(live_by_id={}, label_map={}))
        args = SimpleNamespace(changes=str(p), confirm=True)
        rc = triage.cmd_apply(args)
        self.assertEqual(rc, 0)
        self.assertEqual(fake.mutations, [])

    # SB-624 fix: apply must re-fetch by the SAME team propose used, recorded
    # in apply.json — not a hardcoded "SB" — or a non-default --team run's
    # ids never match at apply time and every change looks "not found".
    def test_apply_filters_current_state_by_team_recorded_in_apply_json(self):
        p = self._changes_file([{"id": "OTHER-1", "title": "t", "estimate": 2}], team="OTHER")
        live = {"OTHER-1": live_node("OTHER-1", linear_id="lid-1", estimate=None, labels=[])}
        fake = self.patch_gql(FakeGql(live_by_id=live, label_map={}))
        args = SimpleNamespace(changes=str(p), confirm=False)

        import io
        from contextlib import redirect_stdout

        out = io.StringIO()
        with redirect_stdout(out):
            rc = triage.cmd_apply(args)

        self.assertEqual(rc, 0)
        fetch_query = next(c for c in fake.calls if "number:{in:[" in c)
        self.assertIn('team:{key:{eq:"OTHER"}}', fetch_query)
        # And the change was actually found/planned, not silently dropped
        # (a wrong/hardcoded team would make it look "not found in Linear").
        self.assertIn("1 to update", out.getvalue())

    # SB-624 fix: issueUpdate's `success` field must be checked, not assumed —
    # a stale label id can make Linear return success:false while the code
    # still counts the write and reports "updated".
    def test_failed_write_is_not_counted_and_is_reported(self):
        p = self._changes_file([{"id": "SB-1", "title": "t", "estimate": 2}])
        live = {"SB-1": live_node("SB-1", linear_id="lid-1", estimate=None, labels=[])}
        fake = self.patch_gql(FakeGql(live_by_id=live, label_map={}, fail_linear_ids={"lid-1"}))
        args = SimpleNamespace(changes=str(p), confirm=True)

        import io
        from contextlib import redirect_stderr, redirect_stdout

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = triage.cmd_apply(args)

        self.assertEqual(rc, 0)
        self.assertEqual(len(fake.mutations), 1)  # the write was attempted
        self.assertNotIn("updated SB-1", out.getvalue())
        self.assertIn("0 issues updated", out.getvalue())
        self.assertIn("SB-1", err.getvalue())
        self.assertIn("FAILED", err.getvalue())

    def test_unknown_label_refuses_the_whole_run(self):
        p = self._changes_file([{"id": "SB-1", "title": "t", "type": "bug", "estimate": 2}])
        live = {"SB-1": live_node("SB-1", linear_id="lid-1", estimate=None, labels=[])}
        # "bug" deliberately absent from label_map — simulates a label that
        # does not exist yet in this Linear workspace.
        self.patch_gql(FakeGql(live_by_id=live, label_map={}))
        args = SimpleNamespace(changes=str(p), confirm=True)
        with self.assertRaises(SystemExit) as cm:
            triage.cmd_apply(args)
        self.assertIn("REFUSING", str(cm.exception))
        self.assertIn("bug", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
