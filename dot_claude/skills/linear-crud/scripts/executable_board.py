#!/usr/bin/env python3
"""board.py — render a delivery board for any silverbeer repo from Linear.

Computes the two things Linear itself has no view for:
  * ready queue  — tickets with no *open* blocker, i.e. startable right now
  * critical path — longest unbroken chain in the `blocks` graph

Everything is derived from Linear. There is no per-project config.

Usage:
  board.py --repo MT [--repo MTA] [--epic "Name"] [--out PATH] [--team SB]

Reads through linear-gql.sh (same auth as the `linear` CLI). Read-only.
"""
import argparse
import functools
import html
import pathlib
import sys
from collections import defaultdict

HERE = pathlib.Path(__file__).resolve().parent
CSS  = HERE / "board.css"

sys.path.insert(0, str(HERE))
from linear_api import gql, warn_if_capped  # noqa: E402

# repo label -> GitHub repo name. Inverse of repo_label() in linear.sh.
GH = {
    "MT": "missing-table", "MTA": "missing-table-android",
    "BOOT": "missingtable-platform-bootstrap", "MS": "match-scraper",
    "MSA": "match-scraper-agent", "QB": "qualityplaybook.dev",
    "STK": "myrunstreak.run", "JT": "janitor", "DOT": "dotfiles",
    "TODO": "todo", "TRD": "trd", "POD": "podtelemetry.com",
    "BET": "bet", "BETC": "bet-collect",
}
TYPES = {"feature", "bug", "chore", "docs", "infra", "security"}
STATE_ORDER = {"In Progress": 0, "In Review": 1, "Todo": 2, "Backlog": 3, "Done": 4, "Canceled": 5}

Q_ISSUES = """query($labels:[String!], $team:String!) {
  issues(first:250, filter:{ team:{key:{eq:$team}}, labels:{name:{in:$labels}} }) {
    nodes { identifier title estimate url
      state { name type }
      project { id name sortOrder }
      cycle { number }
      labels { nodes { name } }
      relations { nodes { type relatedIssue { identifier } } }
      inverseRelations { nodes { type issue { identifier state { name } } } } } } }"""

Q_CYCLE = """query($team:String!) {
  team(id:$team) { activeCycle { number startsAt endsAt } } }"""


def longest_path(graph):
    """Longest chain in a DAG, returned as a list of node ids."""
    @functools.lru_cache(None)
    def depth(u):
        return 1 + max((depth(v) for v in graph.get(u, ())), default=0)
    if not graph:
        return []
    start = max(graph, key=depth)
    path, u = [start], start
    while graph.get(u):
        u = max(graph[u], key=depth)
        path.append(u)
    return path


def find_cycle(graph):
    WHITE, GREY = 0, 1
    colour, found = defaultdict(int), []

    def walk(u, stack):
        colour[u] = GREY
        stack.append(u)
        for v in graph.get(u, ()):
            if colour[v] == GREY:
                found.append(stack[stack.index(v):] + [v])
            elif colour[v] == WHITE:
                walk(v, stack)
        stack.pop()
        colour[u] = 2

    for n in list(graph):
        if colour[n] == WHITE:
            walk(n, [])
    return found[0] if found else None


E = html.escape


