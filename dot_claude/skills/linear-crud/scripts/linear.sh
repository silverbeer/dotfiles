#!/usr/bin/env bash
# linear-crud skill helper — thin wrapper around the `linear` CLI that bakes in
# the silverbeer workspace conventions so Claude never has to remember them.
#
# Conventions enforced:
#   - team     = SB (override: LINEAR_TEAM)
#   - assignee = silverbeer.io, ALWAYS (override: LINEAR_ASSIGNEE)
#   - repo label (group) detected from the current git repo
#   - type label (group) required on create
#   - driven label (group) = autonomy of delivery; defaults to driven:human
#   - epic = Linear project; every epic MUST map to a repo label (repos.json)
#
# Subcommands:
#   repo-label                  print the repo label inferred from cwd
#   epics                       list Linear projects (epics) + their mapped repo
#   board [--repo L] [--epic E] render a delivery board (ready queue + critical path)
#   stats [--days N]            momentum dashboard (totals, velocity, age, sparkline)
#   new   --title T (--body B | --body-file F) --type TYPE [--repo R] [--epic E]
#         [--driven human|agent-supervised|agent-auto] [--label L ...] [--estimate N]
#   branch SB-N                 checkout silverbeer/sb-n-<slug> (triggers auto → In Progress)
#   list  [--all] [--repo R] [--epic E]   my issues (open by default), TSV
#   view  SB-N [--full]         brief JSON for one issue (--full: CLI text incl. description)
#   pack  SB-N                  one JSON: brief issue + branchName + repoLabel + git + pr
#   move  SB-N "State"          change workflow state
#   driven SB-N VALUE           re-stamp autonomy (human|agent-supervised|agent-auto)
#   link  SB-N [PR#]            add "Fixes SB-N" to a PR body
#   audit-unassigned [--fix]    SB issues not assigned to the default assignee
#
# CLI surface (linear 2.0.0, verified 2026-06-20 — this build differs sharply
# from the 2026-05-30 one; do NOT trust older notes):
#   - subcommand is `issue` (SINGULAR); `issues` (plural) just dumps usage.
#   - `issue mine` / `issue list` / `issue l` are ALIASES of one command: MY
#     issues. Needs --team. Filters: --state (repeatable, TYPE values
#     triage|backlog|unstarted|started|completed|canceled), --all-states,
#     --project, --project-label, -l/--label (repeatable), --sort
#     manual|priority (REQUIRED — no default), --limit (0=unlimited). No --json.
#   - `issue query` is the all-assignees query engine: --assignee, -U/--unassigned,
#     --all-teams, --search, --project, -l/--label, --all-states (default), and
#     -j/--json. Used by audit-unassigned.
#   - `issue create`: -t/--title, -a/--assignee <username>, -l/--label
#     (REPEATABLE, not a comma list), --description / --description-file,
#     --team, --project, --no-interactive, -s/--state, -p/--priority,
#     --estimate N.
#   - `issue update <id>`: -a/--assignee, -s/--state, -t/--title, -l/--label,
#     --project, --description/-file, --estimate N. (state accepts a name or
#     type; <id> is the identifier, e.g. SB-123.)
#   - `issue view <id>` shows one issue; `issue comment add <id>` comments.
#   - `project list --team SB [-j]` lists projects; `project view <slug>` for one.
export NO_COLOR=1
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LINEAR_TEAM="${LINEAR_TEAM:-SB}"
LINEAR_ASSIGNEE="${LINEAR_ASSIGNEE:-silverbeer.io}"

die() { echo "error: $*" >&2; exit 1; }
warn() { echo "warn: $*" >&2; }

# Path to a sibling helper (linear-gql.sh, board.py, set-driven.py). Deployed it
# is NAME; in the chezmoi source tree it may carry the executable_ prefix.
# Resolve so both work.
sibling() {
  local name="$1" f
  for f in "$SCRIPT_DIR/$name" "$SCRIPT_DIR/executable_$name"; do
    [[ -f "$f" ]] && { echo "$f"; return 0; }
  done
  die "$name not found next to $0 (run doctor.sh)"
}

