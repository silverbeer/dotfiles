# Agent-driven weekly cycle — home delivery loop goes agentic

> Living doc. Epic: **Agentic Delivery System** (Linear). Plan ticket: SB-921. Approved 2026-08-29. Tick tickets as they close.
> Reused existing tickets: SB-508 → T1 gatekeeper, SB-624 → T9 triage. SB-506 (role agents) is effectively delivered by PR #39 — close at next groom.

## Context

All code is already written by Claude, but the *process* is human-driven: every ticket needs a person in the terminal running `/work`. Goal: an orchestrator that runs the whole weekly Linear cycle (triage → plan → build → PR → report) on the always-on Mac mini, with the human pulled in only at key decisions — via Telegram notifications with quick approve/reject, and Linear comments (phone app) as an equally valid decision channel. A Telegram bot already exists (trd). Quality enforced by blocking GHA checks. Budget: Claude Code subscription only. Plus one ADK + A2A spike to inform work agents (alert triage, deploy-to-lower-envs on GCP/Vertex).

### What already exists (after `git pull`, PRs #38–41)
- `dot_claude/commands/work.md` — 9-phase interactive orchestrator, 4 terminal gates. Agents `dev-engineer` (code only), `qe-engineer` (pytest). `/code-review`.
- `linear-crud`: `linear.sh`, `executable_board.py`, `cycle-report.py`, `executable_metrics.sh`, `executable_linear-gql.sh`, `set-driven.py`, `executable_doctor.sh`. `driven:*` labels intact at HEAD.
- `.github/workflows/ci.yml` — checks in `.github/scripts/check-*.sh`, tested by `.github/tests/harness.sh` (negative tests, `QE_NO_SKIPS`).
- trd Telegram bot (`~/gitrepos/trd/src/trd/notify/bot.py`): stdlib `urllib`, long-poll `getUpdates`, private-chat + user-id allowlist, offset file, atomic queue dir, `FakeTransport` tests. **One `getUpdates` reader per token** → orchestrator needs its own bot token. No inline keyboards yet.
- No `docs/` folder — prior session's work is the commits above.

### Linear integration audit (measured 2026-08-29)
| Finding | Where |
|---|---|
| `view` 4,154 B/call, 92% description; `-j --no-comments --no-download` + jq → 245 B | `linear.sh:247` |
| `list` 42% ANSI; `export NO_COLOR=1` → 2,272→1,320 B; titles truncated | `linear.sh:232`, `strip_ansi` unused except `:299` |
| `/work` loads ~7.6k tokens of instructions; branch rule ×6, `Fixes SB-N` ×5, repo list ×4 | CLAUDE.md, SKILL.md (15.5 KB), work.md, ticket.md |
| Two transports (Homebrew `schpet/tap` CLI, untrusted tap) + GraphQL; two auth stores; credential-scrape fallback | `linear-gql.sh:34-41` |
| Repo map drifted: `stats` missing BET/BETC | `linear.sh:337` vs `:63-79`, `board.py:29-36` |
| `audit-unassigned` prints 🎉 on API failure | `linear.sh:298-300` |
| `link` can clobber PR body on empty `gh` body | `linear.sh:284-288` |
| `set-driven.py` fetches all 250 labels per call (3 round trips); `board.py` refetches everything; no cache/retry/429/pagination | `set-driven.py:66`, `board.py:284` |
| `branch` does GQL for title; `view -j` already returns `title` + `branchName` | `linear.sh:407` |
| Branch-prefix claim ("must be `silverbeer/`") probably false — Linear's `branchName` is `silverbeerio/sb-903-…`; `sb-N` token matches | SKILL.md:232, work.md:41, ticket.md:21 |
| Zero functional tests on 12 scripts (lint only) | `.github/tests/` |

