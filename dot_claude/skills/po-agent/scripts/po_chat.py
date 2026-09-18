#!/usr/bin/env python3
"""po_chat.py — talk to the PO agent from Telegram (SB-1089).

    python3 po_chat.py consume                        # the po-chat Deployment: runs for ever
    python3 po_chat.py ask SB-12 "estimate it? 1/2/3" # inside that pod: post a PO question

`consume` reads the gatekeeper inbox, never Telegram: listen.py is the one
getUpdates reader (SB-951), and a second one is a 409 for both. Messages are
handled one at a time, in this order:

  1. A reply to one of the PO's questions. The answer is scanned with the
     runner's own gitleaks `scan_log_clean`, then posted as a comment on that
     ticket. If the scan fails or can't run, nothing is posted and the text
     goes nowhere else. A question reply never touches a pending proposal.
  2. Otherwise, a proposal is pending. The message confirms it only if it is a
     plain yes, sent after the dry run was delivered, within 30 minutes of it,
     not a reply to some other message, and the file still hashes to what was
     shown. Then `cycle_apply.py --confirm` runs, in ONE function,
     apply_pending. Anything else, including "no", discards the proposal, and
     nothing is written.
  3. The daily budget. Over it, the bot says so and never calls `claude`.
  4. `claude -p`, one resumed session per cycle. The prompt, on stdin, carries
     the live cycle_state.py JSON; the system prompt is chat.md plus
     commands/cycle.md, both read at call time.

The model gets NO tools (`--tools ""`), so it can't write to Linear, or to
anything else. All it can do is return `changes`. This script dry-runs them,
shows the output and waits for a LATER message. A change set is never
confirmed in the turn that proposed it.

State lives under $GATEKEEPER_STATE/po-chat/, which is the runner's PVC in k3s:
`sessions/cycle-<N>.json`, `pending.json` + `pending-changes.json`,
`questions/<telegram message_id>.json`, `ledger/<ET date>.json`,
`log/<ET date>.jsonl`, `notes.json` (what the PO must hear next turn),
`attempts/<update_id>`, `heartbeat` and `work/` (claude's cwd).

Exit codes: 0 stopped (SIGTERM), 75 dotfiles@main moved (the container's loop
fetches, resets and runs this again), anything else is a crash.

check-claude-cli-contract.sh reads every `--flag` string literal in this file:
claude's between the `claude-cli-contract` markers, and the flags of our own
scripts between `not-claude-argv` markers. A flag literal anywhere else fails CI.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import http.client
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
SKILLS = HERE.parents[1]


def _sibling(skill: str, marker: str) -> Path:
    """A peer skill's scripts dir: the chezmoi source tree (or the pod's clone),
    then the deployed ~/.claude copy, as cycle_state.py resolves its own."""
    for p in (SKILLS / skill / "scripts", Path.home() / ".claude/skills" / skill / "scripts"):
        if (p / marker).is_file():
            return p
    sys.exit(f"po_chat: {skill}/scripts/{marker} not found — the {skill} skill must be installed")


GATEKEEPER = _sibling("gatekeeper", "gate.py")
# scan_log_clean lives here. It is sourced, never copied: one gitleaks
# invocation for everything that leaves the machine.
RUN_SH = _sibling("cycle-runner", "run.sh") / "run.sh"
sys.path.insert(0, str(GATEKEEPER))

import gate  # noqa: E402  (tests monkeypatch gate.gql)
import inbox  # noqa: E402
from tg import TelegramError, send_text  # noqa: E402

EXIT_MOVED = 75
POLL_SECONDS = 2
UPDATE_CHECK_SECONDS = 300
TYPING_EVERY_SECONDS = 4
# The liveness probe allows 120s. Beaten for the whole of a message, not only
# while claude runs: a slow cycle_state or dry run must not look like a hang.
HEARTBEAT_EVERY_SECONDS = 20
CLAUDE_TIMEOUT_SECONDS = 150
STATE_TIMEOUT_SECONDS = 90
APPLY_TIMEOUT_SECONDS = 120
SCAN_TIMEOUT_SECONDS = 60
PENDING_TTL_SECONDS = 30 * 60
# A message that killed the process this many times is dropped, not retried
# for ever: a poison message must not wedge the chat.
MAX_ATTEMPTS = 2
LEDGER_TZ = "America/New_York"
# Our own recent message ids, kept for gate.py. Bounded, and far more than a
# day of conversation.
OUTBOX_LIMIT = 500
SENT_TELEGRAM_ERRORS = (TelegramError, OSError, http.client.HTTPException)

# A plain yes, and nothing else. "yes but drop SB-4" is not consent to the list
# as shown, and neither is "yeah" or "sure": the user can say yes.
YES = re.compile(r"(yes|y|yes please|apply)[.!]?", re.IGNORECASE)
TICKET = re.compile(r"SB-\d+")

# cycle_apply.py's changes file, enforced on the model's output. cycle_apply's
# own validate() refuses the same things again at dry run.
CHANGES_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "cycle": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "required": ["number"],
            "properties": {
                "number": {"type": "integer"},
                "planning": {"type": ["string", "null"], "enum": ["planned", "skipped", None]},
                "note": {"type": ["string", "null"]},
            },
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["identifier"],
                "properties": {
                    "identifier": {"type": "string", "pattern": r"^SB-\d+$"},
                    "cycle": {"type": ["integer", "null"]},
                    "priority": {"type": "integer", "enum": [0, 1, 2, 3, 4]},
                    "estimate": {"type": "integer"},
                    "cancel": {"type": "boolean"},
                },
            },
        },
    },
}

RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "reply": {"type": "string"},
        "changes": {"anyOf": [CHANGES_SCHEMA, {"type": "null"}]},
        "ask": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "properties": {"ticket": {"type": "string"}, "question": {"type": "string"}},
            "required": ["ticket", "question"],
        },
    },
    "required": ["reply", "changes", "ask"],
}

# Never handed to `claude`. The model has no tools, so nothing could read them
# anyway; they don't need to be in its environment either.
CLAUDE_ENV_DROP = ("LINEAR_API_KEY", "GATEKEEPER_TG_TOKEN", "GH_TOKEN")


def log(msg: str) -> None:
    print(f"po_chat: {msg}", file=sys.stderr, flush=True)


def chat_dir() -> Path:
    return gate.state_dir() / "po-chat"


def _usd(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        log(f"{name} is not a number, using {default}")
        return default


def model() -> str:
    return os.environ.get("PO_CHAT_MODEL", "").strip() or "sonnet"


def message_cap() -> float:
    return _usd("PO_CHAT_MSG_USD", 0.75)


def daily_cap() -> float:
    return _usd("PO_CHAT_DAILY_USD", 10.0)


def max_turns() -> int:
    try:
        return max(1, int(os.environ.get("PO_CHAT_MAX_TURNS", "").strip() or 30))
    except ValueError:
        return 30


def cycle_md() -> Path:
    """commands/cycle.md, beside skills/ in the source tree, the pod's clone
    and the deployed ~/.claude alike."""
    for p in (HERE.parents[2] / "commands" / "cycle.md", Path.home() / ".claude/commands/cycle.md"):
        if p.is_file():
            return p
    raise FileNotFoundError("commands/cycle.md not found beside the skills")


def system_prompt() -> str:
    """chat.md then cycle.md, read on every call. cycle.md holds the rules and
    chat.md only adapts them, so an edit to /cycle reaches the chat with no
    copy to drift."""
    return (
        (HERE.parent / "chat.md").read_text()
        + "\n\n# The /cycle rules (commands/cycle.md)\n\n"
        + cycle_md().read_text()
    )


def prompt_sha(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()[:16]


def claude_argv(session_id: str, new_session: bool, appended: str) -> list[str]:
    """The one `claude` invocation. The prompt goes on stdin, not argv: a full
    cycle_state document is tens of kilobytes. check-claude-cli-contract.sh
    reads the flags between the markers below.

    SB-991: nothing interactive may leak in. --tools "" leaves no tools;
    --strict-mcp-config loads no MCP servers; --disable-slash-commands loads no
    skills; --setting-sources "" reads no settings file, so no plugin is
    enabled and no hook runs. The pod also points CLAUDE_CONFIG_DIR at a
    directory of its own, with no plugins installed in it."""
    # claude-cli-contract: begin
    argv = [
        "claude",
        "-p",
        "--session-id" if new_session else "--resume",
        session_id,
        "--model",
        model(),
        "--max-budget-usd",
        f"{message_cap():.2f}",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(RESPONSE_SCHEMA),
        "--tools",
        "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--setting-sources",
        "",
        "--append-system-prompt",
        appended,
    ]
    # claude-cli-contract: end
    return argv


def claude_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in CLAUDE_ENV_DROP}


def state_sha(doc: dict) -> str:
    """Identity of the cycle state, ignoring what only the clock moves:
    `generated_at` and the day counts. Otherwise every day's first message would
    resend the whole document into the session, for numbers that did not
    change."""
    body = copy.deepcopy(doc)
    body.pop("generated_at", None)
    for cycle in (body.get("cycle"), (body.get("summary") or {}).get("cycle")):
        if isinstance(cycle, dict):
            cycle.pop("days_left", None)
            cycle.pop("days_elapsed", None)
    for row in body.get("issues") or []:
        row.pop("days_since_activity", None)
    for row in (body.get("at_risk") or {}).get("stalled") or []:
        row.pop("days_since_activity", None)
    for row in body.get("waiting_on_human") or []:
        row.pop("age_days", None)
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


def build_prompt(doc: dict, sha: str, last_sha: str | None, text: str, notes: list[str]) -> str:
    if last_sha == sha:
        cycle = doc.get("cycle") or {}
        state = (f'<cycle_state sha="{sha}" unchanged="true" generated_at="{doc.get("generated_at")}" '
                 f'days_left="{cycle.get("days_left")}">Unchanged since the cycle_state you were given, '
                 "except the clock: use days_left above; day counts per ticket may have grown.</cycle_state>")
    else:
        state = f'<cycle_state sha="{sha}">\n{json.dumps(doc, separators=(",", ":"))}\n</cycle_state>'
    parts = [state]
    parts += [f"<system_note>{n}</system_note>" for n in notes]
    parts.append(f"<telegram_message>\n{text}\n</telegram_message>")
    return "\n\n".join(parts)


def parse_result(stdout: str) -> dict | None:
    """`--output-format json` prints one result object. Anything printed before
    it (a warning) is skipped by trying the last line that parses."""
    for candidate in [stdout, *reversed((stdout or "").strip().splitlines())]:
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class StateError(Exception):
    """cycle_state.py could not produce a document."""


class _Stop(Exception):
    """Raised out of the idle wait by SIGTERM."""


class _Requeue(Exception):
    """SIGTERM arrived before or during the claude call: the message goes back to new/."""


class Chat:
    def __init__(
        self,
        transport,
        chat_id: str,
        allowed_ids: set[int] | None = None,
        *,
        run=None,
        now=None,
        sleep=time.sleep,
        clock=time.monotonic,
        start_sha: str | None = None,
        ls_remote=None,
        update_every: float = UPDATE_CHECK_SECONDS,
    ) -> None:
        self.transport = transport
        self.chat_id = str(chat_id)
        self.allowed_ids = allowed_ids
        self.run = run or self._run
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep
        self.clock = clock
        self.start_sha = start_sha
        self.ls_remote = ls_remote
        self.update_every = update_every
        self.stopping = False
        self._waiting = False
        self._child: subprocess.Popen | None = None

    # ------------------------------------------------------------ files

    @property
    def dir(self) -> Path:
        return chat_dir()

    @staticmethod
    def _read(path: Path):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _write(path: Path, obj) -> None:
        gate._write_atomic(path, json.dumps(obj, indent=2))

    def _today(self) -> str:
        return self.now().astimezone(ZoneInfo(LEDGER_TZ)).date().isoformat()

    def beat(self) -> None:
        try:
            gate._write_atomic(self.dir / "heartbeat", self.now().isoformat())
        except OSError as exc:
            log(f"could not write heartbeat: {exc}")

    @contextmanager
    def _heartbeat(self):
        stop = threading.Event()

        def loop() -> None:
            while True:
                self.beat()
                if stop.wait(HEARTBEAT_EVERY_SECONDS):
                    return

        beating = threading.Thread(target=loop, daemon=True)
        beating.start()
        try:
            yield
        finally:
            stop.set()
            beating.join(timeout=2)

    def chat_log(self, update_id: int | None, direction: str, text: str, **extra) -> None:
        entry = {"ts": self.now().isoformat(), "update_id": update_id, "dir": direction, "text": text,
                 "cost": extra.get("cost"), "session_id": extra.get("session_id"),
                 "latency_s": extra.get("latency_s")}
        path = self.dir / "log" / f"{self._today()}.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError as exc:
            log(f"could not append to the chat log: {exc}")

    def say(self, update_id: int | None, text: str, **extra) -> dict:
        self.chat_log(update_id, "out", text, **extra)
        sent = send_text(self.transport, self.chat_id, text)
        self.remember_outbound(sent)
        return sent

    def remember_outbound(self, sent: dict) -> None:
        """Record our own message ids where gate.py can see them. A reply to one
        of these is a conversation with the PO, and the listener must not read
        it as an answer to whichever gate happens to be open (SB-1089)."""
        ids = sent.get("message_ids") or ([sent["message_id"]] if sent.get("message_id") is not None else [])
        if not ids:
            return
        path = self.dir / "outbox.json"
        known = [i for i in (self._read(path) or []) if isinstance(i, int)]
        known.extend(i for i in ids if isinstance(i, int) and i not in known)
        try:
            self._write(path, known[-OUTBOX_LIMIT:])
        except OSError as exc:
            log(f"could not record our own message ids: {exc}")

    # ------------------------------------------------------------ notes
    # What the PO has to hear on its next turn: a change applied or discarded,
    # a question answered. Kept on disk until a claude call succeeds, so a
    # message stopped by the budget cap loses none of them.

    def notes(self) -> list[str]:
        return self._read(self.dir / "notes.json") or []

    def add_note(self, note: str) -> None:
        self._write(self.dir / "notes.json", [*self.notes(), note])

    def clear_notes(self) -> None:
        (self.dir / "notes.json").unlink(missing_ok=True)

    # --------------------------------------------------------- attempts

    def _attempt_path(self, update_id: int) -> Path:
        return self.dir / "attempts" / str(update_id)

    def _attempt(self, update_id: int) -> int:
        path = self._attempt_path(update_id)
        try:
            n = int(path.read_text().strip()) + 1
        except (OSError, ValueError):
            n = 1
        gate._write_atomic(path, str(n))
        return n

    def _forget_attempt(self, update_id: int) -> None:
        path = self._attempt_path(update_id)
        try:
            n = int(path.read_text().strip()) - 1
        except (OSError, ValueError):
            return
        if n > 0:
            gate._write_atomic(path, str(n))
        else:
            path.unlink(missing_ok=True)

    # ------------------------------------------------------- subprocess

    def _run(self, argv: list[str], *, timeout: float, cwd: str | None = None, env: dict | None = None,
             interruptible: bool = False, input: str | None = None) -> subprocess.CompletedProcess:
        """subprocess.run with stdin closed unless `input` is given. Only an
        interruptible child (claude) is terminated by SIGTERM; cycle_apply
        mid-write is left to finish."""
        proc = subprocess.Popen(argv, cwd=cwd, env=env,
                                stdin=subprocess.DEVNULL if input is None else subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if interruptible:
            self._child = proc
        try:
            out, err = proc.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
        finally:
            self._child = None
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)

    # ---------------------------------------------------------- writing

    @property
    def pending_path(self) -> Path:
        return self.dir / "pending.json"

    @property
    def changes_path(self) -> Path:
        return self.dir / "pending-changes.json"

    def _discard_pending(self) -> None:
        self.pending_path.unlink(missing_ok=True)
        self.changes_path.unlink(missing_ok=True)

    def dry_run(self, changes: dict, cycle: int | None) -> tuple[str, dict | None]:
        """Write the change set and dry-run it. Returns the text that goes under
        the PO's reply and, if the dry run accepted it, the proposal. The
        proposal is NOT live yet: keep_pending makes it live once the message
        showing it has actually been delivered."""
        replaced = self.pending_path.exists()
        self._discard_pending()
        body = json.dumps(changes, indent=2)
        gate._write_atomic(self.changes_path, body)
        # not-claude-argv: begin
        argv = [sys.executable, str(HERE / "cycle_apply.py"), "--changes", str(self.changes_path)]
        # not-claude-argv: end
        try:
            res = self.run(argv, timeout=APPLY_TIMEOUT_SECONDS)
            out, rc = ((res.stdout or "") + (res.stderr or "")).strip(), res.returncode
        except subprocess.TimeoutExpired:
            out, rc = f"the dry run timed out after {APPLY_TIMEOUT_SECONDS}s", 1
        if rc != 0:
            self._discard_pending()
            self.add_note(f"The dry run refused your change set, so nothing is pending and nothing was written:\n{out}")
            return f"The dry run refused that change set. Nothing is pending and nothing was written.\n\n{out}", None
        lead = "This replaces the earlier proposal, which was not applied.\n\n" if replaced else ""
        proposal = {"changes": changes, "sha256": _sha256(body.encode()), "proposed_at": self.now().isoformat(),
                    "cycle": cycle}
        return f"{lead}Dry run:\n\n{out}\n\nReply yes to write exactly this, or no.", proposal

    def keep_pending(self, proposal: dict, sent: dict) -> None:
        """Make a proposal live, bound to the message that showed it. `sent_at`
        is Telegram's own date for that message when it gives one, so it
        compares with the dates on the user's replies."""
        message_id = sent.get("message_id")
        if message_id is None:
            self._discard_pending()
            self.add_note("Your change set could not be shown to the user, so nothing is pending.")
            log("the dry run was sent, but Telegram returned no message_id — proposal discarded")
            return
        date = sent.get("date")
        sent_at = int(date) if isinstance(date, int) else int(self.now().timestamp())
        # Every chunk: a long dry run is split, and the yes comes back on
        # whichever chunk the user was reading.
        ids = [i for i in (sent.get("message_ids") or [message_id]) if isinstance(i, int)]
        self._write(self.pending_path, {**proposal, "message_id": message_id, "message_ids": ids,
                                        "sent_at": sent_at, "expires_at": sent_at + PENDING_TTL_SECONDS})

    def apply_pending(self, changes_file: Path) -> subprocess.CompletedProcess:
        """THE ONLY PLACE `--confirm` IS PASSED. It is reached from exactly one
        path: settle_pending's consent check."""
        # not-claude-argv: begin
        argv = [sys.executable, str(HERE / "cycle_apply.py"), "--changes", str(changes_file), "--confirm"]
        # not-claude-argv: end
        return self.run(argv, timeout=APPLY_TIMEOUT_SECONDS)

    def settle_pending(self, update_id: int, rec: dict, text: str) -> bool:
        """Step 2. True when a yes was acted on (nothing more to do). A message
        older than the proposal leaves it alone; any other message discards it."""
        pending = self._read(self.pending_path)
        if pending is None or pending.get("message_id") is None or not isinstance(pending.get("sent_at"), int):
            self._discard_pending()  # never delivered, so never live
            return False
        date = rec.get("date") if isinstance(rec.get("date"), int) else 0
        if date <= pending["sent_at"]:
            return False  # sent before the proposal it would answer: a replay, or crossed in flight
        reply_to = rec.get("reply_to_message_id")
        shown_on = [i for i in (pending.get("message_ids") or [pending["message_id"]]) if isinstance(i, int)]
        in_reply = reply_to is None or reply_to in shown_on
        expired = date >= int(pending.get("expires_at") or 0)
        try:
            intact = _sha256(self.changes_path.read_bytes()) == pending.get("sha256")
        except OSError:
            intact = False
        said_yes = bool(YES.fullmatch(text.strip()))

        if said_yes and in_reply and not expired and intact:
            self._discard_pending_record()
            try:
                res = self.apply_pending(self.changes_path)
            except Exception as exc:  # noqa: BLE001 — a timeout or a crash mid-write
                self.changes_path.unlink(missing_ok=True)
                log(f"update {update_id}: cycle_apply.py --confirm did not finish: {exc!r}")
                msg = "The write was interrupted and may be partly applied — check with /cycle review before retrying"
                self.add_note(f"The user said yes, but {msg[0].lower()}{msg[1:]}.")
                self.say(update_id, f"⚠️ {msg}.")
                return True
            self.changes_path.unlink(missing_ok=True)
            out = ((res.stdout or "") + (res.stderr or "")).strip()
            # Notes before the reply: the write has happened, and a failed send
            # must not also hide that from the PO.
            if res.returncode == 0:
                self.add_note(f"The user said yes, and the pending change set for cycle {pending.get('cycle')} "
                              f"was applied. cycle_apply.py output:\n{out}")
                self.say(update_id, f"✅ Written.\n\n{out}")
            else:
                self.add_note(f"The user said yes, but cycle_apply.py failed and may be partly applied:\n{out}")
                self.say(update_id, f"❌ The write failed and may be partly applied:\n\n{out}\n\n"
                                    "Check with /cycle review before retrying.")
            return True

        self._discard_pending()
        if said_yes and not in_reply:
            reason = "the yes was a reply to a different message"
        elif said_yes and expired:
            reason = f"it was older than {PENDING_TTL_SECONDS // 60} minutes"
        elif said_yes:
            reason = "its file changed after the dry run was shown"
        else:
            reason = "the user's next message was not a yes"
        self.add_note(f"The pending change set was discarded and nothing was written: {reason}.")
        self.say(update_id, "Nothing written." if not said_yes
                 else f"Nothing written: that proposal was discarded because {reason}.")
        return False

    def _discard_pending_record(self) -> None:
        """Consumed before --confirm starts, so no later message can confirm it twice."""
        self.pending_path.unlink(missing_ok=True)

    # -------------------------------------------------------- questions

    @property
    def questions_dir(self) -> Path:
        return self.dir / "questions"

    def question_for(self, rec: dict) -> Path | None:
        reply_to = rec.get("reply_to_message_id")
        if not isinstance(reply_to, int):
            return None
        path = self.questions_dir / f"{reply_to}.json"
        return path if path.is_file() else None

    def post_question(self, ticket: str, issue: dict, question: str) -> dict | None:
        """Send one PO question as its own message, and remember its message id:
        a Telegram reply to that message is the answer."""
        text = (f"❓ {ticket} — {issue['title']}\n{question}\n"
                "Reply to this message to answer; your reply is posted as a comment on the ticket.\n"
                f"{issue['url']}")
        sent = self.say(None, text)
        message_id = sent.get("message_id")
        if message_id is None:
            log(f"question on {ticket} sent, but Telegram returned no message_id — a reply can't be matched")
            return None
        rec = {"ticket": ticket, "issue_id": issue["id"], "url": issue["url"], "question": question,
               "asked_at": self.now().isoformat(), "message_id": message_id, "answered_update_id": None}
        self._write(self.questions_dir / f"{int(message_id)}.json", rec)
        return rec

    def ask_from_model(self, ask: dict) -> None:
        ticket = str(ask.get("ticket") or "").strip()
        question = str(ask.get("question") or "").strip()
        if not TICKET.fullmatch(ticket) or not question:
            log(f"dropped a PO question: {ticket!r} is not a ticket key like SB-123, or the question is empty")
            return
        try:
            issue = gate.issue_info(ticket)
        except (SystemExit, Exception) as exc:  # noqa: BLE001 — gate.issue_info exits on an unknown ticket
            log(f"dropped a PO question on {ticket}: {exc}")
            return
        try:
            self.post_question(ticket, issue, question)
        except SENT_TELEGRAM_ERRORS as exc:
            log(f"could not send the PO question on {ticket}: {exc!r}")

    def scan(self, body: str) -> int:
        """run.sh's scan_log_clean over the comment, as a file. 0 clean, 1
        flagged, 2 gitleaks missing. Fails closed: anything but 0 posts nothing.

        `$0` is deliberately not run.sh's path. run.sh returns early when
        sourced, which it detects by BASH_SOURCE[0] != $0."""
        with tempfile.TemporaryDirectory() as tmp:
            comment = Path(tmp) / "comment.md"
            comment.write_text(body)
            report = Path(tmp) / "gitleaks.out"
            argv = ["bash", "-c", 'source "$1" && scan_log_clean "$2" "$3"', "po_chat",
                    str(RUN_SH), str(comment), str(report)]
            try:
                res = self.run(argv, timeout=SCAN_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                log("the secret scan timed out — failing closed")
                return 2
            if res.returncode != 0:
                tail = report.read_text()[-500:] if report.exists() else (res.stdout or "")[-500:]
                log(f"secret scan exit {res.returncode} (redacted): {tail.strip()}")
            return res.returncode

    def answer_question(self, update_id: int, path: Path, text: str) -> bool:
        """Step 1. Returns whether the text may go on to the PO. A reply the
        scan did not pass goes nowhere: not to Linear, the chat log or claude."""
        q = self._read(path)
        if q is None:
            return True
        ticket = q["ticket"]
        if q.get("answered_update_id") == update_id:
            return True  # this very message, retried after a crash: already posted
        if q.get("answered_update_id") is not None:
            self.say(update_id, f"{ticket}'s question already has an answer on the ticket, so this reply "
                                "was not posted.")
            return False
        # The echo marker keeps gate.py's Linear reader from forwarding this
        # comment back to Telegram as "discussion" on an open gate.
        body = f"{gate.ECHO_MARKER}\nPO question (Telegram): {q['question']}\n\nAnswer: {text}"
        rc = self.scan(body)
        if rc != 0:
            why = "the secret scan flagged it" if rc == 1 else "the secret scan could not run"
            self.chat_log(update_id, "in", f"[answer withheld: {why}]")
            self.add_note(f"The user replied to your question on {ticket}, but the answer was withheld: {why}. "
                          "Nothing was posted to Linear.")
            self.say(update_id, f"🔒 Not posted to {ticket}: {why}. Nothing went to Linear.")
            return False
        self.chat_log(update_id, "in", text)
        try:
            gate.create_comment(q["issue_id"], body)
        except (SystemExit, Exception) as exc:  # noqa: BLE001 — gate.py exits on a Linear error
            self.add_note(f"The user replied to your question on {ticket}, but posting it to Linear failed.")
            self.say(update_id, f"⚠️ Couldn't post to {ticket}: {exc}. Reply to the question again to retry.")
            return True
        q["answered_update_id"] = update_id
        q["answered_at"] = self.now().isoformat()
        self._write(path, q)
        self.add_note(f'The user answered your question on {ticket} ("{q["question"]}"), and the answer was '
                      f"posted as a comment on the ticket: {text}")
        self.say(update_id, f"Posted on {ticket} {q['url']}")
        return True

    # ----------------------------------------------------------- budget

    def _ledger_path(self) -> Path:
        return self.dir / "ledger" / f"{self._today()}.json"

    def ledger(self) -> dict:
        return self._read(self._ledger_path()) or {"spent_usd": 0.0, "calls": 0}

    def charge(self, usd: float) -> None:
        led = self.ledger()
        led["spent_usd"] = round(float(led.get("spent_usd") or 0) + usd, 6)
        led["calls"] = int(led.get("calls") or 0) + 1
        self._write(self._ledger_path(), led)

    def within_daily_budget(self, update_id: int) -> bool:
        spent, cap = float(self.ledger().get("spent_usd") or 0), daily_cap()
        if spent < cap:
            return True
        self.say(update_id, f"Daily chat budget reached (${spent:.2f} of ${cap:.2f}); resend after midnight ET.")
        return False

    # ----------------------------------------------------------- claude

    def cycle_state(self) -> tuple[dict, str]:
        """`--cycle current` and nothing else: no fallback that could quietly
        answer about a different cycle."""
        # not-claude-argv: begin
        argv = [sys.executable, str(HERE / "cycle_state.py"), "--cycle", "current"]
        # not-claude-argv: end
        try:
            res = self.run(argv, timeout=STATE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            raise StateError(f"timed out after {STATE_TIMEOUT_SECONDS}s") from None
        if res.returncode != 0:
            lines = (res.stderr or "").strip().splitlines()
            raise StateError((lines[-1] if lines else f"exit {res.returncode}")[-200:])
        try:
            doc = json.loads(res.stdout)
        except ValueError:
            raise StateError("its output was not JSON") from None
        return doc, state_sha(doc)

    def _typing(self, stop: threading.Event) -> None:
        while True:
            try:
                self.transport.send_chat_action(self.chat_id, "typing")
            except Exception:  # noqa: BLE001 — a missing "typing…" is cosmetic
                pass
            if stop.wait(TYPING_EVERY_SECONDS):
                return

    def call_claude(self, prompt: str, session_id: str, new_session: bool, appended: str):
        work = self.dir / "work"
        work.mkdir(parents=True, exist_ok=True)
        stop = threading.Event()
        typing = threading.Thread(target=self._typing, args=(stop,), daemon=True)
        typing.start()
        try:
            return self.run(claude_argv(session_id, new_session, appended), timeout=CLAUDE_TIMEOUT_SECONDS,
                            cwd=str(work), env=claude_env(), interruptible=True, input=prompt)
        finally:
            stop.set()
            typing.join(timeout=TYPING_EVERY_SECONDS + 1)

    def _session(self, path: Path, current_prompt_sha: str) -> dict | None:
        session = self._read(path)
        if session is None:
            return None
        # claude records the first system prompt of a session and reuses it on
        # resume, so an edited cycle.md or chat.md needs a new session to apply.
        if session.get("prompt_sha") != current_prompt_sha:
            log(f"{path.name}: the system prompt changed — starting a new session")
        elif int(session.get("turns") or 0) >= max_turns():
            log(f"{path.name}: {session.get('turns')} turns — starting a new session before it grows too large")
        else:
            return session
        path.unlink(missing_ok=True)
        return None

    def converse(self, update_id: int, text: str, started: float) -> None:
        """Step 4."""
        if self.stopping:
            raise _Requeue()
        try:
            doc, sha = self.cycle_state()
        except StateError as exc:
            log(f"update {update_id}: cycle state unavailable: {exc}")
            self.say(update_id, f"Couldn't read cycle state right now ({exc}); try again shortly.")
            return
        cycle = (doc.get("cycle") or {}).get("number")
        session_path = self.dir / "sessions" / f"cycle-{cycle}.json"
        appended = system_prompt()
        current_prompt_sha = prompt_sha(appended)
        session = self._session(session_path, current_prompt_sha)
        notes = self.notes()
        cap = message_cap()

        for _ in range(2):
            if self.stopping:
                raise _Requeue()
            new = session is None
            session_id = str(uuid.uuid4()) if new else session["session_id"]
            prompt = build_prompt(doc, sha, None if new else session.get("last_state_sha"), text, notes)
            try:
                res = self.call_claude(prompt, session_id, new, appended)
            except subprocess.TimeoutExpired:
                if self.stopping:
                    # We killed it ourselves, and the message goes back to the
                    # inbox. The next process pays for the call that answers it;
                    # charging here too would let a rollout loop, or a liveness
                    # kill, spend the day's budget on messages nobody answered.
                    raise _Requeue() from None
                # Cost unknown: charge the cap, or repeated timeouts would
                # bypass the daily budget entirely.
                self.charge(cap)
                log(f"update {update_id}: claude timed out after {CLAUDE_TIMEOUT_SECONDS}s")
                self.say(update_id, f"⏱ The PO took longer than {CLAUDE_TIMEOUT_SECONDS}s, so I stopped it. "
                                    "Nothing was written. Try again, or ask something narrower.")
                return
            if self.stopping:
                raise _Requeue()  # requeued, not answered: the retry pays for it
            blob = (res.stdout or "") + (res.stderr or "")
            if not new and res.returncode != 0 and "No conversation found" in blob:
                log(f"session {session_id} for cycle {cycle} is gone — starting a new one")
                session_path.unlink(missing_ok=True)
                session = None
                continue
            break

        data = parse_result(res.stdout)
        cost = float((data or {}).get("total_cost_usd") or 0)
        subtype = str((data or {}).get("subtype") or "")
        # The subtype when the result parsed; the CLI's own message when it did
        # not. Only on a failed call, so a reply that merely quotes it is a reply.
        if "budget" in subtype or (res.returncode != 0 and "Exceeded USD budget" in blob):
            self.charge(cost or cap)
            if not new:
                # A resumed session that hits the cap is usually one whose
                # history alone costs that much. Narrower questions won't help.
                session_path.unlink(missing_ok=True)
                log(f"session {session_id} hit the ${cap:.2f} cap on resume — rotated")
                msg = (f"💸 That hit the ${cap:.2f} per-message cap, so I started a fresh conversation "
                       "(the old one grew too large). Send that again. Nothing was written.")
            else:
                msg = f"💸 That hit the ${cap:.2f} per-message cap — ask something narrower. Nothing was written."
            self.say(update_id, msg, cost=cost or cap, session_id=session_id)
            return
        if data is None or res.returncode < 0:
            self.charge(cap)  # killed, or nothing readable: the cost is unknown
            log(f"update {update_id}: claude exit {res.returncode}, unreadable output: {blob.strip()[-500:]}")
            self.say(update_id, "⚠️ The PO call failed (logged). Nothing was written; try again.")
            return
        self.charge(cost)
        if res.returncode != 0 or data.get("is_error"):
            log(f"update {update_id}: claude exit {res.returncode}: {blob.strip()[-500:]}")
            self.say(update_id, "⚠️ The PO call failed (logged). Nothing was written; try again.")
            return
        out = data.get("structured_output")
        if not isinstance(out, dict):
            log(f"update {update_id}: no structured_output in the result: {blob.strip()[-500:]}")
            self.say(update_id, "⚠️ The PO returned an unreadable answer (logged). Nothing was written; try again.")
            return

        self._write(session_path, {
            "session_id": session_id,
            "created_at": (session or {}).get("created_at") or self.now().isoformat(),
            "turns": int((session or {}).get("turns") or 0) + 1,
            "last_state_sha": sha,
            "prompt_sha": current_prompt_sha,
        })
        self.clear_notes()
        reply = str(out.get("reply") or "").strip() or "(no reply)"
        proposal = None
        if isinstance(out.get("changes"), dict):
            extra, proposal = self.dry_run(out["changes"], cycle)
            reply = f"{reply}\n\n{extra}"
        try:
            sent = self.say(update_id, reply, cost=cost, session_id=session_id,
                            latency_s=round(self.clock() - started, 1))
        except BaseException:
            if proposal is not None:
                self._discard_pending()
                self.add_note("Your change set could not be shown to the user, so nothing is pending.")
            raise
        # The reply is delivered. Nothing after this may turn into "Something
        # broke" for a message that was answered.
        try:
            if proposal is not None:
                self.keep_pending(proposal, sent)
            if isinstance(out.get("ask"), dict):
                self.ask_from_model(out["ask"])
        except Exception as exc:  # noqa: BLE001
            log(f"update {update_id}: after the reply was sent: {exc!r}")

    # ------------------------------------------------------------ inbox

    def handle(self, path: Path) -> None:
        rec = json.loads(path.read_text())
        update_id = int(rec["update_id"])
        started = self.clock()
        text = (rec.get("text") or "").strip()
        # The listener allowlisted the sender already. Checked again because
        # this process acts on Linear, and the inbox is only a directory.
        if str(rec.get("chat_id")) != self.chat_id or (
            self.allowed_ids is not None and rec.get("from_id") not in self.allowed_ids
        ):
            log(f"update {update_id}: not from the allowlisted chat — dropped")
            return
        question = self.question_for(rec)
        if question is None:
            self.chat_log(update_id, "in", text)  # a question's answer is logged only once scanned
        if self._attempt(update_id) > MAX_ATTEMPTS:
            log(f"update {update_id}: failed {MAX_ATTEMPTS} times — dropped")
            self.say(update_id, "⚠️ I couldn't process your message, so I've dropped it. Nothing was written.")
            return
        if question is not None:
            if not self.answer_question(update_id, question, text):
                return
        elif self.settle_pending(update_id, rec, text):
            return
        if not self.within_daily_budget(update_id):
            return
        self.converse(update_id, text, started)

    def process(self, path: Path) -> None:
        """Handle one claimed message, then complete it. Every path completes it
        except a SIGTERM before or during the claude call, which puts it back."""
        try:
            update_id = int(path.stem)
        except ValueError:
            update_id = -1
        with self._heartbeat():
            try:
                self.handle(path)
            except _Requeue:
                self._forget_attempt(update_id)
                inbox.requeue(path)
                log(f"update {update_id}: stopping — requeued")
                return
            except Exception as exc:  # noqa: BLE001 — one bad message must not stop the chat
                log(f"update {update_id}: {exc!r}")
                try:
                    self.say(update_id, "⚠️ Something broke while handling that message (logged). Try again.")
                except Exception:  # noqa: BLE001
                    pass
            self._attempt_path(update_id).unlink(missing_ok=True)
            inbox.complete(path)

    def requeue_stale(self) -> None:
        """cur/ holds whatever the previous process claimed and never finished
        (a crash, an OOM kill). This is the one consumer, so all of it is ours."""
        cur = inbox.inbox_dir() / "cur"
        for path in sorted(cur.glob("*.json")) if cur.is_dir() else []:
            inbox.requeue(path)
            log(f"requeued {path.name}, left in progress by the previous process")

    # ------------------------------------------------------------- loop

    def moved(self) -> bool:
        if not (self.start_sha and self.ls_remote):
            return False
        try:
            remote = self.ls_remote()
        except Exception as exc:  # noqa: BLE001 — GitHub down is not a reason to stop
            log(f"self-update check failed, carrying on: {exc}")
            return False
        return bool(remote) and remote != self.start_sha

    def request_stop(self, *_: object) -> None:
        self.stopping = True
        child = self._child
        if child is not None and child.poll() is None:
            child.terminate()
        if self._waiting:
            raise _Stop()

    def _wait(self, seconds: float) -> None:
        self._waiting = True
        try:
            self.sleep(seconds)
        finally:
            self._waiting = False

    def run_loop(self) -> int:
        self.requeue_stale()
        next_check = self.clock() + self.update_every
        log(f"consuming {inbox.inbox_dir()} (dotfiles {(self.start_sha or 'sha unknown')[:7]})")
        try:
            while not self.stopping:
                self.beat()
                path = inbox.claim_next()
                if path is not None:
                    self.process(path)
                else:
                    self._wait(POLL_SECONDS)
                if not self.stopping and self.clock() >= next_check:
                    next_check = self.clock() + self.update_every
                    if self.moved():
                        log(f"dotfiles@main moved — exiting {EXIT_MOVED} so the container fetches and re-runs")
                        return EXIT_MOVED
        except _Stop:
            pass
        log("stopping")
        return 0


# ------------------------------------------------------------------ wiring


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30, check=True)
    return out.stdout.strip()


def _ticket(s: str) -> str:
    if not TICKET.fullmatch(s):
        raise argparse.ArgumentTypeError(f"not a ticket key like SB-123: {s!r}")
    return s


def cmd_consume(args: argparse.Namespace) -> int:
    gk = gate.gatekeeper_from_env()
    # HERE is <clone>/dot_claude/skills/po-agent/scripts.
    repo = Path(args.repo) if args.repo else HERE.parents[3]
    start_sha = None
    if (repo / ".git").exists():
        try:
            start_sha = _git(repo, "rev-parse", "HEAD")
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"cannot read HEAD of {repo}, self-update off: {exc}")
    else:
        log(f"{repo} is not a git clone — self-update off")

    def ls_remote() -> str:
        line = _git(repo, "ls-remote", "origin", f"refs/heads/{args.ref}")
        return line.split()[0] if line else ""

    chat = Chat(gk.transport, gk.chat_id, gk.allowed_ids, start_sha=start_sha,
                ls_remote=ls_remote if start_sha else None)
    signal.signal(signal.SIGTERM, chat.request_stop)
    signal.signal(signal.SIGINT, chat.request_stop)
    return chat.run_loop()


