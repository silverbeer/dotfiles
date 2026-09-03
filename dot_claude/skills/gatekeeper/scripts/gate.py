#!/usr/bin/env python3
"""Dual-channel human-in-the-loop gates: Linear comment + Telegram DM (SB-508).

A headless agent that needs a human decision calls `gate.py open` and exits.
The full proposal lands as a Linear comment (the record); a summary lands as a
Telegram DM with Approve/Reject/Note buttons (the interrupt). `gate.py poll`
watches BOTH channels; the first decision wins, is recorded with its source,
and is echoed to the channel that lost so the two never disagree.

Gate state is a JSON file per gate under $GATEKEEPER_STATE/gates/. The Linear
comment carries a marker line `<!-- sb-agent:{kind}:{run_id}:{session_id} -->`
so a resumed run on either Mac can find its own comment, and so poll only
reads comments posted AFTER the proposal.

Labels: `gate:*` is a mutually-exclusive Linear label group, so setting one
means rewriting the issue's full label list minus any other `gate:*` — same
dance as set-driven.py, for the same API reason.

The protocol spec lives in docs/agentic-delivery.md → "Gate protocol".
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _linear_scripts() -> Path:
    """linear-crud's scripts dir: a sibling skill in the source tree and in the
    deployed ~/.claude alike (HERE = .../skills/gatekeeper/scripts)."""
    for p in (HERE.parent.parent / "linear-crud" / "scripts", Path.home() / ".claude/skills/linear-crud/scripts"):
        if (p / "linear_api.py").exists():
            return p
    sys.exit("gate: linear-crud/scripts not found — the gatekeeper needs the linear-crud skill")


sys.path.insert(0, str(_linear_scripts()))
sys.path.insert(0, str(HERE))
from linear_api import gql  # noqa: E402  (tests monkeypatch gate.gql)
from tg import (  # noqa: E402
    TelegramError,
    TelegramTransport,
    Transport,
    approve_keyboard,
    links_keyboard,
    parse_callback,
    send_text,
)

KINDS = ("plan", "cycle-plan", "triage", "pr", "merge", "blocked")
GATE_VALUES = ("awaiting-approval", "approved", "rejected", "needs-human")

# How much of the body goes to the phone. The Linear comment is the full text;
# the DM has to fit one screen or the approval is a rubber stamp.
SUMMARY_CHARS = 1500

# Echoes are posted by the same Linear user as decisions (one API key), so they
# carry a marker or poll would read its own "approved via Telegram" as input.
ECHO_MARKER = "<!-- sb-agent:echo -->"

DEFAULT_TIMEOUT_HOURS = 72


def state_dir() -> Path:
    return Path(os.environ.get("GATEKEEPER_STATE", "") or Path.home() / ".local/state/cycle-runner")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_decision(text: str) -> tuple[str, str | None] | None:
    """First line `approve` / `approve: note` / `reject: reason` → (decision,
    note). Case-insensitive. Anything else is not a decision."""
    first = text.strip().splitlines()[0].strip() if text.strip() else ""
    verb, sep, rest = first.partition(":")
    if verb.strip().lower() not in ("approve", "reject"):
        return None
    return verb.strip().lower(), (rest.strip() or None) if sep else None


# ------------------------------------------------------------------- Linear
# Module-level functions calling the module-global `gql`, so the tests can
# swap in a fake with `gate.gql = ...` and every path below uses it.


def issue_info(ticket: str) -> dict:
    q = """
    query($key: String!) {
      issue(id: $key) { id title url assignee { id } state { name type } labels { nodes { id name } } }
    }
    """
    issue = gql(q, {"key": ticket})["issue"]
    if not issue:
        sys.exit(f"gate: issue {ticket} not found")
    return issue


def create_comment(issue_id: str, body: str) -> str:
    m = """
    mutation($issueId: String!, $body: String!) {
      commentCreate(input: {issueId: $issueId, body: $body}) { success comment { id } }
    }
    """
    res = gql(m, {"issueId": issue_id, "body": body})["commentCreate"]
    if not res["success"]:
        sys.exit("gate: commentCreate returned success=false")
    return res["comment"]["id"]


def issue_comments(ticket: str) -> list[dict]:
    q = """
    query($key: String!) {
      issue(id: $key) { comments(first: 100) { nodes { id body createdAt user { id } } } }
    }
    """
    nodes = gql(q, {"key": ticket})["issue"]["comments"]["nodes"]
    return sorted(nodes, key=lambda c: c["createdAt"])


# Label name → id cache, shared with linear-crud's set-driven.py (SB-508):
# same file, same 1h TTL, same miss/stale-id-retry-once dance — see its
# label_ids() for the original. A `gate:*` re-stamp and a `driven:*` re-stamp
# are the same lookup, so sharing the cache file means the second of the two
# in any given hour is free rather than paying for its own full-page fetch.
# A function, not a constant, so XDG_CACHE_HOME is re-read every call — the
# same reason state_dir() is a function: tests point it at a tempdir.
LABEL_CACHE_TTL = 3600


def label_cache_path() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "linear-crud" / "labels.json"


def _fetch_labels() -> dict[str, str]:
    labels = gql("{ issueLabels(first: 250) { nodes { id name } } }")["issueLabels"]["nodes"]
    return {n["name"]: n["id"] for n in labels}


def _label_ids(need: str) -> dict[str, str]:
    """Name → id map guaranteed fresh if `need` is absent from it. Bypass with
    LINEAR_CRUD_NO_CACHE=1, same escape hatch as set-driven.py."""
    if os.environ.get("LINEAR_CRUD_NO_CACHE") == "1":
        return _fetch_labels()
    cache = label_cache_path()
    try:
        if time.time() - cache.stat().st_mtime < LABEL_CACHE_TTL:
            cached = json.loads(cache.read_text())
            if isinstance(cached, dict) and need in cached:
                return cached
    except (OSError, ValueError):
        pass
    fresh = _fetch_labels()
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=cache.parent, delete=False) as tmp:
            tmp.write(json.dumps(fresh))
        os.replace(tmp.name, cache)
    except OSError as e:
        print(f"warn: could not write label cache {cache}: {e}", file=sys.stderr)
    return fresh


def set_gate_label(ticket: str, value: str, issue: dict | None = None) -> dict:
    """Full-label rewrite: keep everything that is not `gate:*`, add the target.
    `gate` is an exclusive group, so appending would be refused by the API.

    Returns the issue it fetched (or was handed), so a caller that already has
    one — decide(), open_gate() — doesn't have to fetch it a second time."""
    issue = issue or issue_info(ticket)
    target = f"gate:{value}"
    current = [n["name"] for n in issue["labels"]["nodes"] if n["name"].startswith("gate:")]
    if current == [target]:
        return issue
    by_name = _label_ids(target)
    if target not in by_name:
        sys.exit(f"gate: label '{target}' does not exist — create the gate label group first")
    keep = [n["id"] for n in issue["labels"]["nodes"] if not n["name"].startswith("gate:")]
    m = """
    mutation($id: String!, $labelIds: [String!]!) {
      issueUpdate(id: $id, input: {labelIds: $labelIds}) { success }
    }
    """

    def update(label_id: str) -> dict:
        return gql(m, {"id": issue["id"], "labelIds": keep + [label_id]})["issueUpdate"]

    # A cached id can go stale if the label was deleted and re-created with the
    # same name; refetch (bypassing the cache) and retry exactly once — same
    # dance as set-driven.py's label_ids() caller.
    try:
        res = update(by_name[target])
    except SystemExit as e:
        msg = str(e)
        if "not found" not in msg and "Entity" not in msg:
            raise
        print(f"warn: {msg} — refetching labels and retrying once", file=sys.stderr)
        by_name = _fetch_labels()
        if target not in by_name:
            sys.exit(f"gate: label '{target}' does not exist — create the gate label group first")
        res = update(by_name[target])
    if not res["success"]:
        sys.exit("gate: issueUpdate returned success=false")
    return issue