# Brief JSON for one issue (~300 B, no description) via a single GraphQL call.
# This is what view / branch / pack all read from (SB-922).
# KEY is validated here, once; VERB names the caller in the usage message.
# branchName is deliberately NOT projected: Linear's own (silverbeerio/…)
# conflicts with ours — pack carries the branch_name_for() value instead.
issue_brief() {
  local key="${1:-}" verb="${2:-view}"
  [[ "$key" =~ ^[A-Za-z]+-[0-9]+$ ]] || die "usage: $verb SB-123"
  command -v jq >/dev/null || die "jq not found"
  local raw
  raw="$(bash "$(sibling linear-gql.sh)" \
    "{ issue(id:\"$key\"){ identifier title state{name} estimate priority labels{nodes{name}} url } }")" \
    || die "$key: GraphQL call failed (is linear-gql.sh set up? run doctor.sh)"
  jq -e . >/dev/null 2>&1 <<<"$raw" || die "$key: non-JSON response from Linear"
  if [[ "$(jq -r '.errors // empty | length' <<<"$raw")" -gt 0 ]]; then
    die "$key: $(jq -r '.errors[0].message' <<<"$raw")"
  fi
  [[ "$(jq -r '.data.issue // empty | length' <<<"$raw")" -gt 0 ]] || die "$key not found"
  jq -c '.data.issue | {identifier,title,state:.state.name,estimate,priority,labels:[.labels.nodes[].name],url}' <<<"$raw"
}

# The repo <-> label map lives in ../repos.json (one entry per repo label:
# dirGlobs, epicGlobs, ghRepo). board.py and stats read the same file. Deployed
# it sits at ~/.claude/skills/linear-crud/repos.json, next to SKILL.md.
repos_json() {
  local f="$SCRIPT_DIR/../repos.json"
  [[ -f "$f" ]] || die "repos.json not found at $f — it ships with the skill; run 'chezmoi apply' (doctor.sh checks it)"
  echo "$f"
}

# repos.json parsed once per process into two flat rule lists, one line per
# glob as label<TAB>glob, in file order. Loaded lazily on first match so the
# hot paths (epics loops over every project) pay for jq exactly once.
REPO_DIR_RULES=()
REPO_EPIC_RULES=()
load_repo_rules() {
  [[ ${#REPO_DIR_RULES[@]} -gt 0 ]] && return 0
  command -v jq >/dev/null || die "jq not found"
  local f kind label glob
  f="$(repos_json)"
  while IFS=$'\t' read -r kind label glob; do
    case "$kind" in
      d) REPO_DIR_RULES+=("$label"$'\t'"$glob") ;;
      e) REPO_EPIC_RULES+=("$label"$'\t'"$glob") ;;
    esac
  done < <(jq -r '.[] | .label as $l
                  | ((.dirGlobs // [])[] | "d\t\($l)\t\(.)"),
                    ((.epicGlobs // [])[] | "e\t\($l)\t\(.)")' "$f")
  [[ ${#REPO_DIR_RULES[@]} -gt 0 ]] || die "repos.json: no dirGlobs rules parsed from $f (invalid JSON?)"
}

# First label whose FIELD (dirGlobs|epicGlobs) has a glob matching NAME.
# Entries are tried in file order and globs in list order, exactly like the
# old `case` arms — order matters (bet-collect must beat bet-*). Exit 1 on miss.
match_repo_glob() {
  local field="$1" name="$2" rule label glob
  load_repo_rules
  local -a rules=()
  case "$field" in
    dirGlobs)  rules=(${REPO_DIR_RULES[@]+"${REPO_DIR_RULES[@]}"}) ;;
    epicGlobs) rules=(${REPO_EPIC_RULES[@]+"${REPO_EPIC_RULES[@]}"}) ;;
    *) die "match_repo_glob: unknown field '$field'" ;;
  esac
  for rule in ${rules[@]+"${rules[@]}"}; do
    label="${rule%%$'\t'*}"; glob="${rule#*$'\t'}"
    # shellcheck disable=SC2053 # $glob is meant to be a pattern
    [[ "$name" == $glob ]] && { echo "$label"; return 0; }
  done
  return 1
}

# Map the current git repo (or cwd) to its Linear `repo` label.
repo_label() {
  local root name
  root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
  name="$(basename "$root")"
  match_repo_glob dirGlobs "$name"
}

# Epic (Linear project) -> repo label. EVERY epic MUST resolve to a repo here —
# that is the required "project-level label". To add an epic, give it a repo:
# add a lowercase glob to that label's epicGlobs in repos.json.
# (Inferred 2026-06-20 from each project's existing issue labels.)
epic_repo() {
  local key; key="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  match_repo_glob epicGlobs "$key"
}

# Render a delivery board (ready queue + critical path) for one or more repos.
# Read-only. Writes an HTML file and prints its path.
cmd_board() {
  local args=() repo=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --repo) args+=(--repo "$2"); repo="$2"; shift 2 ;;
      *) args+=("$1"); shift ;;
    esac
  done
  if [ -z "$repo" ]; then
    repo="$(repo_label || die "board: could not detect repo from $(pwd) — pass --repo")"
    args+=(--repo "$repo")
  fi
  python3 "$(sibling board.py)" "${args[@]}"
}

cmd_repo_label() { repo_label || die "unknown repo '$(basename "$(git rev-parse --show-toplevel 2>/dev/null || pwd)")' — pass --repo explicitly"; }

# List Linear projects (epics) with the repo each maps to. Names shown here are
# the exact strings to pass to --epic / --project. Driven off JSON (jq) so wide
# names don't get mangled by the text table's column padding.
cmd_epics() {
  command -v jq >/dev/null || die "epics: jq not found"
  # Parse repos.json here, in the parent shell, so a missing/invalid file dies
  # with its real message instead of every row showing [??] — and so the
  # per-row lookups below inherit the parsed rules rather than re-running jq.
  load_repo_rules
  # name<TAB>status<TAB>[repo]; [??] = not mapped in repos.json — add it before use.
  local out
  out="$(linear project list --team "$LINEAR_TEAM" -j)" || die "epics: linear CLI failed"
  jq -r '.nodes[] | "\(.name)\t\(.status.name // "?")"' <<<"$out" \
    | while IFS=$'\t' read -r name status; do
        local repo; repo="$(epic_repo "$name" 2>/dev/null || echo '??')"
        printf '%s\t%s\t[%s]\n' "$name" "$status" "$repo"
      done
}

cmd_new() {
  local title="" body="" body_file="" type="" repo="" epic="" driven="" estimate="" extra_labels=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --title)     title="$2"; shift 2 ;;
      --body)      body="$2"; shift 2 ;;
      --body-file) body_file="$2"; shift 2 ;;
      --type)      type="$2"; shift 2 ;;
      --repo)      repo="$2"; shift 2 ;;
      --epic)      epic="$2"; shift 2 ;;
      --driven)    driven="$2"; shift 2 ;;
      --label)     extra_labels+=("$2"); shift 2 ;;
      --estimate)  estimate="$2"; shift 2 ;;
      *) die "new: unknown arg '$1'" ;;
    esac
  done
  [[ -n "$title" ]] || die "new: --title required"
  [[ -n "$type"  ]] || die "new: --type required (bug|feature|chore|docs|infra|security)"
  [[ -z "$estimate" || "$estimate" =~ ^[0-9]+$ ]] || die "new: --estimate must be an integer (got '$estimate')"

  # Autonomy attribution (SB-507). Defaults to human because that is what an
  # interactive session is; an agent must opt in explicitly, and the only way to
  # do so is to pass the flag. A human cannot emit it by accident, which is the
  # whole point — every commit already carries Co-Authored-By: Claude, so
  # authorship cannot distinguish autonomy. The label can only mean what the
  # caller asserts.
  driven="${driven:-${LINEAR_DRIVEN:-human}}"
  driven="${driven#driven:}"
  case "$driven" in
    human|agent-supervised|agent-auto) ;;
    *) die "new: --driven must be human|agent-supervised|agent-auto (got '$driven')" ;;
  esac

  # The epic supplies the default repo label (the project-level label). An
  # explicit --repo wins: some epics span repos — Podtelemetry is a POD service
  # with STK-side integration tickets in the same epic.
  if [[ -n "$epic" ]]; then
    local epic_r
    epic_r="$(epic_repo "$epic" || die "new: epic '$epic' is not mapped to a repo — add an epicGlobs entry in repos.json (run 'epics' to see the list)")"
    if [[ -n "$repo" && "$repo" != "$epic_r" ]]; then
      warn "new: --repo $repo overrides epic '$epic' default ($epic_r)"
    fi
    [[ -z "$repo" ]] && repo="$epic_r"
  fi
  [[ -z "$repo" ]] && repo="$(repo_label || die "new: could not detect repo — pass --repo or --epic")"

  # Resolve the description body (file wins over inline).
  if [[ -n "$body_file" ]]; then
    [[ -f "$body_file" ]] || die "new: body file not found: $body_file"
  fi

  # Labels are REPEATED -l flags on this CLI (repo + type + any area labels).
  local args=(issue create --title "$title" --team "$LINEAR_TEAM"
              --assignee "$LINEAR_ASSIGNEE" --no-interactive
              -l "$repo" -l "$type" -l "driven:$driven")
  local l; for l in "${extra_labels[@]:-}"; do [[ -n "$l" ]] && args+=(-l "$l"); done
  [[ -n "$epic" ]] && args+=(--project "$epic")
  if [[ -n "$body_file" ]]; then
    args+=(--description-file "$body_file")
  elif [[ -n "$body" ]]; then
    args+=(--description "$body")
  fi

  [[ -n "$estimate" ]] && args+=(--estimate "$estimate")

  linear "${args[@]}" || die "new: linear CLI failed"
}