### Verified platform facts
- `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN` (1 yr) works for unattended `claude -p` and `claude-code-action`. Non-`--bare` headless loads `~/.claude/commands` + `agents`. Agent SDK = API key only → excluded.
- Cloud routines (`/schedule`) subscription-included; possible later for triage/plan runs. Not primary.
- ADK local, no GCP project; $0 via LiteLLM→Ollama (`ollama_chat/`); HITL via `require_confirmation` / `LongRunningFunctionTool`; `adk eval`; `to_a2a()`/`RemoteA2aAgent`; pins a2a-sdk 0.3 vs spec 1.x.

### Decisions
| Topic | Decision |
|---|---|
| Target | Full cycle orchestrator |
| HITL channel | **Dual**: Telegram = notification + quick tap (own bot token); Linear comment (phone app) = equally valid decision + richer discussion. Poller watches both; first decision wins, echoed to the other channel. Which one gets used more is itself a signal — measure it |
| Runtime | k3s CronJob on the mini (rancher-desktop/Lima), `claude -p` — launchd until SB-976 |
| Auto scope | `driven:agent-auto` only if estimate ≤2 **and** `adhoc`/`chore`; else agent-supervised with plan gate |
| Merge | CI green required; human taps "merge" in Telegram initially |
| Quality gate | `silverbeer/ci-workflows` reusable workflows: lint+typecheck, tests+coverage floor, gitleaks, AI review (blocking on confirmed correctness bugs) |
| Pilot repos | dotfiles + **missing-table + match-scraper-agent** (assumed — confirm) |
| Cycle planning | Agent proposes at capacity = velocity × (1 − adhoc share); approve via Telegram before Linear writes |
| ADK/A2A | One spike ticket, zero-cost |

## Architecture

1. **Linear layer** (hardened `linear-crud`) — GraphQL-only, JSON-first, one `repos.json`, `pack` command, tests.
2. **Gate layer** — `dot_claude/skills/gatekeeper/`: `tg.py` (send with inline keyboard, poll callback, stdlib urllib, patterned on trd `TelegramTransport`), `gate.py` (post Linear audit comment + label, send Telegram, wait or park). State: `~/.local/state/cycle-runner/gates/<gate_id>.json`.
3. **Runner** — `dot_claude/skills/cycle-runner/scripts/run.sh`: lock, token, pick, `claude -p "/work-headless SB-N" --output-format json --json-schema … --max-turns N --permission-mode acceptEdits --allowedTools …`, resume on approval.
4. **Headless commands** — `work-headless.md`, `cycle-plan.md`, `triage.md`.
5. **Quality gate** — `silverbeer/ci-workflows`.

### Gate protocol (dual channel)
- `gate.py open --kind plan|cycle-plan|triage|pr|merge --ticket SB-N --body file`: writes gate state; posts **full** proposal as Linear comment `<!-- sb-agent:{kind}:{run_id}:{session_id} -->` + `gate:awaiting-approval`; sends Telegram DM (≤4096 chars, summary + deep link to the Linear issue / PR) with inline keyboard `✅ Approve · ❌ Reject · 💬 Note`. Exits 0 with `{status:awaiting, gate_id}`.
- `gate.py poll` checks both channels each cycle:
  - **Telegram**: `getUpdates` (`allowed_updates: [callback_query, message]`), `from.id` ∈ allowlist, `callback_data = gate_id:verb`, `answerCallbackQuery`. "Note" → next text from allowed user attaches. Free-text `approve` / `reject: reason` also accepted.
  - **Linear**: comments on the issue after the marker, by the assignee. First line `approve`, `approve: note`, `reject: reason`. Any other comment = discussion → forwarded to Telegram, gate stays open (agent may reply in-thread via `--resume` if the comment is a question — cap at 2 exchanges before `needs-human`).
  - First decision wins; echoed to the other channel ("approved via Linear"). Decision source recorded in gate state → weekly report shows Telegram-vs-Linear usage.
- Runner: approved → `gate:approved`, `claude -p --resume <session_id>` with note; rejected → `gate:rejected`, ack in both channels, branch untouched. Timeout / failure / ambiguity → `gate:needs-human` + Telegram "🛑 stuck"; ticket skipped thereafter.
- Auto path (`agent-auto`): no plan gate; plan still posted (Linear + Telegram, no buttons). PR gate → Telegram `✅ Merge` button; runner runs `gh pr merge --squash` only on that tap.
- Bot token + allowlisted user id from `op://agents/cycle-runner-telegram/*`. Separate bot from trd.