def assignee_id() -> str:
    """LINEAR_ASSIGNEE_ID if set, else viewer{id} resolved once and cached —
    the API key is the assignee's own, so viewer IS the assignee."""
    env = os.environ.get("LINEAR_ASSIGNEE_ID", "").strip()
    if env:
        return env
    cache = state_dir() / "viewer.json"
    try:
        cached = json.loads(cache.read_text())["id"]
        if cached:
            return cached
    except (OSError, ValueError, KeyError):
        pass
    viewer = gql("{ viewer { id } }")["viewer"]["id"]
    _write_atomic(cache, json.dumps({"id": viewer}))
    return viewer


# -------------------------------------------------------------------- state


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as tmp:
        tmp.write(text)
    os.replace(tmp.name, path)


def gates_dir() -> Path:
    return state_dir() / "gates"


def load_gate(gate_id: str) -> dict:
    try:
        return json.loads((gates_dir() / f"{gate_id}.json").read_text())
    except OSError:
        sys.exit(f"gate: no such gate {gate_id}")


def save_gate(gate: dict) -> None:
    _write_atomic(gates_dir() / f"{gate['gate_id']}.json", json.dumps(gate, indent=2))


def all_gates() -> list[dict]:
    out = []
    if gates_dir().is_dir():
        for p in sorted(gates_dir().glob("*.json")):
            try:
                out.append(json.loads(p.read_text()))
            except ValueError:
                print(f"warn: unreadable gate state {p}", file=sys.stderr)
    return out