cmd_list() {
  local all=0 repo="" epic=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --all)  all=1; shift ;;
      --repo) repo="$2"; shift 2 ;;
      --epic) epic="$2"; shift 2 ;;
      *) die "list: unknown arg '$1'" ;;
    esac
  done

  # An epic implies its repo label; otherwise fall back to cwd detection.
  if [[ -n "$epic" ]]; then
    repo="$(epic_repo "$epic" || die "list: epic '$epic' is not mapped to a repo (run 'epics')")"
  fi
  [[ -z "$repo" && -z "$epic" ]] && repo="$(repo_label || true)"

  # `issue query -j` (the only listing verb with JSON) scoped to my issues.
  # Open = unstarted + started. Output is TSV: id, state, Pn, e<est>, title —
  # full titles, no ANSI, no column padding (SB-922).
  command -v jq >/dev/null || die "list: jq not found"
  local args=(issue query --team "$LINEAR_TEAM" --assignee "$LINEAR_ASSIGNEE" --limit 0 -j)
  if [[ $all -eq 1 ]]; then
    args+=(--all-states)
  else
    args+=(-s unstarted -s started)
  fi
  [[ -n "$repo" ]] && args+=(-l "$repo")
  [[ -n "$epic" ]] && args+=(--project "$epic")
  local out
  out="$(linear "${args[@]}")" || die "list: linear CLI failed"
  jq -r '.nodes[] | "\(.identifier)\t\(.state.name)\tP\(.priority)\te\(.estimate // "-")\t\(.title)"' <<<"$out"
}