### Guard rails
- Lock `~/.local/state/cycle-runner/lock` (mkdir-atomic, pid check). One ticket per run, ≤1 per 30 min.
- Session id in gate state **and** Linear comment (survives both Macs).
- Wrapper asserts branch contains `sb-<n>` before push; `--allowedTools` excludes `git push --force*`, `git merge*`, `gh pr merge*` (merge happens in runner, not in Claude).
- Logs gitleaks-scanned before any comment/message. Telegram body never contains diff hunks with env values.
- `--append-system-prompt`: headless; never ask questions; every decision → `gate.py open` then exit.

## Phases and tickets (one epic; Fibonacci)

### Phase 0 — Linear hardening (token + risk) — prerequisite for headless
- **T0a · SB-922 (3)** Token diet, no behaviour change: `export NO_COLOR=1`; `view` default brief via `-j --no-comments --no-download` + jq (`--full` keeps body); `list`/`epics` via `issue query -j`; `branch` reads `branchName`/`title` from same JSON (drop GQL call); new `linear.sh pack SB-N` → ~400 B JSON `{issue, branchName, repoLabel, git:{branch,dirty}, pr}` replacing view+repo-label+branch lookup+git status.
- **T0b · SB-923 (3)** Risk fixes: `audit-unassigned` distinguishes API failure from empty; `link` refuses empty body (or uses GraphQL `attachmentLinkURL`); `set -e` + drop blanket `2>/dev/null` in `metrics.sh`/`linear.sh`; retry+429 backoff in `linear-gql.sh`; drop credential-scrape fallback (`linear-gql.sh:34-41`), require `gql-key`; `hasNextPage` warning at `first:` caps; `new` prints UUID / `--estimate` inline; label-id cache `~/.cache/linear-crud/labels.json` (1 h TTL) for `set-driven.py`.
- **T0c · SB-924 (2)** Single `repos.json` (`{label, dirGlobs, ghRepo, epicGlobs}`) consumed by `repo_label()`, `epic_repo()`, `stats`, `board.py`. Fixes BET/BETC drift.
- **T0d · SB-925 (2)** Tests via `harness.sh`: `repo_label`/`epic_repo` table, stats jq fixture, `longest_path`/`find_cycle`, `pack` output schema, `apply.py` idempotence. Wire `check-linear-crud.sh` into `ci.yml`.
- **T0e · SB-926 (1)** Docs diet: split `SKILL.md` → hot (~7 KB) + `REFERENCE.md`; state branch rule + `Fixes SB-N` once in SKILL.md, others point. **Verify branch-prefix claim** against Linear settings (test: push `feat/sb-N-x`, see if In Progress fires) and correct 4 copies. Decide on single GraphQL path (drop `schpet/tap` CLI) — do it if T0a shows CLI is the only remaining dependency.

**Verify:** byte counts before/after on `view`, `list`, `pack`; `/work` on a real ticket with fewer tool calls; CI green with new tests.

### Phase 1 — Gatekeeper (Telegram HITL)
- **T1 · SB-508 (3)** `dot_claude/skills/gatekeeper/`: `SKILL.md`, `scripts/tg.py` (send/keyboard/getUpdates/answerCallbackQuery, offset file, allowlist, 409 raised by name — port from trd `bot.py`), `scripts/gate.py` (open/poll/resolve, Linear comment + `gate:*` label via `linear-gql.sh`). `.chezmoiignore` exception `!.claude/skills/gatekeeper`. Create Telegram bot + `gate:*` labels + `op://agents/cycle-runner-telegram` (human steps).
- **T2 · SB-927 (2)** Tests: `FakeTransport` unit tests (pytest, same style as trd `tests/test_bot.py`): callback match, stranger drop, note attach, timeout → needs-human, no secret in message body; fake GraphQL: Linear `approve`/`reject: x`/discussion comment parsing, comment-before-marker ignored, both-channels-decide → first wins, source recorded. Bash harness test for `gate.py open --dry-run`.