# A ticket can reach Done without its gate ever being answered: the PR merges,
# Linear closes the issue via "Fixes SB-N", and nothing tells the gate. It then
# sits `awaiting` forever, and SB-944 makes a pending gate skip the ticket — so
# a stale gate is not merely clutter, it can park a ticket permanently. Four of
# these accumulated in two days (SB-949).
CLOSED_STATE_TYPES = ("completed", "canceled")


def superseded_gates() -> list[dict]:
    """Awaiting gates whose ticket has already finished. Resolves each as
    `superseded`, clears its `gate:*` label, and returns what it closed."""
    closed = []
    for gate in awaiting_gates():
        try:
            issue = issue_info(gate["ticket"])
        except SystemExit:
            continue  # a deleted ticket is not this function's problem
        if (issue.get("state") or {}).get("type") not in CLOSED_STATE_TYPES:
            continue
        gate["status"] = "superseded"
        gate["source"] = "ticket-closed"
        gate["note"] = f"ticket reached {issue['state']['name']} without this gate being answered"
        save_gate(gate)
        # Drop the gate:* label entirely rather than stamping another value —
        # the question is moot, and gate:approved would imply a human answered.
        keep = [n["id"] for n in issue["labels"]["nodes"] if not n["name"].startswith("gate:")]
        try:
            gql(
                """
                mutation($id: String!, $labelIds: [String!]!) {
                  issueUpdate(id: $id, input: {labelIds: $labelIds}) { success }
                }
                """,
                {"id": issue["id"], "labelIds": keep},
            )
        except SystemExit:
            print(f"gate: could not clear gate label on {gate['ticket']}", file=sys.stderr)
        closed.append(gate)
    return closed


REMINDER_AFTER_HOURS = 12.0
REMINDER_EVERY_HOURS = 24.0

# Quiet hours (SB-985).
#
# REMINDER_AFTER_HOURS is a debounce, not a curfew: it fires 12 hours after a
# gate opens, whenever that lands. SB-870's gate opened at 16:03 and reminded
# at 04:30. And because REMINDER_EVERY_HOURS is exactly 24, a reminder that
# first fires at 04:30 fires at 04:30 every day for the life of the gate — the
# design does not occasionally wake the user, it settles on the hour that woke
# them. Raising REMINDER_AFTER_HOURS only changes which gates land badly.
#
# start == end disables the window. The offline suite sets that, so no test
# depends on when it happens to run.
QUIET_START_HOUR = int(os.environ.get("GATEKEEPER_QUIET_START", "21"))
QUIET_END_HOUR = int(os.environ.get("GATEKEEPER_QUIET_END", "8"))
QUIET_TZ = os.environ.get("GATEKEEPER_QUIET_TZ", "America/New_York")