# Print a single issue. The first thing /work and /ticket do, and the reason this
# verb exists here rather than callers reaching past the wrapper for
# `linear issue view` (SB-905).
#
# Default is the brief JSON (issue_brief, ~300 B) — enough to restate, branch
# and plan. `--full` is the old passthrough to `linear issue view`, which prints
# the description as markdown and exits 1 with a clear message on a bad key;
# reach for it only when the AC text itself is needed (SB-922).
cmd_view() {
  local id="" full=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --full) full=1; shift ;;
      *) id="$1"; shift ;;
    esac
  done
  [[ -n "$id" ]] || die "view: usage: view SB-N [--full]"
  if [[ $full -eq 1 ]]; then
    linear issue view "$id" --no-download
  else
    issue_brief "$id" view
  fi
}

cmd_move() {
  local id="${1:-}" state="${2:-}"
  [[ -n "$id" && -n "$state" ]] || die "move: usage: move SB-N \"State\""
  linear issue update "$id" --state "$state"
}

# Re-stamp the autonomy label on an existing issue (SB-507).
#
# `driven` is a mutually-exclusive group, so the CLI's `-l` would reject a second
# child ("labelIds not exclusive child labels"). The whole label set has to be
# rewritten with the old driven:* value dropped — which needs GraphQL, not the CLI.
#
# This is the hook an agent calls when it opens a PR: it starts life as
# driven:human (filed by a human) and promotes itself to agent-supervised.
cmd_driven() {
  local id="${1:-}" value="${2:-}"
  [[ -n "$id" && -n "$value" ]] || die "driven: usage: driven SB-N human|agent-supervised|agent-auto"
  value="${value#driven:}"
  case "$value" in
    human|agent-supervised|agent-auto) ;;
    *) die "driven: value must be human|agent-supervised|agent-auto (got '$value')" ;;
  esac
  local num="${id##*-}"
  LINEAR_DRIVEN_VALUE="$value" LINEAR_ISSUE_NUM="$num" LINEAR_TEAM="$LINEAR_TEAM" \
    python3 "$(sibling set-driven.py)"
}

