---
name: session-audit
description: Audit Claude Code session transcripts for the current project — token burn, cache-read waste, tool usage, oversized reads, skill/agent adoption. Use when the user asks "audit my sessions", "how am I using tokens", "session stats", or wants efficiency tips.
allowed-tools: Bash, Read
---

Analyzes the JSONL transcripts under `~/.claude/projects/<project-slug>/` and reports
where tokens actually go, so the user can fix habits instead of guessing.

## How to run

1. Run the analyzer (lives next to this file):

   ```bash
   python3 ~/.claude/skills/session-audit/analyze.py
   ```

   It auto-derives the project slug from the current working directory. To audit a
   different project, pass its path: `python3 .../analyze.py /path/to/repo`.

2. Interpret the output for the user. The numbers that matter, in order:

   - **cache_read (M tokens)** — the silent killer. Every turn re-reads the whole
     accumulated context. A session with hundreds of millions of cache-read tokens is
     a session that lived too long. Rule of thumb: one session per ticket/task;
     start fresh when switching topics. Memory + CLAUDE.md carry the context across.
   - **days spanned** — a session reopened across many days pays a full cache
     re-write (cache_create) after every >5-minute gap.
   - **top single tool results** — any Read over ~20KB should have used
     offset/limit or been delegated to an Explore/investigator subagent (subagent
     reads don't live in the main context forever).
   - **agent/skill counts vs. turn counts** — thousands of turns with single-digit
     subagent use means exploration is happening in the main thread, where every
     result is re-paid on every subsequent turn.
   - **compacts** — zero compacts on a giant session means context grew unbounded.

3. Close with 2–4 concrete, prioritized recommendations tied to the user's actual
   numbers — not generic advice. If `rtk` is installed, include `rtk gain` output
   as the "what's working" baseline.
