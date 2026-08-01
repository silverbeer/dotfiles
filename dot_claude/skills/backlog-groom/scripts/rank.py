#!/usr/bin/env python3
"""Turn per-issue rubric scores into priorities, and render a review file.

The scoring is judgement (an agent does it). The *ranking* is arithmetic, and
lives here on purpose: an agent asked to assign P1-P4 per ticket inflates —
every ticket looks important on its own. Scoring against a fixed rubric and
then forcing the distribution keeps priority meaning something.

    python3 rank.py --scores scores.json --input groom-input.json \
        --out-review review.md --out-apply apply.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Linear's scale. 0 = no priority.
URGENT, HIGH, MEDIUM, LOW = 1, 2, 3, 4
NAMES = {0: "None", URGENT: "Urgent", HIGH: "High", MEDIUM: "Medium", LOW: "Low"}

# Target shares of the graded set. A backlog where a third of everything is
# "High" carries no more information than one with no priorities at all.
DISTRIBUTION = [(URGENT, 0.05), (HIGH, 0.20), (MEDIUM, 0.45), (LOW, 0.30)]


def score_of(s: dict) -> float:
    """Weighted rubric score. Higher sorts first.

    impact and blocking dominate; effort is a mild tiebreak, not a driver —
    cheapness is a reason to do something *first*, not a reason to call it
    important. decay covers things that get worse if deferred (security,
    schema drift, data loss).
    """
    return (
        2.0 * s["impact"]
        + 1.5 * s["blocking"]
        + 1.5 * s["decay"]
        - 0.5 * s["effort"]
    )


def assign_priorities(ranked: list[dict]) -> None:
    """Walk the sorted list, cutting at the cumulative distribution boundaries."""
    n = len(ranked)
    cut, idx = 0, 0
    for level, share in DISTRIBUTION:
        cut += share
        boundary = round(n * cut)
        while idx < boundary and idx < n:
            ranked[idx]["priority"] = level
            idx += 1
    while idx < n:  # rounding crumbs land in Low
        ranked[idx]["priority"] = LOW
        idx += 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scores", required=True, help="agent output: rubric scores per issue")
    ap.add_argument("--input", required=True, help="the fetch.py JSON these were scored from")
    ap.add_argument("--out-review", required=True, help="human-readable review file")
    ap.add_argument("--out-apply", required=True, help="machine-readable change set")
    args = ap.parse_args()

    source = json.loads(Path(args.input).read_text())
    issues = {i["id"]: i for i in source["issues"]}
    scores = json.loads(Path(args.scores).read_text())
    rows = scores["issues"] if isinstance(scores, dict) else scores

    unknown = [r["id"] for r in rows if r["id"] not in issues]
    if unknown:
        sys.exit(f"scored issues not present in --input: {', '.join(unknown)}")
    missing = sorted(set(issues) - {r["id"] for r in rows})
    if missing:
        print(f"WARNING: {len(missing)} issue(s) were never scored: {', '.join(missing)}",
              file=sys.stderr)

    for r in rows:
        r["score"] = round(score_of(r), 2)
    ranked = sorted(rows, key=lambda r: (-r["score"], r["id"]))
    assign_priorities(ranked)

    changes = []
    for rank, r in enumerate(ranked, start=1):
        cur = issues[r["id"]]
        delta = {}
        if cur["priority"] != r["priority"]:
            delta["priority"] = r["priority"]
        if r.get("epic") and cur["project"] != r["epic"]:
            delta["project"] = r["epic"]
        if r.get("estimate") and cur["estimate"] != r["estimate"]:
            delta["estimate"] = r["estimate"]
        changes.append({**r, "rank": rank, "current": cur, "changes": delta})

    # ---- review file -----------------------------------------------------
    out = [
        f"# Backlog groom — review ({source['generated_for']})",
        "",
        f"{len(changes)} issues scored. **Nothing has been written to Linear.**",
        "",
        "Edit the `priority` / `project` / `estimate` values below, or delete a whole",
        "block to leave that issue untouched. Then run `apply.py` against the JSON.",
        "",
        "To cancel an issue, do it in Linear directly — this pass does not propose",
        "cancellations.",
        "",
    ]
    by_level: dict[int, list] = {}
    for c in changes:
        by_level.setdefault(c["priority"], []).append(c)

    out += ["## Distribution", "", "| priority | count |", "|---|---|"]
    for level, _ in DISTRIBUTION:
        out.append(f"| {NAMES[level]} | {len(by_level.get(level, []))} |")
    out.append("")

    for level, _ in DISTRIBUTION:
        group = by_level.get(level, [])
        if not group:
            continue
        out += [f"## {NAMES[level]} ({len(group)})", ""]
        for c in group:
            cur = c["current"]
            moved = " ".join(
                f"`{k}: {cur.get('project') if k == 'project' else cur.get(k)} → {v}`"
                for k, v in c["changes"].items()
            )
            out += [
                f"### {c['rank']}. {c['id']} — {cur['title']}",
                "",
                f"- score **{c['score']}** "
                f"(impact {c['impact']}, effort {c['effort']}, "
                f"blocking {c['blocking']}, decay {c['decay']})",
                f"- age {cur['age_days']}d · labels {', '.join(cur['labels']) or '—'}",
                f"- epic: **{c.get('epic') or '(unchanged)'}**"
                + (f" · estimate: **{c['estimate']}**" if c.get("estimate") else ""),
                f"- changes: {moved or '_none_'}",
                f"- _{c.get('rationale', '')}_",
                "",
            ]

    Path(args.out_review).write_text("\n".join(out))
    Path(args.out_apply).write_text(
        json.dumps(
            {
                "generated_for": source["generated_for"],
                "changes": [
                    {"id": c["id"], "title": c["current"]["title"], **c["changes"]}
                    for c in changes
                    if c["changes"]
                ],
            },
            indent=2,
        )
    )

    touched = sum(1 for c in changes if c["changes"])
    print(f"review  → {args.out_review}", file=sys.stderr)
    print(f"apply   → {args.out_apply}  ({touched} issues would change)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