# Add "Fixes SB-N" to a PR body (idempotent). PR# optional — defaults to the
# open PR for the current branch.
cmd_link() {
  local id="${1:-}" pr="${2:-}"
  [[ -n "$id" ]] || die "link: usage: link SB-N [PR#]"
  command -v gh >/dev/null || die "link: gh CLI not found"
  # ${sel[@]+"${sel[@]}"}: bash 3.2 (macOS /bin/bash) treats an empty array
  # as unset under set -u, so the plain expansion aborts when PR# is omitted.
  local sel=(); [[ -n "$pr" ]] && sel=("$pr")
  local body
  body="$(gh pr view ${sel[@]+"${sel[@]}"} --json body --jq .body 2>/dev/null)" || die "link: no PR found (specify PR#)"
  if grep -qiE "(fixes|closes|ref) ${id}\b" <<<"$body"; then
    echo "link: PR already references ${id}"; return 0
  fi
  # An empty body is fine: the result is just the Fixes line.
  [[ -n "$body" ]] && body="${body}"$'\n\n'
  gh pr edit ${sel[@]+"${sel[@]}"} --body "${body}Fixes ${id}"
  echo "link: added 'Fixes ${id}' to PR body"
}

# Unassigned audit. `issue mine/list` only ever return MY issues (they are
# aliases), so a diff is useless — use `issue query -U`, which filters to
# unassigned issues across all assignees directly.
cmd_audit_unassigned() {
  local fix=0; [[ "${1:-}" == "--fix" ]] && fix=1
  command -v jq >/dev/null || die "audit: jq not found"
  local out ids
  out="$(linear issue query --team "$LINEAR_TEAM" --unassigned --all-states --limit 0 -j)" \
    || die "audit: linear CLI failed"
  ids="$(jq -r '.nodes[].identifier' <<<"$out" | sort -u)" || die "audit: non-JSON response from linear CLI"
  if [[ -z "$ids" ]]; then echo "audit: no unassigned ${LINEAR_TEAM} issues 🎉"; return 0; fi
  echo "Issues not assigned to ${LINEAR_ASSIGNEE}:"
  # shellcheck disable=SC2001 # indents every line of a multi-line list; ${//} can't do that
  echo "$ids" | sed 's/^/  /'
  if [[ $fix -eq 1 ]]; then
    while read -r id; do
      [[ -z "$id" ]] && continue
      linear issue update "$id" --assignee "$LINEAR_ASSIGNEE" >/dev/null && echo "  ✓ assigned $id"
    done <<<"$ids"
  else
    echo "(re-run with --fix to assign them all to ${LINEAR_ASSIGNEE})"
  fi
}