def render(issues, cycle, labels, title, team):
    for i in issues:
        i["_blockers"] = [r["issue"]["identifier"] for r in i["inverseRelations"]["nodes"]
                          if r["type"] == "blocks" and r["issue"]["state"]["name"] != "Done"]
        i["_blocks"] = [r["relatedIssue"]["identifier"] for r in i["relations"]["nodes"]
                        if r["type"] == "blocks"]
        i["_repo"] = next((lb["name"] for lb in i["labels"]["nodes"] if lb["name"] in GH), "")
        i["_type"] = next((lb["name"] for lb in i["labels"]["nodes"] if lb["name"] in TYPES), "")

    graph = {i["identifier"]: i["_blocks"] for i in issues if i["_blocks"]}
    cyc = find_cycle(graph)
    nrel = sum(len(v) for v in graph.values())
    by_id = {i["identifier"]: i for i in issues}
    def est(v):
        return sum(i["estimate"] or 0 for i in v)

    open_ = [i for i in issues if i["state"]["type"] not in ("completed", "canceled")]
    ready = [i for i in open_ if not i["_blockers"]]
    blocked = [i for i in open_ if i["_blockers"]]
    done = [i for i in issues if i["state"]["type"] == "completed"]
    chain = longest_path(graph)

    # ---- epics, ordered by Linear's own project sortOrder
    groups = defaultdict(list)
    for i in issues:
        groups[i["project"]["name"] if i["project"] else "No epic"].append(i)
    order = sorted(groups, key=lambda n: (
        next((i["project"]["sortOrder"] for i in groups[n] if i["project"]), 1e9), n))

    def link(k):
        return f'<a href="{by_id[k]["url"]}">{k}</a>' if k in by_id else f"<span>{k}</span>"

    cards = []
    for name in order:
        v = groups[name]
        counts = defaultdict(int)
        for i in v:
            counts[i["state"]["name"]] += 1
        segs = "".join(
            f'<span class="seg s-{k.lower().replace(" ", "")}" style="flex:{counts[k]}"></span>'
            for k in ("Done", "In Progress", "In Review", "Todo", "Backlog") if counts[k])
        nb = len([i for i in v if i in blocked])
        nr = len([i for i in v if i in ready])
        repos = sorted({i["_repo"] for i in v if i["_repo"]})
        badges = "".join(f'<span class="repo">{r}</span>' for r in repos)
        tail = (f"<b>{nb}</b> blocked" if nb else "nothing blocked") + (f" · {nr} ready" if nr else "")
        cards.append(
            f'<article class="epic{" is-gated" if nb else ""}">'
            f'<header class="epic-hd">{badges}</header>'
            f'<h3>{E(name)}</h3>'
            f'<div class="figs"><span class="pts">{est(v)}</span><span class="unit">pts</span>'
            f'<span class="cnt">{len(v)} tickets</span></div>'
            f'<div class="bar">{segs}</div>'
            f'<p class="gate{"" if nb else " gate-ok"}">{tail}</p></article>')

    rq = []
    for i in sorted(ready, key=lambda x: (-len(x["_blocks"]), x["identifier"])):
        n = len(i["_blocks"])
        unblocks = f"unblocks {n} ticket" + ("s" if n != 1 else "") if n else "unblocks nothing downstream"
        rq.append(
            f'<div class="rq"><div class="rq-top"><a href="{i["url"]}">{i["identifier"]}</a>'
            f'<span class="ep">{E(i["project"]["name"] if i["project"] else "—")}</span>'
            f'<span class="e">{i["estimate"] or 0}pts</span></div>'
            f'<p>{E(i["title"])}</p>'
            f'<p class="ep">{unblocks}</p></div>')

    ledger = []
    for name in order:
        v = sorted(groups[name], key=lambda i: (STATE_ORDER.get(i["state"]["name"], 9), i["identifier"]))
        rows = []
        for i in v:
            bl = i["_blockers"]
            cell = ('<span class="bk">' + "".join(link(x) for x in bl) + "</span>") if bl \
                else '<span class="free">—</span>'
            st = i["state"]["name"]
            rows.append(
                f'<tr class="{"is-blocked" if bl else ""}">'
                f'<td class="id"><a href="{i["url"]}">{i["identifier"]}</a></td>'
                f'<td class="ti">{E(i["title"])}</td>'
                f'<td class="ty"><span class="tag">{i["_type"]}</span></td>'
                f'<td class="es">{i["estimate"] if i["estimate"] is not None else "—"}</td>'
                f'<td class="st"><span class="pill p-{st.lower().replace(" ", "")}">{st}</span></td>'
                f'<td class="bb">{cell}</td></tr>')
        ledger.append(
            f'<section class="grp"><h3 class="grp-hd">{E(name)}'
            f'<span class="grp-meta">{len(v)} · {est(v)}pts</span></h3>'
            f'<div class="tw"><table><thead><tr><th>ID</th><th>Ticket</th><th>Type</th>'
            f'<th>Pts</th><th>State</th><th>Blocked by</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div></section>')

    # ---- notes: only rendered when there is something true to say
    notes = []
    if cyc:
        notes.append(
            '<div class="note alert"><h2>The blocking graph has a cycle</h2>'
            '<p>Every ticket in this loop is permanently unstartable — each waits on the next. '
            'Break it by deleting one relation.</p>'
            f'<div class="chain">{" <span>&rarr;</span> ".join(link(k) for k in cyc)}</div></div>')
    if cycle:
        inc = [i for i in issues if i["cycle"] and i["cycle"]["number"] == cycle["number"]]
        if inc:
            rows = "".join(
                f'<li><span class="k">{i["identifier"]}</span> {E(i["title"])} — '
                f'{i["estimate"] or 0}pts · {i["state"]["name"]}</li>'
                for i in sorted(inc, key=lambda x: x["identifier"]))
            notes.append(
                f'<div class="note"><h2>Cycle {cycle["number"]} — '
                f'{cycle["startsAt"][:10]} to {cycle["endsAt"][:10]}</h2>'
                f'<p>{len(inc)} of these tickets are in the active cycle, {est(inc)} points.</p>'
                f'<ul class="mini">{rows}</ul></div>')
    if len(chain) > 2:
        top = sorted((i for i in open_ if i["_blocks"]), key=lambda x: -len(x["_blocks"]))[:3]
        blurb = " ".join(
            f'<code>{t["identifier"]}</code> blocks {len(t["_blocks"])}.' for t in top)
        notes.append(
            f'<div class="note alert"><h2>The critical path is {len(chain)} deep</h2>'
            '<p>Longest unbroken chain of blocking relations. Nothing shortens it except '
            'starting at the left.</p>'
            f'<div class="chain">{" <span>&rarr;</span> ".join(link(k) for k in chain)}</div>'
            f'<p>{blurb}</p></div>')
    elif not nrel:
        notes.append(
            '<div class="note"><h2>No dependencies modelled</h2>'
            '<p>Nothing in this set has a <code>blocks</code> relation, so every open ticket counts '
            'as ready and there is no critical path to compute. Add relations in Linear to get '
            'a real ready queue.</p></div>')

    links = "".join(
        f'<a href="https://github.com/silverbeer/{GH[lb]}"><span class="lk">repo</span> {GH[lb]}</a>'
        for lb in labels if lb in GH)
    team_link = (f'<a href="https://linear.app/silverbeer/team/{E(team)}/active">'
                 f'<span class="lk">Linear</span> {E(team)} board</a>')
    ready_html = "".join(rq) or '<p class="hint">Nothing is startable — every open ticket has an open blocker.</p>'

    return f'''<title>{E(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500&display=swap">
<style>{CSS.read_text()}</style>
<div class="wrap">
 <header class="top">
  <span class="eyebrow">Linear · team {E(team)} · {E(" + ".join(labels))}</span>
  <h1>{E(title)}</h1>
  <p class="sub">{len(issues)} tickets, {est(issues)} points, {len(order)} epics, {nrel} blocking relations.</p>
  <div class="links">{team_link}{links}</div>
 </header>
 <div class="stats">
  <div class="stat"><b>{len(issues)}</b><span>tickets</span></div>
  <div class="stat"><b>{est(issues)}</b><span>points</span></div>
  <div class="stat"><b>{nrel}</b><span>dependencies</span></div>
  <div class="stat"><b>{len(ready)}</b><span>ready · {est(ready)}pts</span></div>
  <div class="stat"><b>{len(blocked)}</b><span>blocked · {est(blocked)}pts</span></div>
  <div class="stat"><b>{len(done)}</b><span>done</span></div>
 </div>
 {"".join(notes)}
 <section class="sec"><div class="sec-hd"><h2>Ready queue</h2>
  <span class="hint">no open blocker · ordered by how much each unblocks</span></div>
  <div class="ready">{ready_html}</div></section>
 <section class="sec"><div class="sec-hd"><h2>Epics</h2>
  <span class="hint">ordered as in Linear · red border means something in it is blocked</span></div>
  <div class="grid">{"".join(cards)}</div></section>
 <section class="sec"><div class="sec-hd"><h2>Ledger</h2>
  <span class="hint">every ticket · blocked-by is live from Linear relations</span></div>
  {"".join(ledger)}</section>
 <footer>Generated from the Linear API by <code>linear.sh board</code>. Read-only.</footer>
</div>'''


