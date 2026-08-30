# linear-crud — reference

Cold detail moved out of `SKILL.md` (SB-926) so the hot path stays small. Nothing
here is needed to file, view, branch or move a ticket; it is here for when a
command misbehaves, a metric looks wrong, or you need to reach past the wrapper.

## Transport (CLI vs GraphQL)

After SB-922/923 the skill runs on two transports. The `linear` CLI (schpet/tap,
v2.0.0, its own keychain auth) remains behind `new`, `move`, `list`, `epics`,
`audit-unassigned` and `view --full`. Raw GraphQL via `scripts/linear-gql.sh`
(key from `LINEAR_API_KEY` or `~/.config/linear/gql-key`) backs `view`, `pack`,
`branch`, `driven`, `board`, `metrics.sh`, `cycle-report.py` and
`backlog-groom`. Decision: keep both for now. Revisit when SB-937 (pagination)
lands — going GraphQL-only at that point removes the untrusted tap dependency and
the second auth store, which is the only reason the split is tolerated today.

## CLI gotchas (linear 2.0.0, verified 2026-06-20 — the helper targets this)

This build differs sharply from the 2026-05-30 surface — the flags flipped back. Do **not** trust older notes.

- Subcommand is `issue` (**singular**); `issues` (plural) just dumps usage and fails.
- `issue create` takes `-t/--title`, `-l/--label` **repeated** (NOT a comma list), `--description` or `--description-file`, `--project "<epic>"`, and **has** `--no-interactive`. The helper uses all of these.
- **Labels are validated against their group**: `repo`/`type` are mutually-exclusive groups — exactly one of each. `docs` lives in the `type` group (not an area label). Two type labels → `labelIds not exclusive child labels`.
- `issue mine` / `issue list` / `issue l` are **the same command** — they all list *my* issues, not the team's. Needs `--team`, requires `--sort manual|priority` (no default), and has **no `--assignee` or `--json/-j`** — text only. Open states = `--state unstarted --state started`; `--all-states` for everything.
- `issue query` is the real query engine across **all** assignees: `--assignee`, `-U/--unassigned`, `--all-teams`, `--search`, `--project`, `-l/--label`, `--all-states` (its default), and `-j/--json`. The helper's audit uses `issue query --team SB -U`.
- `issue update <id>` uses `-t/--title`, `-s/--state` (name or type), `-a/--assignee`, `-l/--label`, `--project`, `--description`/`-file`.
- `project list --team SB [-j]` lists epics (the helper parses the `-j` JSON: `.nodes[].name`); `project view <slug>` shows one.

## Epics and repos.json

Linear has no native "Epic" — its **Project** is the epic. Every epic must resolve
to a repo via the `epicGlobs` in `repos.json`; to add a new epic, add a lowercase
glob to its repo's entry there (this is the "project-level label" requirement).
Entries are matched in file order, so put a specific glob before a broader one.

Adding a repo = adding an entry to `repos.json`: `label` (the Linear `repo`
label, which must already exist), `dirGlobs` (paths that auto-detect it from
cwd), `ghRepo`, `epicGlobs`. An epic may span repos: `Podtelemetry — Run Audio`
defaults to `POD` (the service) but holds `STK` integration tickets; an explicit
`--repo` overrides the epic's default with a warning rather than failing.

## Stats — `/linear stats [--days N]`

Momentum dashboard: lifetime shipped/open/canceled, velocity over the last N days (default 7), avg open-ticket age, oldest open, approx ship time, per-repo + per-epic breakdown, a 14-day created-per-day sparkline, and a motivational closer. Read-only — runs immediately.
```bash
bash scripts/linear.sh stats            # last 7 days
bash scripts/linear.sh stats --days 30  # last 30 days
```
Computed from `issue query --all-states -j`. **Caveat:** this CLI has no `completedAt`, so "shipped in window" and "avg ship time" use `updatedAt` as the close-time proxy — directional, not exact.

## Delivery board — `/linear board`

Renders a self-contained HTML board for a repo: **ready queue** (tickets with no
*open* blocker — what can actually be started right now) and **critical path**
(longest unbroken chain of `blocks` relations), plus epic cards and a full
ledger with a live blocked-by column. Linear has no view for either of the first
two. Read-only.

```bash
bash scripts/linear.sh board                          # repo detected from cwd
bash scripts/linear.sh board --repo BET --repo BETC   # several labels at once
bash scripts/linear.sh board --epic "BET — Import Platform"
bash scripts/linear.sh board --out /tmp/mt.html
```

Prints the path of the file it wrote. Everything is computed from Linear — epic
order comes from the project `sortOrder`, there is no per-project config.

Degrades cleanly: a repo with no `blocks` relations renders epics + ledger and
says so instead of showing an empty critical path. If the graph contains a
**cycle** the board leads with it — a cycle makes every ticket in the loop
permanently unstartable, and Linear will not warn you.