# Momentum dashboard. Pulls every SB issue once (issue query -j) and crunches it
# with jq. No completedAt field exists on this CLI, so "ship time" / "closed in
# window" use updatedAt as the close-time proxy (flagged approx in the output).
cmd_stats() {
  command -v jq >/dev/null || die "stats: jq not found"
  local days=7
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --days) days="$2"; shift 2 ;;
      *) die "stats: unknown arg '$1'" ;;
    esac
  done

  local json
  json="$(linear issue query --team "$LINEAR_TEAM" --all-states --limit 0 -j)" \
    || die "stats: query failed"
  [[ -n "$json" ]] || die "stats: empty result"

  local repos; repos="$(jq -c 'map(.label)' "$(repos_json)")"
  jq -r --argjson win "$days" --arg team "$LINEAR_TEAM" --argjson repos "$repos" '
    def age($iso): ($iso | sub("\\.[0-9]+Z$";"Z") | fromdateiso8601) as $t | (now - $t)/86400;
    def r1: (.*10|round)/10;
    def bar($n; $max): ["▁","▂","▃","▄","▅","▆","▇","█"] as $b
      | if $max<=0 then " " else $b[(($n/$max)*7|floor)] end;
    .nodes as $all
    | ($all|length) as $total
    | [ $all[] | select(.state.type=="completed") ] as $done
    | [ $all[] | select(.state.type=="canceled") ]  as $cancel
    | [ $all[] | select(.state.type|IN("backlog","unstarted","started","triage")) ] as $open
    | [ $open[] | select(.state.type=="started") ] as $active
    | [ $open[] | select(.state.type=="backlog") ] as $backlog
    | [ $open[] | select(.state.type|IN("unstarted","triage")) ] as $todo

    | [ $all[]  | select(age(.createdAt) <= $win) ]  as $created_win
    | [ $done[] | select(age(.updatedAt) <= $win) ]  as $closed_win
    | (($created_win|length) - ($closed_win|length)) as $net

    | (if ($open|length)>0 then ([ $open[] | age(.createdAt) ]|add/(. as $x|$open|length)) else 0 end) as $avg_open
    | (if ($done|length)>0 then ([ $done[] | (age(.createdAt) - age(.updatedAt)) ]|add/(. as $x|$done|length)) else 0 end) as $avg_ship
    | ($open | max_by(age(.createdAt))) as $oldest

    # per-repo and per-epic counts (desc, top 6)
    | ( [ $all[] | (.labels.nodes[].name | select(. as $n|$repos|index($n))) ] | group_by(.)
        | map({k:.[0], n:length}) | sort_by(-.n) ) as $by_repo
    | ( [ $all[] | .project.name | select(.) ] | group_by(.)
        | map({k:.[0], n:length}) | sort_by(-.n)[0:6] ) as $by_epic

    # created-per-day sparkline, last 14 days (bucket 0 = today)
    | ( [ range(0;14) as $d
          | { d:$d, n: ([ $all[] | select((age(.createdAt)|floor)==$d) ] | length) } ]
        | reverse ) as $spark
    | ( $spark | map(.n) | max ) as $smax

    | "🏔  SILVERBEER MOMENTUM — \($team) team",
      "────────────────────────────────────────────",
      "  ✅ Shipped     \($done|length)   (\((($done|length)/$total*100)|floor)% of all tickets)",
      "  🔨 Open        \($open|length)   (\($active|length) in progress · \($todo|length) todo · \($backlog|length) backlog)",
      "  🗑  Canceled    \($cancel|length)",
      "  📦 Total ever  \($total)",
      "",
      "  Last \($win)d:   +\($created_win|length) created   −\($closed_win|length) shipped   → net \(if $net>=0 then "+" else "" end)\($net)",
      "  Avg age (open):  \($avg_open|r1)d        Oldest open: \(if $oldest then "\($oldest.identifier) (\(age($oldest.createdAt)|floor)d)" else "—" end)",
      "  Avg ship time:   \($avg_ship|r1)d  (approx, by last-update)",
      "",
      "  By repo:  \($by_repo | map("\(.k) \(.n)") | join("  ·  "))",
      "  By epic:  \($by_epic | map("\(.k|.[0:18]) \(.n)") | join("  ·  "))",
      "",
      "  Created/day (14d, →today):  \($spark | map(bar(.n;$smax)) | join(""))   peak \($smax)",
      ""
  ' <<<"$json"

  # Motivational closer (computed in jq above would be awkward; do it here).
  local shipped created_win closed_win
  shipped="$(jq '[.nodes[]|select(.state.type=="completed")]|length' <<<"$json")"
  created_win="$(jq --argjson w "$days" '[.nodes[]|select(((.createdAt|sub("\\.[0-9]+Z$";"Z")|fromdateiso8601)) as $t|(now-$t)/86400 <= $w)]|length' <<<"$json")"
  closed_win="$(jq --argjson w "$days" '[.nodes[]|select(.state.type=="completed")|select(((.updatedAt|sub("\\.[0-9]+Z$";"Z")|fromdateiso8601)) as $t|(now-$t)/86400 <= $w)]|length' <<<"$json")"
  local msg
  if   (( closed_win >= 5 )); then msg="🔥 ${closed_win} shipped in ${days} days — you are ON FIRE. Keep crushing."
  elif (( closed_win >= 1 )); then msg="💪 ${closed_win} shipped in ${days} days. Momentum is real — push the next one."
  elif (( created_win >= 1 )); then msg="🌱 ${created_win} new ideas logged. Now go close one. 🎯"
  else msg="🧗 Quiet ${days} days. ${shipped} lifetime ships say you've got this — pick one and go."; fi
  echo "  ${msg}"
  echo
}

