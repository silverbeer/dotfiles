#!/usr/bin/env bash
# metrics.sh — capacity + DORA-style delivery metrics for team SB (SB-360).
#
#   bash metrics.sh [--days N]     # default 30
#
# Delivery metrics come from the Linear API (real createdAt/startedAt/completedAt
# — not the CLI's updatedAt proxy). Deploy frequency proxies from merged PRs to
# main across the org (the CD/ArgoCD flow deploys on merge; no GitHub Deployments
# are recorded). Change-failure is a revert/hotfix proxy; MTTR needs incident
# tracking we don't have yet — both are marked honestly.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GQL="$SCRIPT_DIR/linear-gql.sh"
DAYS=30
[ "${1:-}" = "--days" ] && DAYS="${2:-30}"
WEEKS=$(awk "BEGIN{printf \"%.2f\", $DAYS/7}")
CUTOFF="$(date -u -v-"${DAYS}"d +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d "-${DAYS} days" +%Y-%m-%dT%H:%M:%SZ)"

command -v jq >/dev/null || { echo "metrics: jq required" >&2; exit 1; }

echo "════════════════════════════════════════════"
echo "  SB DELIVERY METRICS — last ${DAYS}d"
echo "════════════════════════════════════════════"

# ---- Delivery (Linear) -----------------------------------------------------
issues_json="$(bash "$GQL" "{ issues(first:250, filter:{completedAt:{gte:\"$CUTOFF\"}}){ nodes { identifier createdAt startedAt completedAt } } }" 2>/dev/null)"

echo "$issues_json" | jq -r --argjson weeks "$WEEKS" '
  def parse: sub("\\.[0-9]+Z$";"Z") | fromdateiso8601;
  def days($a;$b): (($b|parse) - ($a|parse)) / 86400;
  def median: sort | if length==0 then null elif length%2==1 then .[length/2|floor] else (.[length/2-1]+.[length/2])/2 end;
  (.data.issues.nodes // []) as $n
  | ($n | length) as $count
  | ($n | map(select(.createdAt and .completedAt) | days(.createdAt; .completedAt))) as $lead
  | ($n | map(select(.startedAt and .completedAt) | days(.startedAt; .completedAt))) as $cycle
  | "▸ Throughput",
    "    completed:   \($count)   (\((($count/$weeks)*10|round)/10) / week)",
    "▸ Lead time (created → done)",
    (if ($lead|length)>0 then "    avg \((($lead|add/($lead|length))*10|round)/10)d   median \((($lead|median)*10|round)/10)d   (n=\($lead|length))" else "    (no data)" end),
    "▸ Cycle time (started → done)",
    (if ($cycle|length)>0 then "    avg \((($cycle|add/($cycle|length))*24*10|round)/10)h   median \((($cycle|median)*24*10|round)/10)h   (n=\($cycle|length))" else "    (no started timestamps)" end)
'

# ---- Deploy frequency + change-failure (GitHub, org-wide) ------------------
echo "▸ Deploy frequency (merged PRs → main, org-wide proxy)"
prs="$(gh search prs --owner=silverbeer --merged --merged-at=">${CUTOFF%%T*}" --json title,repository --limit 300 2>/dev/null)"
if [ -n "$prs" ] && [ "$prs" != "null" ]; then
  echo "$prs" | jq -r --argjson weeks "$WEEKS" '
    length as $m
    | (map(select(.title|test("(?i)revert|hotfix|rollback"))) | length) as $cf
    | "    merges:      \($m)   (\((($m/$weeks)*10|round)/10) / week)",
      "▸ Change-failure proxy (revert/hotfix PRs)",
      "    \($cf)/\($m)   \(if $m>0 then (($cf/$m)*1000|round)/10 else 0 end)%"'
else
  echo "    (gh search returned nothing — check gh auth)"
fi

echo "▸ MTTR"
echo "    not tracked yet — needs incident labels/records (future)"

# ---- DORA bands (lead time + deploy freq) ----------------------------------
echo "────────────────────────────────────────────"
echo "  DORA bands: lead-time <1d elite · <1wk high · <1mo medium"
echo "              deploys: multi/day elite · weekly high · monthly medium"
echo "  Caveats: deploy freq = merge proxy (no GH Deployments); change-failure"
echo "  = title heuristic; MTTR unimplemented. Estimates backfill = SB-363."