def in_quiet_hours(when: datetime | None = None) -> bool:
    """True inside the local overnight window.

    The window wraps midnight, so the comparison cannot be a single `start <=
    h < end` — at 21:00–08:00 that is empty and would silently disable the
    feature rather than fail.
    """
    if QUIET_START_HOUR == QUIET_END_HOUR:
        return False
    try:
        local = (when or now_utc()).astimezone(ZoneInfo(QUIET_TZ))
    except (ZoneInfoNotFoundError, ValueError):
        # No tzdata is not a reason to start waking someone at 04:30, but it is
        # also not a reason to go permanently silent. Say so, treat as awake.
        print(f"gate: unknown timezone {QUIET_TZ!r} — quiet hours disabled", file=sys.stderr)
        return False
    h = local.hour
    if QUIET_START_HOUR < QUIET_END_HOUR:
        return QUIET_START_HOUR <= h < QUIET_END_HOUR
    return h >= QUIET_START_HOUR or h < QUIET_END_HOUR


def issue_url(ticket: str) -> str:
    """The ticket's own URL. A reminder that names a ticket must link to THAT
    ticket — a team-board link makes the reader go find it, which is the
    friction this channel exists to remove (SB-973)."""
    return f"https://linear.app/silverbeer/issue/{ticket}"


def _due_for_reminder(gate: dict) -> bool:
    """True at most once every REMINDER_EVERY_HOURS, and never before the gate
    has been parked REMINDER_AFTER_HOURS. Stamps the gate when it returns True,
    so a caller cannot accidentally re-notify."""
    # Inside quiet hours: not due, and NOT stamped (SB-985). Returning False
    # after stamping would drop the reminder for a full 24 hours — worse than
    # the 04:30 ping it is meant to prevent. Leaving the stamp alone means the
    # first tick after the window opens sends it, which is the "flush in the
    # morning" behaviour with no queue and nothing durable to lose.
    if in_quiet_hours():
        return False
    opened = datetime.fromisoformat(gate["opened_at"])
    if (now_utc() - opened).total_seconds() / 3600 < REMINDER_AFTER_HOURS:
        return False
    last = gate.get("reminded_at")
    if last and (now_utc() - datetime.fromisoformat(last)).total_seconds() / 3600 < REMINDER_EVERY_HOURS:
        return False
    gate["reminded_at"] = now_utc().isoformat()
    save_gate(gate)
    return True


def awaiting_gates() -> list[dict]:
    return sorted((g for g in all_gates() if g["status"] == "awaiting"), key=lambda g: g["opened_at"])


# --------------------------------------------------------------- gatekeeper