# Branch name for an issue: silverbeer/sb-<n>-<title-slug>. Shared by branch
# and pack so the two can never disagree.
branch_name_for() {
  local key="$1" title="$2"
  local slug
  slug="$(printf '%s' "$title" | tr '[:upper:]' '[:lower:]' \
    | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//' | cut -c1-40 | sed -E 's/-+$//')"
  echo "silverbeer/$(printf '%s' "$key" | tr '[:upper:]' '[:lower:]')-$slug"
}

# Create + checkout a branch named to trigger Linear's git automation
# (branch → In Progress). Needs linear-gql.sh.
cmd_branch() {
  local key="${1:-}"
  local title
  title="$(issue_brief "$key" branch | jq -r .title)"
  local branch
  branch="$(branch_name_for "$key" "$title")"
  git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "branch: not inside a git repo"
  git checkout -b "$branch"
  echo "→ $branch"
  echo "  Linear will auto-move $key to In Progress when this branch is pushed."
}

# Everything /work needs to start, in one ~400 B JSON: the brief issue, the
# branch name it would check out, the repo label, git state and the most
# relevant PR for that branch, any state (null if none). Replaces view +
# repo-label + branch + git status.
cmd_pack() {
  local key="${1:-}"
  local brief bn repo cur dirty pr
  brief="$(issue_brief "$key" pack)"
  bn="$(branch_name_for "$key" "$(jq -r .title <<<"$brief")")"
  repo="$(repo_label 2>/dev/null || echo null)"
  # One git call for both: `# branch.head` gives the branch, any non-# line
  # means dirty. -uno = untracked files are ignored (not counted as dirty).
  local st
  st="$(git status --porcelain=v2 --branch -uno 2>/dev/null || true)"
  cur="$(sed -n 's/^# branch\.head //p' <<<"$st")"
  [[ "$cur" == "(detached)" ]] && cur=""
  if grep -qv '^#' <<<"$st"; then dirty=true; else dirty=false; fi
  # PR lookup: prefer the checked-out branch when it is this ticket's
  # (sb-<n>- anywhere, case-insensitive) — a hand-named branch still resolves.
  # Any state; an OPEN PR wins if several exist for the head.
  local head="$bn" num="${key##*-}"
  shopt -s nocasematch
  [[ "$cur" == *"sb-${num}-"* ]] && head="$cur"
  shopt -u nocasematch
  pr="$(gh pr list --head "$head" --state all --json number,url,state \
        --jq 'sort_by(.state=="OPEN"|not) | .[0] // null' 2>/dev/null || echo null)"
  [[ -n "$pr" ]] || pr=null
  jq -nc --argjson issue "$brief" --arg bn "$bn" --arg repo "$repo" \
         --arg cur "$cur" --argjson dirty "$dirty" --argjson pr "$pr" \
    '{issue:$issue, branchName:$bn, repoLabel:(if $repo=="null" then null else $repo end),
      git:{branch:$cur, dirty:$dirty}, pr:$pr}'
}

sub="${1:-}"; shift || true
case "$sub" in
  repo-label)        cmd_repo_label "$@" ;;
  epics)             cmd_epics "$@" ;;
  board)             cmd_board "$@" ;;
  stats)             cmd_stats "$@" ;;
  new)               cmd_new "$@" ;;
  branch)            cmd_branch "$@" ;;
  pack)              cmd_pack "$@" ;;
  list)              cmd_list "$@" ;;
  view)              cmd_view "$@" ;;
  move)              cmd_move "$@" ;;
  driven)            cmd_driven "$@" ;;
  link)              cmd_link "$@" ;;
  audit-unassigned)  cmd_audit_unassigned "$@" ;;
  ""|-h|--help) sed -n '2,/^# CLI surface/{/^# CLI surface/!p;}' "$0" ;;
  *) die "unknown subcommand '$sub' (try --help)" ;;
esac
