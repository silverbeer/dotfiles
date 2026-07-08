#!/usr/bin/env python3
"""Audit Claude Code session transcripts: token burn, tool usage, waste patterns."""

import glob
import json
import os
import sys
from collections import Counter, defaultdict


def project_dir(repo_path: str) -> str:
    slug = repo_path.rstrip("/").replace("/", "-").replace(".", "-")
    return os.path.expanduser(f"~/.claude/projects/{slug}/")


def audit(path: str) -> None:
    files = sorted(glob.glob(path + "*.jsonl"), key=os.path.getsize, reverse=True)
    if not files:
        print(f"No transcripts under {path}")
        return

    grand = {"out": 0, "cache_read": 0, "cache_create": 0, "turns": 0}

    for f in files:
        size_mb = os.path.getsize(f) / 1e6
        tools: Counter[str] = Counter()
        skills: Counter[str] = Counter()
        agents: Counter[str] = Counter()
        big_results: list[tuple[str, int]] = []
        tool_names: dict[str, str] = {}
        n_user, n_asst, out_tok, cache_read, cache_create, compacts = 0, 0, 0, 0, 0, 0
        first_ts = last_ts = None

        with open(f) as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = rec.get("timestamp")
                if ts:
                    first_ts = first_ts or ts
                    last_ts = ts
                if rec.get("isCompactSummary") or rec.get("subtype") == "compact_boundary":
                    compacts += 1
                msg = rec.get("message") or {}
                rtype = rec.get("type")
                if rtype == "user":
                    content = msg.get("content")
                    if isinstance(content, str):
                        n_user += 1
                    elif isinstance(content, list):
                        for c in content:
                            if c.get("type") == "tool_result":
                                body = c.get("content")
                                length = len(json.dumps(body)) if body else 0
                                big_results.append((tool_names.get(c.get("tool_use_id"), "?"), length))
                            elif c.get("type") == "text":
                                n_user += 1
                elif rtype == "assistant":
                    n_asst += 1
                    u = msg.get("usage") or {}
                    out_tok += u.get("output_tokens", 0)
                    cache_read += u.get("cache_read_input_tokens", 0)
                    cache_create += u.get("cache_creation_input_tokens", 0)
                    for c in msg.get("content") or []:
                        if isinstance(c, dict) and c.get("type") == "tool_use":
                            tools[c["name"]] += 1
                            tool_names[c["id"]] = c["name"]
                            if c["name"] == "Skill":
                                skills[c["input"].get("skill", "?")] += 1
                            elif c["name"] == "Agent":
                                agents[c["input"].get("subagent_type", "general")] += 1

        grand["out"] += out_tok
        grand["cache_read"] += cache_read
        grand["cache_create"] += cache_create
        grand["turns"] += n_asst
        big_results.sort(key=lambda x: -x[1])
        agg: dict[str, int] = defaultdict(int)
        for t, length in big_results:
            agg[t] += length

        span = f"{first_ts[:10]} -> {last_ts[:10]}" if first_ts and last_ts else "?"
        days = ""
        if first_ts and last_ts and first_ts[:10] != last_ts[:10]:
            days = "  ⚠ multi-day session"
        print(f"\n=== {os.path.basename(f)[:8]}  {size_mb:.1f}MB  {span}{days}")
        print(
            f"  user_msgs={n_user}  turns={n_asst}  out={out_tok/1000:.0f}k  "
            f"cache_read={cache_read/1e6:.1f}M  cache_create={cache_create/1e6:.1f}M  compacts={compacts}"
        )
        print(f"  tools: {tools.most_common(8)}")
        if skills:
            print(f"  skills: {skills.most_common()}")
        if agents:
            print(f"  agents: {agents.most_common()}")
        oversized = [(t, round(length / 1000)) for t, length in big_results[:5] if length > 20_000]
        if oversized:
            print(f"  ⚠ oversized results (tool, KB): {oversized}")

    print("\n=== TOTALS")
    print(
        f"  sessions={len(files)}  turns={grand['turns']}  out={grand['out']/1e6:.1f}M  "
        f"cache_read={grand['cache_read']/1e6:.0f}M  cache_create={grand['cache_create']/1e6:.1f}M"
    )


if __name__ == "__main__":
    repo = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    audit(project_dir(repo))