class Gatekeeper:
    """The side-effect half: everything that talks to Telegram or Linear.
    Constructed with a Transport so the tests can hand in a fake."""

    def __init__(self, transport: Transport, chat_id: str, allowed_ids: set[int]) -> None:
        self.transport = transport
        self.chat_id = chat_id
        self.allowed_ids = allowed_ids
        self.offset_path = state_dir() / "telegram-offset"

    # -------------------------------------------------------------- open

    def open_gate(self, kind: str, ticket: str, body: str, session_id: str, run_id: str, link: str) -> dict:
        gate_id = secrets.token_hex(4)  # 8 hex chars: callback_data must stay ≤ 64 bytes
        issue = issue_info(ticket)
        marker = f"<!-- sb-agent:{kind}:{run_id}:{session_id} -->"
        comment_id = create_comment(issue["id"], f"{marker}\n{body}")
        set_gate_label(ticket, "awaiting-approval", issue=issue)
        gate = {
            "gate_id": gate_id,
            "kind": kind,
            "ticket": ticket,
            "run_id": run_id,
            "session_id": session_id,
            "opened_at": now_utc().isoformat(),
            "status": "awaiting",
            "decision": None,
            "source": None,
            "note": None,
            "linear_comment_id": comment_id,
            "tg_message_id": None,
            "forwarded_comment_ids": [],
            "note_pending": False,
        }
        # Saved before the Telegram send: the Linear comment + label are
        # already live, so a DM failure below must not orphan the gate — it
        # stays resolvable via the Linear channel, and `poll` can find it.
        save_gate(gate)
        text = telegram_text(kind, ticket, issue["title"], body, link or issue["url"], issue["url"])
        # A `blocked` gate asks nothing, so it gets no verbs — but it still
        # gets the link buttons, because "go and look" IS the ask (SB-982).
        pr_link = link if link and link != issue["url"] else ""
        keyboard = (
            links_keyboard(issue["url"], pr_link)
            if kind == "blocked"
            else approve_keyboard(gate_id, issue["url"], pr_link)
        )
        try:
            # Silent inside quiet hours, not held. `open_gate` records
            # `tg_message_id` from this send and edits that message when the
            # gate resolves; a held DM has no id, so queueing would degrade
            # into the existing "DM failed" path — no buttons overnight and
            # gate state disagreeing with the chat. Silent delivery keeps the
            # buttons, the id and the edit intact (SB-985).
            sent = send_text(self.transport, self.chat_id, text, keyboard, silent=in_quiet_hours())
            gate["tg_message_id"] = sent.get("message_id")
            save_gate(gate)
        except Exception as e:
            print(
                f"warn: gate.py open: Telegram DM failed for {gate_id} ({ticket}): {e} — "
                "Linear comment and label are posted; the gate is resolvable via Linear",
                file=sys.stderr,
            )
        return gate

    # ------------------------------------------------------------ decide

    def decide(self, gate: dict, decision: str, note: str | None, source: str) -> None:
        """First decision wins — the caller checks status before calling. Echo
        goes to the channel that did NOT decide, so both always agree.

        The label update and echo happen BEFORE save_gate() marks the gate
        resolved: if either raises, the gate must stay "awaiting" on disk so
        `poll` retries it, rather than being marked resolved with a side
        effect that never actually landed."""
        status = "approved" if decision == "approve" else "rejected"
        icon = "✅" if decision == "approve" else "❌"
        suffix = f" — {note}" if note else ""
        try:
            issue = set_gate_label(gate["ticket"], status)
            if source != "linear":
                create_comment(issue["id"], f"{ECHO_MARKER}\n{icon} {status} via {source}{suffix}")
            if source != "telegram":
                msg = f"{icon} [{gate['kind']}] {gate['ticket']} {status} via {source}{suffix}"
                send_text(self.transport, self.chat_id, msg)
        except (SystemExit, Exception) as e:
            print(
                f"warn: gate.py decide: label update or echo failed for {gate['gate_id']} "
                f"({gate['ticket']}): {e} — gate stays awaiting, poll will retry",
                file=sys.stderr,
            )
            return
        gate["status"] = status
        gate["decision"] = decision
        gate["source"] = source
        if note:
            gate["note"] = f"{gate['note']}\n{note}" if gate.get("note") else note
        save_gate(gate)

    def mark_needs_human(self, gate: dict, hours: float) -> None:
        gate["status"] = "needs-human"
        gate["decision"] = "needs-human"
        gate["source"] = "timeout"
        save_gate(gate)
        set_gate_label(gate["ticket"], "needs-human")
        send_text(
            self.transport,
            self.chat_id,
            f"🛑 stuck: [{gate['kind']}] {gate['ticket']} gate {gate['gate_id']} unanswered for {hours:.0f}h — "
            f"resolve with `gate.py resolve {gate['gate_id']} approve|reject --source cli`",
        )

    # ----------------------------------------------------------- telegram

    def _offset(self) -> int:
        try:
            return int(self.offset_path.read_text().strip())
        except (OSError, ValueError):
            return 0

    def _remember(self, offset: int) -> None:
        _write_atomic(self.offset_path, str(offset))

    def drain_telegram(self, timeout: int) -> None:
        updates = self.transport.get_updates(
            offset=self._offset(), timeout=timeout, allowed_updates=["callback_query", "message"]
        )
        for update in updates:
            try:
                self._handle(update)
            except TelegramError as exc:
                # Transport trouble: the decision may not have been recorded.
                # Do NOT ack — Telegram replays unacked updates for 24h, which
                # is exactly the safety net wanted here. Stop the drain so the
                # rest of the batch is retried in order on the next poll
                # (SB-950: acking through a failure destroyed a real approval).
                print(f"gate: telegram error draining update {update.get('update_id')}: {exc}", file=sys.stderr)
                return
            except Exception as exc:  # noqa: BLE001 — poison-pill guard
                # A bug in our own handling, not a transport failure. Ack it:
                # an update we can never process must not block every future
                # poll behind it for 24h.
                print(
                    f"gate: dropping unprocessable update {update.get('update_id')}: {exc!r}",
                    file=sys.stderr,
                )
            self._remember(int(update["update_id"]) + 1)

    def _allowed(self, sender: dict) -> bool:
        # Numeric id, never username: usernames are changeable and, once
        # released, re-registerable by somebody else.
        try:
            return int(sender.get("id")) in self.allowed_ids
        except (TypeError, ValueError):
            return False

    def _handle(self, update: dict) -> None:
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
        elif "message" in update:
            self._handle_message(update["message"])

    def _answer_quietly(self, cq: dict, text: str = "") -> None:
        """Best-effort `answerCallbackQuery`. NEVER raises (SB-950).

        A callback query id expires about a minute after the tap, and this
        poller runs on a 30-minute tick, so the answer usually fails. It used
        to be called first and unguarded: the raise propagated, `drain_telegram`
        acked the update in its `finally` anyway, and the human's decision was
        destroyed by the failure of a courtesy toast. The toast is optional;
        the decision is the payload."""
        try:
            self.transport.answer_callback_query(cq.get("id", ""), text)
        except TelegramError as exc:
            print(f"gate: answerCallbackQuery failed (harmless): {exc}", file=sys.stderr)

    def _handle_callback(self, cq: dict) -> None:
        # Silence is the right reply to a stranger, and answering costs nothing.
        if not self._allowed(cq.get("from") or {}):
            self._answer_quietly(cq)
            return
        parsed = parse_callback(cq.get("data", ""))
        if parsed is None:
            self._answer_quietly(cq)
            return
        gate_id, verb = parsed
        gate = next((g for g in awaiting_gates() if g["gate_id"] == gate_id), None)
        if gate is None:
            self._answer_quietly(cq, "already resolved")
            send_text(self.transport, self.chat_id, f"gate {gate_id} is not awaiting (already resolved?)")
            return
        # Record FIRST, acknowledge second: everything below this line is a
        # courtesy, and a courtesy must not be able to lose a decision.
        if verb == "note":
            gate["note_pending"] = True
            save_gate(gate)
            self._answer_quietly(cq, "send your note")
            send_text(self.transport, self.chat_id, f"💬 reply with your note for [{gate['kind']}] {gate['ticket']}")
        else:
            self.decide(gate, verb, None, "telegram")
            self._answer_quietly(cq, f"{verb}d — {gate['ticket']}")

    def _handle_message(self, message: dict) -> None:
        chat = message.get("chat") or {}
        if chat.get("type") != "private" or not self._allowed(message.get("from") or {}):
            return
        text = (message.get("text") or "").strip()
        if not text:
            return
        # A pending 💬 Note claims the next text: it attaches, the gate stays
        # awaiting. Most recent claim wins if several are somehow pending.
        pending = [g for g in awaiting_gates() if g.get("note_pending")]
        if pending:
            gate = pending[-1]
            gate["note_pending"] = False
            gate["note"] = f"{gate['note']}\n{text}" if gate.get("note") else text
            save_gate(gate)
            send_text(self.transport, self.chat_id, f"noted on [{gate['kind']}] {gate['ticket']} — still awaiting")
            return
        decision = parse_decision(text)
        open_gates = awaiting_gates()
        if decision is None or not open_gates:
            return
        # Free text names no gate; it applies to the most recently opened one.
        self.decide(open_gates[-1], decision[0], decision[1], "telegram")

    # ------------------------------------------------------------- linear

    def check_linear(self, gate: dict) -> None:
        comments = issue_comments(gate["ticket"])
        marker_at = next((c["createdAt"] for c in comments if c["id"] == gate["linear_comment_id"]), None)
        if marker_at is None:
            return  # marker not in the first page — nothing trustworthy to read
        me = assignee_id()
        for c in comments:
            if c["createdAt"] <= marker_at or (c.get("user") or {}).get("id") != me:
                continue
            if c["body"].startswith(ECHO_MARKER):
                continue
            decision = parse_decision(c["body"])
            if decision is not None:
                self.decide(gate, decision[0], decision[1], "linear")
                return
            if c["id"] not in gate["forwarded_comment_ids"]:
                gate["forwarded_comment_ids"].append(c["id"])
                save_gate(gate)
                send_text(self.transport, self.chat_id, f"💬 {gate['ticket']} (Linear): {c['body']}")

    # --------------------------------------------------------------- poll

    def poll_once(self, timeout: int) -> list[dict]:
        # Before anything else: a gate whose ticket is already finished has no
        # question left to ask (SB-949).
        for gate in superseded_gates():
            print(
                f"gate: {gate['gate_id']} ({gate['ticket']}, {gate['kind']}) superseded — {gate['note']}",
                file=sys.stderr,
            )
        timeout_hours = float(os.environ.get("GATE_TIMEOUT_HOURS", DEFAULT_TIMEOUT_HOURS))
        cutoff = now_utc() - timedelta(hours=timeout_hours)
        timed_out = set()
        for gate in awaiting_gates():
            if datetime.fromisoformat(gate["opened_at"]) < cutoff:
                self.mark_needs_human(gate, timeout_hours)
                timed_out.add(gate["gate_id"])
        still_awaiting = {g["gate_id"] for g in awaiting_gates()}
        if still_awaiting:
            self.drain_telegram(timeout)
        for gate in awaiting_gates():
            self.check_linear(gate)
        # `before` covers both gates that resolved via Telegram/Linear this
        # tick AND ones that just timed out, so timeout→needs-human shows up
        # in the same "resolved" list `poll`'s JSON output reports.
        before = still_awaiting | timed_out
        return [g for g in all_gates() if g["gate_id"] in before and g["status"] != "awaiting"]