def main():
    p = argparse.ArgumentParser(description="Render a delivery board from Linear.")
    p.add_argument("--repo", action="append", default=[], metavar="LABEL",
                   help="repo label (repeatable), e.g. --repo MT --repo MTA")
    p.add_argument("--epic", help="restrict to one Linear project (epic)")
    p.add_argument("--team", default="SB")
    p.add_argument("--title", help="board title (default: derived from the repo labels)")
    p.add_argument("--out", help="output path (default: ./<repo>-board.html)")
    a = p.parse_args()
    if not a.repo:
        sys.exit("board: no repo label — pass --repo (linear.sh board detects it from cwd)")

    issues = gql(Q_ISSUES, {"labels": a.repo, "team": a.team})["issues"]["nodes"]
    warn_if_capped(issues, 250, "board issues")
    if a.epic:
        issues = [i for i in issues if i["project"] and i["project"]["name"] == a.epic]
    if not issues:
        sys.exit(f"board: no issues found for {'+'.join(a.repo)}"
                 + (f" in epic {a.epic!r}" if a.epic else ""))
    cycle = gql(Q_CYCLE, {"team": a.team})["team"]["activeCycle"]

    title = a.title or (a.epic if a.epic else f"{'+'.join(a.repo)} Delivery Board")
    out = pathlib.Path(a.out or f"{a.repo[0].lower()}-board.html")
    out.write_text(render(issues, cycle, a.repo, title, a.team))
    print(out.resolve())


if __name__ == "__main__":
    main()
