---
name: gatekeeper
description: Open a dual-channel (Linear + Telegram) human-approval gate for a headless agent and wait for a decision — `gate.py open` posts a proposal and DMs Approve/Reject/Note buttons, the always-on `listen.py` records Telegram answers within seconds, and `gate.py poll` reads Linear plus what the listener recorded and hands decided gates to the runner. Use when a headless run (plan, cycle-plan, triage, PR, merge, or a stuck ticket) needs a yes/no from a human before it may proceed.
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

# read Linear + the decisions listen.py recorded, and claim what was decided
# since the last call; without --once, repeat every --timeout seconds until
# nothing is awaiting. Never reads Telegram.
python3 scripts/gate.py poll [--once] [--timeout 25]
# -> {"resolved": [{"gate_id", "ticket", "status", "source"}, ...], "awaiting": N}

# the one Telegram reader — runs for ever (k3s Deployment gatekeeper-listener)
python3 scripts/listen.py [--repo CLONE] [--ref main] [--timeout 25]

python3 scripts/gate.py status [gate_id]              # JSON of one gate, or all
python3 scripts/gate.py resolve GATE_ID approve|reject --source cli [--note N]
```

`--kind` is one of `plan | cycle-plan | triage | pr | merge | blocked`. `open`
exits 0 whether or not anyone has answered yet — a headless run always calls
`open` then exits; `poll` is a separate step (the runner's next tick, which
resumes the run with its `--session-id`).

## Gate protocol (dual channel)

1. **open** writes `$GATEKEEPER_STATE/gates/<gate_id>.json`, posts the full
   proposal as a Linear comment prefixed with the marker
   `<!-- sb-agent:{kind}:{run_id}:{session_id} -->`, sets the issue's
   `gate:awaiting-approval` label (full rewrite — `gate:*` is an exclusive
   group, same as `driven:*`), and DMs a summary (head of the body, capped,
   plus the issue link) with inline buttons `✅ Approve · ❌ Reject · 💬 Note`.
   `blocked` gates get no buttons — nothing to approve, only to see.
2. **Telegram** is read by `listen.py`, continuously (SB-951).
   `from.id` must be in the allowlist or the update is silently dropped (but
   always `answerCallbackQuery`ed so the client stops spinning). A button tap
   is **recorded, answered, then applied**: `pending_decision` is saved on the
   gate, the toast goes back within seconds, and only then is Linear updated.
   If Linear is down, the decision stays pending and is retried by the next
   listener loop and by `poll`. `💬 Note` claims the sender's *next* text
   message as a note and leaves the gate awaiting. Free text `approve` /
   `reject: reason` also decides (the most recently opened gate). Any other
   allowlisted private text goes to the [inbox](#inbox-seam-for-the-po-chat).
3. **Linear** is read by `poll`: comments on the ticket *after* the marker
   comment, by the assignee. First line `approve`, `approve: note`, or
   `reject: reason` decides. Any other comment is discussion — forwarded to
   Telegram once, gate stays open.
4. **First decision wins.** It is checked under the gate's lock on a fresh
   read, and a recorded-but-unapplied `pending_decision` counts as first. It is
   recorded with its `source` (`telegram` / `linear` / `cli`), the label swaps
   to `gate:approved` or `gate:rejected`, and the channel that did *not*
   decide gets an echo — so Linear and Telegram can never show conflicting
   answers.
5. **Timeout.** A gate open longer than `GATE_TIMEOUT_HOURS` (default 72)
   flips to `gate:needs-human`, with a "🛑 stuck" Telegram message naming the
   `resolve` command to unstick it. It is not auto-approved and not
   auto-rejected.
6. **Handoff.** Every decision (and a timeout) sets `handoff: "pending"` on
   the gate. `poll` returns each such gate once in `resolved` and marks it
   `claimed` with `claimed_at`, before the runner acts. That's at most once,
   same as before. A gate with no `handoff` field predates SB-951 and is
   never returned.

Full spec: `docs/agentic-delivery.md` → "Gate protocol (dual channel)".

## One Telegram reader

**Only `listen.py` calls `getUpdates`.** Telegram allows one reader per bot
token: a second one gets HTTP 409, and so does the first. `tg.py` raises that as
`TelegramConflict`, and the listener exits **3** on it. Nothing else exits 3, so
`doctor.sh` can report "second reader" rather than "crashed". Anything else that wants Telegram *input* reads what the
listener records: gate state, or the inbox. **Sending from other processes is
fine** (`sendMessage`, `editMessageText`: the runner's summary, `gate.py open`):
only reading conflicts. `test_listen.py` fails if a second `get_updates(` call
site appears anywhere under `dot_claude/skills/`.

The listener acks an update only after its effect is on disk. A Telegram error
or a failed write leaves it unacked, and Telegram redelivers it for 24h. Every
handler is idempotent on `update_id` (`pending_decision.update_id`,
`decided_update_id`, `tg_update_ids` on the gate; the filename in the inbox).
State lives under `$GATEKEEPER_STATE`: `telegram-offset`, `listener/heartbeat`
(the liveness probe reads it), and `gates/<id>.lock` (per-gate `flock`, held
around every write to an existing gate, because the listener and `poll` both
write).

## Inbox seam for the PO chat

Free text that no gate claims is kept for the PO chat (SB-1089) in a Maildir under
`$GATEKEEPER_STATE/inbox/telegram/`, one file per update, named
`<update_id>.json`:

| dir | meaning |
| --- | --- |
| `tmp/` | being written; never read |
| `new/` | recorded, unclaimed |
| `cur/` | claimed by a consumer, in progress |
| `done/` | handled |

Protocol (`scripts/inbox.py`). Every step is a rename, so it's atomic:

- `record(message, update_id)` writes `tmp/` then renames into `new/`. It is a
  no-op if that update is already in `new/`, `cur/` or `done/`. A stale `tmp/`
  file doesn't count, because it means the write never finished.
- `claim_next()` renames the oldest `new/` file (by `update_id`) into `cur/` and
  returns its path, or `None`. Two consumers can't claim one message: the
  loser's rename finds nothing.
- `complete(path)` moves `cur/` to `done/`. `requeue(path)` moves `cur/` back
  to `new/` for a retry.

Record, schema `gatekeeper.inbox/1` (adding a field keeps `/1`, renaming or
removing one bumps it):

```json
{"schema": "gatekeeper.inbox/1", "update_id": 123456789, "message_id": 42,
 "chat_id": 111, "from_id": 111, "date": 1789000000,
 "text": "what's at risk this cycle?", "received_at": "2026-09-16T12:00:00+00:00",
 "reply_to_message_id": null}
```

`reply_to_message_id` (added in SB-1089, still `/1`) is the `message_id` of the
message this one replies to in Telegram, or `null`. Records written before it
existed don't have the field, so read it with `.get`.

Only allowlisted senders in a private chat, with non-empty text, are recorded.
A reply goes out with `tg.send_text`, which is safe from any process. The
listener records messages whether or not anything consumes them.

**The one consumer is the PO chat**: `po-agent/scripts/po_chat.py consume`,
the `po-chat` Deployment (`k3s/cycle-runner/po-chat.yaml`). It claims one
message at a time, and it answers with `tg.send_text` plus
`send_chat_action` ("typing…") while it works. While a gate is awaiting, a
free-text `approve` or `reject` still decides the newest one, and a pending
💬 Note still claims the next text. Neither reaches the chat. Everything else
does, including `yes` and `no` to a PO proposal.

## Config (env)

| Var | Default | Notes |
| --- | --- | --- |
| `GATEKEEPER_TG_TOKEN` | — | required; the gatekeeper's own bot token (never trd's — a second poller on one token is a 409) |
| `GATEKEEPER_TG_CHAT_ID` | — | required; the human's private chat id |
| `GATEKEEPER_ALLOWED_USER_IDS` | `GATEKEEPER_TG_CHAT_ID` | comma/space-separated numeric Telegram user ids |
| `GATEKEEPER_STATE` | `~/.local/state/cycle-runner` | `gates/` (JSON + locks), `inbox/telegram/`, `telegram-offset`, `listener/heartbeat` |
| `LINEAR_ASSIGNEE_ID` | resolved via `viewer{id}`, cached in state | set only if the API key's own viewer id is ever wrong for this |
| `GATE_TIMEOUT_HOURS` | `72` | consulted by `poll` |

`listen.py` runs as the `gatekeeper-listener` Deployment
(`k3s/cycle-runner/listener.yaml`). It needs the two Telegram vars plus
`LINEAR_API_KEY`, and runs from a dotfiles clone. When `main` moves, it
fetches, resets that clone and re-execs itself in place: same pid, same
environment, no container restart. A failed fetch leaves the current code
running and is retried at the next five-minute check.

Exit codes: `0` stopped (SIGTERM), `3` Telegram 409 (a second `getUpdates`
reader). Anything else is a crash.

A recorded decision that won't apply to Linear is retried with backoff
(`pending_decision.attempts` / `last_error` / `next_retry_at`, 30s doubling
to 15min) by both the listener and `poll`. After 24h the gate becomes
`needs-human` with `reason: decision_not_applied`, and the verb and note are
kept in `unapplied_decision`. The gate is handed to the runner, and the human
gets one DM.

`source scripts/env.sh` first to pull the two Telegram vars from
`op://agents/cycle-runner-telegram/{token,chat-id}` when they are not already
set and `op` is available (a silent no-op otherwise — never prints a value).
Needs the sibling `linear-crud` skill for `linear_api.gql` and its GraphQL
credentials.