# ------------------------------------------------------------------ wiring


def telegram_text(kind: str, ticket: str, title: str, body: str, link: str, issue_url: str = "") -> str:
    """The DM for one gate.

    The ticket link is ALWAYS present (SB-954). `pr` and `merge` gates pass the
    PR as `link`, and with a single-link trailer that silently replaced the only
    route back to the ticket — on exactly the gates where a human wants both:
    read the PR, decide on the ticket. It also matters for answering: until the
    long-poll agent lands (SB-951), a Linear comment is the more reliable
    channel, and the message has to say so.
    """
    summary = body.strip()
    if len(summary) > SUMMARY_CHARS:
        summary = summary[:SUMMARY_CHARS].rstrip() + "…"

    ticket_url = issue_url or link
    trailer = [f"Ticket: {ticket_url}"]
    if link and link != ticket_url:
        trailer.append(f"PR:     {link}")
    # `blocked` gets no keyboard, so telling its reader to tap would be a lie.
    if kind != "blocked":
        trailer.append("Approve with the buttons, or comment `approve` on the ticket.")

    return f"[{kind}] {ticket} — {title}\n\n{summary}\n\n" + "\n".join(trailer)


def gatekeeper_from_env() -> Gatekeeper:
    token = os.environ.get("GATEKEEPER_TG_TOKEN", "").strip()
    chat_id = os.environ.get("GATEKEEPER_TG_CHAT_ID", "").strip()
    if not token or not chat_id:
        sys.exit("gate: GATEKEEPER_TG_TOKEN and GATEKEEPER_TG_CHAT_ID are not set — source scripts/env.sh")
    raw = os.environ.get("GATEKEEPER_ALLOWED_USER_IDS", "").strip() or chat_id
    try:
        allowed = {int(c) for c in raw.replace(",", " ").split()}
    except ValueError:
        sys.exit("gate: GATEKEEPER_ALLOWED_USER_IDS must be numeric Telegram user ids")
    return Gatekeeper(TelegramTransport(token), chat_id, allowed)


