---
name: gatekeeper
description: Open a dual-channel (Linear + Telegram) human-approval gate for a headless agent and wait for a decision — `gate.py open` posts a proposal and DMs Approve/Reject/Note buttons, `gate.py poll` drains both channels until one of them answers. Use when a headless run (plan, cycle-plan, triage, PR, merge, or a stuck ticket) needs a yes/no from a human before it may proceed.
allowed-tools: Bash, Read
---

Human-in-the-loop for the headless delivery loop (SB-508). A gate is never
optional and never times a human out silently — 72h unanswered becomes
`gate:needs-human`, not an auto-decision.

## Commands

```bash
# open a gate: full proposal -> Linear comment, summary + buttons -> Telegram DM
python3 scripts/gate.py open --kind plan --ticket SB-N --body proposal.md \
  --session-id "$SESSION_ID" --run-id "$RUN_ID" [--link URL] [--dry-run]
# -> {"status": "awaiting", "gate_id": "a1b2c3d4"}

# drain Telegram + read new Linear comments until every gate resolves (or once)
python3 scripts/gate.py poll [--once] [--timeout 25]
# -> {"resolved": [{"gate_id", "ticket", "status", "source"}, ...], "awaiting": N}

python3 scripts/gate.py status [gate_id]              # JSON of one gate, or all
python3 scripts/gate.py resolve GATE_ID approve|reject --source cli [--note N]
```

`--kind` is one of `plan | cycle-plan | triage | pr | merge | blocked`. `open`
exits 0 whether or not anyone has answered yet — a headless run always calls
`open` then exits; `poll` is a separate step (a cron / launchd tick, or the
next run resuming with `--session-id`).

## Gate protocol (dual channel)

1. **open** writes `$GATEKEEPER_STATE/gates/<gate_id>.json`, posts the full
   proposal as a Linear comment prefixed with the marker
   `<!-- sb-agent:{kind}:{run_id}:{session_id} -->`, sets the issue's
   `gate:awaiting-approval` label (full rewrite — `gate:*` is an exclusive
   group, same as `driven:*`), and DMs a summary (head of the body, capped,
   plus the issue link) with inline buttons `✅ Approve · ❌ Reject · 💬 Note`.
   `blocked` gates get no buttons — nothing to approve, only to see.
2. **poll** checks both channels every call:
   - **Telegram**: `getUpdates`, `from.id` must be in the allowlist or the
     update is silently dropped (but always `answerCallbackQuery`ed so the
     client stops spinning). A button tap decides immediately; `💬 Note`
     claims the sender's *next* text message as a note and leaves the gate
     awaiting. Free text `approve` / `reject: reason` also decides.
   - **Linear**: comments on the ticket *after* the marker comment, by the
     assignee. First line `approve`, `approve: note`, or `reject: reason`
     decides. Any other comment is discussion — forwarded to Telegram once,
     gate stays open.
3. **First decision wins.** It is recorded with its `source` (`telegram` /
   `linear` / `cli`), the label swaps to `gate:approved` or `gate:rejected`,
   and the channel that did *not* decide gets an echo — so Linear and
   Telegram can never show conflicting answers.
4. **Timeout.** A gate open longer than `GATE_TIMEOUT_HOURS` (default 72)
   flips to `gate:needs-human`, with a "🛑 stuck" Telegram message naming the
   `resolve` command to unstick it. It is not auto-approved and not
   auto-rejected.

Full spec: `docs/agentic-delivery.md` → "Gate protocol (dual channel)".

## Config (env)

| Var | Default | Notes |
| --- | --- | --- |
| `GATEKEEPER_TG_TOKEN` | — | required; the gatekeeper's own bot token (never trd's — a second poller on one token is a 409) |
| `GATEKEEPER_TG_CHAT_ID` | — | required; the human's private chat id |
| `GATEKEEPER_ALLOWED_USER_IDS` | `GATEKEEPER_TG_CHAT_ID` | comma/space-separated numeric Telegram user ids |
| `GATEKEEPER_STATE` | `~/.local/state/cycle-runner` | gate JSON + Telegram offset live under `<state>/gates/` |
| `LINEAR_ASSIGNEE_ID` | resolved via `viewer{id}`, cached in state | set only if the API key's own viewer id is ever wrong for this |
| `GATE_TIMEOUT_HOURS` | `72` | consulted by `poll` |

`source scripts/env.sh` first to pull the two Telegram vars from
`op://agents/cycle-runner-telegram/{token,chat-id}` when they are not already
set and `op` is available (a silent no-op otherwise — never prints a value).
Needs the sibling `linear-crud` skill for `linear_api.gql` and its GraphQL
credentials.