**Verify:** `gate.py open --kind plan --ticket SB-N` → phone shows buttons + Linear comment; tap ✅ → `approved`, label swapped, Linear echo; comment `approve` in Linear app → same result, Telegram echo; tap ❌ → rejected; stranger tap ignored.

### Phase 2 — Headless `/work` + runner
- **T3 · SB-928 (2)** `commands/work-headless.md`: phases 1–8 of `/work`; each gate = `gate.py open` + exit with JSON; phase 9 → `gate.py open --kind merge`. `--json-schema` contract `{status, ticket, branch, pr_url, session_id}`. Uses `linear.sh pack`.
- **T4 · SB-929 (3)** `cycle-runner` skill: `run.sh` (lock, `CLAUDE_CODE_OAUTH_TOKEN` from `op://agents/...`, mode select: pending gates → resume, else pick), `pick.py` (ready queue from `board.py` + policy driven×estimate×label; refuses `driven:human`), merge-on-approval step, summary Telegram message per run. Policy tests + `check-cycle-runner-policy.sh`.
- **T5 · SB-930 (2)** launchd `Library/LaunchAgents/io.silverbeer.cycle-runner.plist.tmpl` (hostname-templated, mini only), `StartInterval` 1800; `doctor.sh` gains: token valid (`claude -p ok --max-turns 1`), `gh auth`, Telegram `getMe`, stale lock, **`chezmoi status` clean before re-add** (prevents the #37-reverts-#35 class).

**Verify:** `run.sh --dry-run`; real agent-supervised ticket → plan on phone → approve → PR → merge button → merged, ticket Done; kill mid-run → stale lock detected, resumes via session id; `driven:human`-only queue exits clean.

### Phase 3 — Quality gate repo
- **T6 · SB-931 (3)** `silverbeer/ci-workflows`: `python-quality.yml` (uv, ruff check+format, mypy/ty, pytest `--cov` → artifact), `coverage-floor.yml` (baseline from main artifact, fail on decrease, missing baseline = warn), `gitleaks.yml`, `shell-quality.yml`, `ai-review.yml` (`claude-code-action` + `claude_code_oauth_token`; prompt emits `REVIEW: PASS|FAIL`, step fails on FAIL). SHA-pinned, `check-pins.sh`, self-test workflow on fixtures.
- **T7 · SB-932 (2)** Wire pilots (missing-table, match-scraper-agent: all five; dotfiles: shell + ai-review). Required checks via `gh api …/branches/main/protection`. Secret from `agents` vault.

**Verify:** coverage-drop PR blocked; injected-bug PR `REVIEW: FAIL`; clean PR green.

### Phase 4 — Cycle planning, triage, reporting
- **T8 · SB-933 (3)** `commands/cycle-plan.md` + `plan.py`: capacity = last-3 velocity × (1 − adhoc share) (`cycle-report.py`); order by `board.py` critical path + groom scores; proposal → Telegram gate; approve → `linear.sh move` + cycle assignment. No writes before approval.
- **T9 · SB-624 (2)** `commands/triage.md`: issues in Triage / missing type/estimate/driven → one batched proposal → Telegram gate. Monday calendar trigger.
- **T10 · SB-934 (1)** `report.py`: weekly summary (attempted/PRs/gates/durations + DORA from `metrics.sh`) → Telegram + cycle issue comment.

**Verify:** dry-run vs backlog; totals ≤ capacity; reject leaves Linear untouched.

### Phase 5 — ADK + A2A spike
- **T11 · SB-935 (3)** Repo `adk-spike`, no GCP. `LiteLlm("ollama_chat/<coder>")`, Gemini free tier behind flag. Build `triage_agent` (FunctionTool over `linear-gql.sh`, write tool `require_confirmation=True`), expose via `to_a2a()`, consume via `RemoteA2aAgent`, `adk eval` on 5 fixtures. Deliverable: comparison doc vs Claude Code path — HITL ergonomics (confirmation tool vs Telegram gate), testability, cost, auth, A2A interop (0.3 pin), fit for work targets (alert triage, deploy-to-lower-envs on Vertex Agent Engine), which home piece ports first.

## Files
Create: `dot_claude/skills/gatekeeper/{SKILL.md,scripts/tg.py,gate.py,tests/}`, `dot_claude/skills/cycle-runner/{SKILL.md,scripts/run.sh,pick.py,plan.py,report.py}`, `dot_claude/skills/linear-crud/{REFERENCE.md,repos.json}`, `dot_claude/commands/{work-headless.md,cycle-plan.md,triage.md}`, `Library/LaunchAgents/io.silverbeer.cycle-runner.plist.tmpl`, `.github/scripts/{check-linear-crud.sh,check-cycle-runner-policy.sh}`, `.github/tests/{test_linear_crud.sh,test_cycle_runner.sh}`, repos `silverbeer/ci-workflows`, `adk-spike`.
Modify: `linear.sh`, `executable_linear-gql.sh`, `executable_board.py`, `set-driven.py`, `executable_metrics.sh`, `executable_doctor.sh`, `SKILL.md`, `.chezmoiignore`, `ci.yml`, `CLAUDE.md`, `work.md`, `ticket.md`, pilot repos' workflows.

## Risks
- Subscription rate limits / token expiry → doctor check, `needs-human` on auth failure, ≤1 ticket/30 min.
- Headless picks up interactive hooks (RTK, caveman, statusline) → test on mini; headless system prompt.
- Second Telegram poller on trd token → 409; mitigated by dedicated bot token.
- Phone approval of a plan you didn't read → Telegram body must carry AC + files + risks in ≤ 1 screen; `💬 Note` for pushback.
- AI-review false positives → required on Python pilots one cycle before dotfiles.
- Label-group exclusivity → full-label rewrite (as `set-driven.py`).

## Status log
- 2026-08-29 — plan approved; SB-921–935 filed, SB-508/624 repurposed; this doc committed (SB-921).
- 2026-08-30 — Phase 0 (Linear hardening) complete: SB-922–926 merged. SB-937 (pagination) and SB-939 (chezmoi __pycache__) filed and merged along the way; SB-938 (branch-prefix experiment) closed.
- 2026-08-30 — Phase 1 (gatekeeper) complete: SB-508 merged (dual-channel Telegram+Linear gates, 35 tests). SB-927 closed as superseded — fully covered by SB-508's suite.
- 2026-08-30 — Phase 2 (headless loop) complete: SB-928 (`/work-headless`), SB-929 (`cycle-runner` pick+run+merge), SB-930 (launchd + doctor, mini = `Toms-Mac-mini`) all merged. SB-941 (doctor.sh `stat` portability, same bug class SB-929 hit in CI) filed.
- **Next: first real run on the mini.** Human steps outstanding: confirm pilot repos (SB-508 checklist); on the mini, `chezmoi update` → `bash ~/.claude/skills/linear-crud/scripts/doctor.sh` → `launchctl load` the plist.
- 2026-09-02/03 — Phase 3: the runner moved to k3s. SB-975 (image, pinned toolchain + a build-time `claude` CLI contract), SB-976 (CronJob, Secret, PVC; the hand-rolled lock deleted in favour of `concurrencyPolicy: Forbid`), SB-978 (weekly refresh behind a green suite and a PR; the CronJob pins `:claude-<version>`), SB-979 (launchd deleted). **SB-870 was the first ticket taken from pick to merged PR entirely in-cluster.** Deploying found four defects nothing else would have: SB-980 (the runner died silently for 3 ticks because SB-974's provisioning step was never run, and the reporting credentials were in the same missing set), a curl HTTP/2 `PROTOCOL_ERROR` caused by a trailing newline in the Linear API key that `$(<file)` had always stripped, SB-982 (Telegram links unclickable on macOS — no `entities` were sent at all), and SB-985 (`REMINDER_AFTER_HOURS` is a debounce, not a curfew, and `REMINDER_EVERY_HOURS = 24` locks a reminder onto whatever hour it first fired).