def cmd_open(args: argparse.Namespace) -> int:
    body = sys.stdin.read() if args.body == "/dev/stdin" else Path(args.body).read_text()
    if args.dry_run:
        # No network at all: no Linear lookup (so no real title), no Telegram.
        print(
            json.dumps(
                {
                    "status": "dry-run",
                    "kind": args.kind,
                    "ticket": args.ticket,
                    "marker": f"<!-- sb-agent:{args.kind}:{args.run_id}:{args.session_id} -->",
                    "telegram": telegram_text(
                        args.kind,
                        args.ticket,
                        "(title not fetched)",
                        body,
                        args.link or "(issue url)",
                        "(issue url)",
                    ),
                    "keyboard": args.kind != "blocked",
                },
                indent=2,
            )
        )
        return 0
    gk = gatekeeper_from_env()
    gate = gk.open_gate(args.kind, args.ticket, body, args.session_id, args.run_id, args.link)
    print(json.dumps({"status": "awaiting", "gate_id": gate["gate_id"]}))
    return 0


def cmd_poll(args: argparse.Namespace) -> int:
    gk = gatekeeper_from_env()
    resolved: list[dict] = []
    while True:
        resolved.extend(gk.poll_once(args.timeout))
        if args.once or not awaiting_gates():
            break
    print(
        json.dumps(
            {
                "resolved": [
                    {"gate_id": g["gate_id"], "ticket": g["ticket"], "status": g["status"], "source": g["source"]}
                    for g in resolved
                ],
                "awaiting": len(awaiting_gates()),
                # So run.sh can send its own summary silently in the window
                # without reimplementing the clock (SB-985).
                "quiet": in_quiet_hours(),
                # Named, not just counted (SB-949): an idle tick sends no
                # Telegram summary, so a parked ticket is invisible — "you are
                # the bottleneck" looks exactly like "nothing to do".
                # `notify` is the rate limit (SB-973). The first version keyed
                # the reminder on `hours >= 12`, which stays true forever, so it
                # fired on EVERY tick once a gate crossed 12h — five messages in
                # 96 minutes, the exact nagging SB-945 removed. A parked gate is
                # worth one reminder a day, not one every 30 minutes.
                "parked": [
                    {
                        "ticket": g["ticket"],
                        "kind": g["kind"],
                        "url": issue_url(g["ticket"]),
                        "hours": round((now_utc() - datetime.fromisoformat(g["opened_at"])).total_seconds() / 3600, 1),
                        "notify": _due_for_reminder(g),
                    }
                    for g in awaiting_gates()
                ],
            },
            indent=2,
        )
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    print(json.dumps(load_gate(args.gate_id) if args.gate_id else all_gates(), indent=2))
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    gate = load_gate(args.gate_id)
    if gate["status"] not in ("awaiting", "needs-human"):
        sys.exit(f"gate: {args.gate_id} is already {gate['status']}")
    gk = gatekeeper_from_env()
    gk.decide(gate, args.decision, args.note, args.source)
    print(json.dumps({"gate_id": gate["gate_id"], "status": gate["status"], "source": gate["source"]}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gate.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("open", help="open a gate: Linear comment + label + Telegram DM")
    p.add_argument("--kind", required=True, choices=KINDS)
    p.add_argument("--ticket", required=True)
    p.add_argument("--body", required=True, help="file with the full proposal (markdown)")
    p.add_argument("--session-id", default="")
    p.add_argument("--run-id", default="")
    p.add_argument("--link", default="", help="deep link for the DM (default: the issue url)")
    p.add_argument("--dry-run", action="store_true", help="no network; print what would be sent")
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("poll", help="drain Telegram + read Linear comments until gates resolve")
    p.add_argument("--once", action="store_true")
    p.add_argument("--timeout", type=int, default=25, help="getUpdates long-poll seconds")
    p.set_defaults(func=cmd_poll)

    p = sub.add_parser("status", help="JSON of one gate, or all")
    p.add_argument("gate_id", nargs="?")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("resolve", help="manual override, same side effects as a decision")
    p.add_argument("gate_id")
    p.add_argument("decision", choices=("approve", "reject"))
    p.add_argument("--note", default=None)
    p.add_argument("--source", default="cli")
    p.set_defaults(func=cmd_resolve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