ASK_ELSEWHERE = ("po_chat: run inside the po-chat pod, where the question is recorded on the PVC that the chat "
                 "reads: kubectl exec deploy/po-chat --namespace cycle-runner -- python3 "
                 "/work/dotfiles/dot_claude/skills/po-agent/scripts/po_chat.py ask SB-N '…'")


def cmd_ask(args: argparse.Namespace) -> int:
    # Anywhere else, the question file lands in a state dir the chat never
    # reads, and the user's reply is silently never posted.
    if os.environ.get("PO_CHAT_POD") != "1":
        sys.exit(ASK_ELSEWHERE)
    # `kubectl exec` gets the container's env but not what env.sh exported at
    # start, so the Telegram files are read here the same way.
    secrets = Path(os.environ.get("CYCLE_RUNNER_SECRETS_DIR", "") or Path.home() / ".config/cycle-runner")
    for var, name in (("GATEKEEPER_TG_TOKEN", "telegram-token"), ("GATEKEEPER_TG_CHAT_ID", "telegram-chat-id")):
        if not os.environ.get(var) and (secrets / name).is_file():
            os.environ[var] = (secrets / name).read_text().strip()
    gk = gate.gatekeeper_from_env()
    issue = gate.issue_info(args.ticket)
    rec = Chat(gk.transport, gk.chat_id, gk.allowed_ids).post_question(args.ticket, issue, args.question)
    if rec is None:
        return 1
    print(json.dumps(rec, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="po_chat.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("consume", help="answer inbox messages as the PO, for ever")
    # not-claude-argv: begin
    p.add_argument("--repo", default=None, help="the dotfiles clone this runs from (default: detected)")
    p.add_argument("--ref", default="main")
    # not-claude-argv: end
    p.set_defaults(func=cmd_consume)
    p = sub.add_parser("ask", help="post a PO question; a Telegram reply to it lands on the ticket")
    p.add_argument("ticket", type=_ticket)
    p.add_argument("question")
    p.set_defaults(func=cmd_ask)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