The board is only worth much once dependencies are actually modelled. Most repos
have none; `BET` is the worked example (108 relations).

**Publishing is manual** — the script writes a file; turning it into a shareable
Artifact is a Claude tool call, not something the script can do.

## DORA metrics — `bash scripts/metrics.sh [--days N]`

Capacity + DORA-style delivery metrics (SB-360). Unlike `stats`, this uses the **Linear API** (`linear-gql.sh`) for real `createdAt/startedAt/completedAt` → true lead time + cycle time, plus deploy frequency (merged-PRs-to-main proxy via `gh search prs`) and a revert/hotfix change-failure proxy. MTTR is not implemented (no incident tracking yet). Read-only.
```bash
bash scripts/metrics.sh --days 30
```

## Cycle report — `python3 scripts/cycle-report.py [--cycle N | --previous]`

Planned vs **adhoc** split for a cycle: issues and points, completion per stream,
adhoc share, and how many issues were created mid-cycle. Read-only.

```bash
python3 scripts/cycle-report.py              # active cycle
python3 scripts/cycle-report.py --previous   # the one that just ended
```

The portfolio is pre-user, so unplanned work is most of the throughput — the
`adhoc` label exists to measure that, not to scold it. Run this at every cycle
boundary and plan the next cycle at roughly `100% − adhoc share` of capacity.

Two things to watch in the output:
- **Unestimated issues make the point totals lie.** The report names them; fill
  them in before trusting the ratio.
- **Adhoc typically completes at a higher rate than planned work** — it jumps the
  queue by definition. If planned completion is much lower, the cycle was
  over-committed, not the team under-delivering.

## Estimates — rationale and history

Fibonacci 1/2/3/5/8. A first guess is fine — revise at close if reality
differed. Use `0` for anything closed as superseded, duplicate or won't-do, so
velocity doesn't count work nobody did (`cycle-report.py` distinguishes a
deliberate 0 from a missing estimate). To (re)set one on an existing issue:

```bash
linear issue update SB-123 --estimate 3
```

Unestimated tickets are not a cosmetic gap: on 2026-07-28 backfilling 18 of them
moved the cycle from an apparent 30 points to an actual 73, and adhoc's share of
points from 34% to 58%. Point totals without estimates understate throughput by
more than half and make capacity planning worthless.

## The `driven` label — `/linear driven SB-N <value>`

`driven:human` · `driven:agent-supervised` · `driven:agent-auto`. **Autonomy of
delivery, not whether an LLM touched the code** — every commit in every
silverbeer repo already carries `Co-Authored-By: Claude`, so authorship cannot
tell the two apart. `linear.sh new` stamps `driven:human` unless `--driven` (or
`LINEAR_DRIVEN`) says otherwise; pass `agent-supervised` only when an agent
filed the ticket unprompted.

Re-stamping rewrites the issue's whole label set (via `set-driven.py`) rather
than appending — `driven` is a mutually-exclusive group, so the CLI's `-l` would
fail with `labelIds not exclusive child labels`. Idempotent.

```bash
bash scripts/linear.sh driven SB-42 agent-supervised   # agent built it, human merges
bash scripts/linear.sh driven SB-42 agent-auto         # agent delivered + QE verified it
```

**When an agent should call this:** at PR-open time, on its own ticket. Tickets are filed
`driven:human` by default, so an agent that forgets to promote its work under-reports
itself — which is the safe direction to fail.

Both `cycle-report.py` and `metrics.sh` slice completed work by this label. The slice is
over *completed* issues only: an unfinished ticket has not been delivered by anyone yet.
It is orthogonal to `adhoc` — an adhoc ticket can be agent-delivered, and the two are
never merged into one number.

## Audit unassigned — `/linear audit`

Find (and optionally fix) issues with no assignee — backs convention #1:
```bash
bash scripts/linear.sh audit-unassigned         # list only
bash scripts/linear.sh audit-unassigned --fix   # assign all to silverbeer.io
```

## Raw CLI and GraphQL recipes

Drop to the raw CLI (`linear issue ...`, **singular**) but keep conventions #1–#4:
- List labels (to confirm group membership): `linear label list --team SB`
- Add a comment: `linear issue comment add SB-N --body "text"` (or `--body-file /tmp/c.md` for markdown)
- Unassigned set: `linear issue query --team SB --unassigned --all-states` (or `--assignee <user>` for someone specific; add `-j` for JSON).

For anything the CLI can't do (initiatives, cycles, team settings, metrics) use the raw GraphQL wrapper `scripts/linear-gql.sh` (see Transport above for the key; supports variables as `$2`). The CLI also has a native `linear api '<query>'` that uses its own keychain auth. Verify the whole setup with `scripts/doctor.sh`.

**Still open (SB-18):** first-class sub-issues in the CLI (currently done via `linear-gql.sh` `parentId`), and smarter area-label inference from changed files.
