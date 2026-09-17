#!/usr/bin/env python3
"""po_chat.py — talk to the PO agent from Telegram (SB-1089).

    python3 po_chat.py consume                        # the po-chat Deployment: runs for ever
    python3 po_chat.py ask SB-12 "estimate it? 1/2/3" # post a PO question; the reply lands on SB-12

`consume` reads the gatekeeper inbox, never Telegram: listen.py is the one
getUpdates reader (SB-951), and a second one is a 409 for both. Messages are
handled one at a time, and each goes through four steps in this order:

  1. A proposal is pending. If the message is a plain yes, and the proposal
     has not expired and its file is unchanged, the bot applies exactly that
     file with `cycle_apply.py --confirm`. That flag appears in ONE function,
     apply_pending. Anything else, including "no", discards the proposal, and
     nothing is written.
  2. A reply to one of the PO's questions. The answer is scanned with the
     runner's own gitleaks `scan_log_clean`, then posted as a comment on that
     ticket. If the scan fails or can't run, nothing is posted.
  3. The daily budget. Over it, the bot says so and never calls `claude`.
  4. `claude -p`, one resumed session per cycle. The prompt carries the live
     cycle_state.py JSON; the system prompt is chat.md plus commands/cycle.md,
     both read at call time.

The model gets NO tools (`--tools ""`), so it can't write to Linear, or to
anything else. All it can do is return `changes`. This script dry-runs them,
shows the output and waits for the user's NEXT message. A change set is never
confirmed in the turn that proposed it.

State lives under $GATEKEEPER_STATE/po-chat/, which is the runner's PVC in k3s:
`sessions/cycle-<N>.json`, `pending.json` + `pending-changes.json`,
`questions/<telegram message_id>.json`, `ledger/<ET date>.json`,
`log/<ET date>.jsonl`, `notes.json` (what the PO must hear next turn),
`attempts/<update_id>`, `heartbeat` and `work/` (claude's cwd).

Exit codes: 0 stopped (SIGTERM), 75 dotfiles@main moved (the container's loop
fetches, resets and runs this again), anything else is a crash.
"""

from __future__ import annotations

import argparse
import hashlib
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
from datetime import datetime, timedelta, timezone
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
CLAUDE_TIMEOUT_SECONDS = 150
STATE_TIMEOUT_SECONDS = 90
APPLY_TIMEOUT_SECONDS = 120
SCAN_TIMEOUT_SECONDS = 60
PENDING_TTL = timedelta(minutes=30)
# A message that killed the process this many times is dropped, not retried
# for ever: a poison message must not wedge the chat.
MAX_ATTEMPTS = 2
LEDGER_TZ = "America/New_York"

# A plain yes, and nothing else. "yes but drop SB-4" is not consent to the list
# as shown, and neither is "yeah" or "sure": the user can say yes.
YES = re.compile(r"(yes|y|yes please|apply)[.!]?", re.IGNORECASE)
TICKET = re.compile(r"SB-\d+")

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "changes": {"type": ["object", "null"]},
        "ask": {
            "type": ["object", "null"],
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


def cycle_md() -> Path:
    """commands/cycle.md, beside skills/ in the source tree, the pod's clone
    and the deployed ~/.claude alike."""
    for p in (HERE.parents[2] / "commands" / "cycle.md", Path.home() / ".claude/commands/cycle.md"):
        if p.is_file():
            return p
    raise FileNotFoundError("commands/cycle.md not found beside the skills")


def system_prompt() -> str:
    """chat.md then cycle.md, read on every call. cycle.md holds the rules and
    chat.md only adapts them, so an edit to /cycle reaches the chat on its next
    message, with no copy to drift."""
    return (
        (HERE.parent / "chat.md").read_text()
        + "\n\n# The /cycle rules (commands/cycle.md)\n\n"
        + cycle_md().read_text()
    )


def claude_argv(prompt: str, session_id: str, new_session: bool) -> list[str]:
    """The one `claude` invocation. check-claude-cli-contract.sh reads the flags
    between the markers below and fails if the image's contract lacks one.

    SB-991: nothing interactive may leak in. --tools "" leaves no tools;
    --strict-mcp-config loads no MCP servers; --disable-slash-commands loads no
    skills; --setting-sources "" reads no settings file, so no plugin is
    enabled and no hook runs. The pod also points CLAUDE_CONFIG_DIR at a
    directory of its own, with no plugins installed in it."""
    # claude-cli-contract: begin
    argv = [
        "claude",
        "-p",
        prompt,
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
        system_prompt(),
    ]
    # claude-cli-contract: end
    return argv


def claude_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in CLAUDE_ENV_DROP}


def state_sha(doc: dict) -> str:
    """Identity of the cycle state, ignoring `generated_at`, which changes on
    every run. Otherwise the same numbers would never read as unchanged."""
    body = {k: v for k, v in doc.items() if k != "generated_at"}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


def build_prompt(doc: dict, sha: str, last_sha: str | None, text: str, notes: list[str]) -> str:
    if last_sha == sha:
        state = (f'<cycle_state sha="{sha}" unchanged="true">Unchanged since your last message; '
                 "use the cycle_state you were given then.</cycle_state>")
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


def structured(data: dict) -> dict:
    out = data.get("structured_output")
    if not isinstance(out, dict):
        try:
            out = json.loads(data.get("result") or "")
        except (ValueError, TypeError):
            out = None
    if not isinstance(out, dict):
        out = {"reply": str(data.get("result") or ""), "changes": None, "ask": None}
    return out


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class StateError(Exception):
    """cycle_state.py could not produce a document."""


class _Stop(Exception):
    """Raised out of the idle wait by SIGTERM."""


class _Requeue(Exception):
    """SIGTERM arrived during the claude call: the message goes back to new/."""


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
        return send_text(self.transport, self.chat_id, text)

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
             interruptible: bool = False) -> subprocess.CompletedProcess:
        """subprocess.run with stdin closed. Only an interruptible child (claude)
        is terminated by SIGTERM; cycle_apply mid-write is left to finish."""
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        if interruptible:
            self._child = proc
        try:
            out, err = proc.communicate(timeout=timeout)
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

    def propose(self, changes: dict, cycle: int | None) -> str:
        """Write the change set and dry-run it. It stays pending only if the dry
        run accepts it. Returns the text that goes under the PO's reply. Never
        writes to Linear: that needs the user's next message (settle_pending)."""
        replaced = self.pending_path.exists()
        self._discard_pending()
        body = json.dumps(changes, indent=2)
        gate._write_atomic(self.changes_path, body)
        argv = [sys.executable, str(HERE / "cycle_apply.py"), "--changes", str(self.changes_path)]
        try:
            res = self.run(argv, timeout=APPLY_TIMEOUT_SECONDS)
            out, rc = ((res.stdout or "") + (res.stderr or "")).strip(), res.returncode
        except subprocess.TimeoutExpired:
            out, rc = f"the dry run timed out after {APPLY_TIMEOUT_SECONDS}s", 1
        if rc != 0:
            self._discard_pending()
            self.add_note(f"The dry run refused your change set, so nothing is pending and nothing was written:\n{out}")
            return f"The dry run refused that change set. Nothing is pending and nothing was written.\n\n{out}"
        now = self.now()
        self._write(self.pending_path, {
            "changes": changes,
            "sha256": _sha256(body.encode()),
            "proposed_at": now.isoformat(),
            "expires_at": (now + PENDING_TTL).isoformat(),
            "cycle": cycle,
        })
        lead = "This replaces the earlier proposal, which was not applied.\n\n" if replaced else ""
        return f"{lead}Dry run:\n\n{out}\n\nReply yes to write exactly this, or no."

    def apply_pending(self, changes_file: Path) -> subprocess.CompletedProcess:
        """THE ONLY PLACE `--confirm` IS PASSED. It is reached from exactly one
        path: a plain yes, as the next message after a dry run, to a proposal
        that has not expired and whose file still hashes to what was shown."""
        argv = [sys.executable, str(HERE / "cycle_apply.py"), "--changes", str(changes_file), "--confirm"]
        return self.run(argv, timeout=APPLY_TIMEOUT_SECONDS)

    def settle_pending(self, update_id: int, text: str) -> bool:
        """Step 1. True when the message was a yes that was acted on (nothing
        more to do). Any other message discards the proposal and falls through."""
        pending = self._read(self.pending_path)
        if pending is None:
            self.changes_path.unlink(missing_ok=True)
            return False
        try:
            expired = self.now() >= datetime.fromisoformat(pending["expires_at"])
        except (KeyError, TypeError, ValueError):
            expired = True
        try:
            intact = _sha256(self.changes_path.read_bytes()) == pending.get("sha256")
        except OSError:
            intact = False
        said_yes = bool(YES.fullmatch(text.strip()))

        if said_yes and not expired and intact:
            res = self.apply_pending(self.changes_path)
            self._discard_pending()
            out = ((res.stdout or "") + (res.stderr or "")).strip()
            # Notes before the reply: the write has happened, and a failed send
            # must not also hide that from the PO.
            if res.returncode == 0:
                self.add_note(f"The user said yes, and the pending change set for cycle {pending.get('cycle')} "
                              f"was applied. cycle_apply.py output:\n{out}")
                self.say(update_id, f"✅ Written.\n\n{out}")
            else:
                self.add_note(f"The user said yes, but cycle_apply.py failed part way through the write:\n{out}")
                self.say(update_id, f"❌ The write stopped part way:\n\n{out}\n\n"
                                    "Ask me to propose it again. Fields already written are skipped.")
            return True

        self._discard_pending()
        if not said_yes:
            reason = "the user's next message was not a yes"
            self.say(update_id, "Nothing written.")
        else:
            reason = (f"it was older than {int(PENDING_TTL.total_seconds() // 60)} minutes" if expired
                      else "its file changed after the dry run was shown")
            self.say(update_id, f"Nothing written: that proposal was discarded because {reason}.")
        self.add_note(f"The pending change set was discarded and nothing was written: {reason}.")
        return False

    # -------------------------------------------------------- questions

    @property
    def questions_dir(self) -> Path:
        return self.dir / "questions"

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
        except TelegramError as exc:
            log(f"could not send the PO question on {ticket}: {exc}")

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

    def answer_question(self, update_id: int, rec: dict, text: str) -> None:
        """Step 2. Only a Telegram reply to a question message posts anything."""
        reply_to = rec.get("reply_to_message_id")
        if reply_to is None:
            return
        path = self.questions_dir / f"{int(reply_to)}.json"
        q = self._read(path)
        if q is None:
            return
        ticket = q["ticket"]
        if q.get("answered_update_id") == update_id:
            return  # this very message, retried after a crash: already posted
        if q.get("answered_update_id") is not None:
            self.say(update_id, f"{ticket}'s question already has an answer on the ticket, so this reply "
                                "was not posted.")
            return
        body = f"PO question (Telegram): {q['question']}\n\nAnswer: {text}"
        rc = self.scan(body)
        if rc != 0:
            why = "the secret scan flagged something in it" if rc == 1 else "the secret scan could not run"
            self.say(update_id, f"🔒 Not posted to {ticket}: {why}. Nothing went to Linear.")
            self.add_note(f"The user replied to your question on {ticket}, but it was NOT posted to Linear: {why}.")
            return
        try:
            gate.create_comment(q["issue_id"], body)
        except (SystemExit, Exception) as exc:  # noqa: BLE001 — gate.py exits on a Linear error
            self.say(update_id, f"⚠️ Couldn't post to {ticket}: {exc}. Reply to the question again to retry.")
            self.add_note(f"The user replied to your question on {ticket}, but posting it to Linear failed.")
            return
        q["answered_update_id"] = update_id
        q["answered_at"] = self.now().isoformat()
        self._write(path, q)
        self.add_note(f'The user answered your question on {ticket} ("{q["question"]}"), and the answer was '
                      f"posted as a comment on the ticket: {text}")
        self.say(update_id, f"Posted on {ticket} {q['url']}")

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
        errors = []
        for mode in ("current", "plan-target"):
            argv = [sys.executable, str(HERE / "cycle_state.py"), "--cycle", mode]
            try:
                res = self.run(argv, timeout=STATE_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                errors.append(f"--cycle {mode}: timed out")
                continue
            if res.returncode == 0:
                try:
                    doc = json.loads(res.stdout)
                except ValueError:
                    errors.append(f"--cycle {mode}: output was not JSON")
                    continue
                return doc, state_sha(doc)
            errors.append(f"--cycle {mode}: {(res.stderr or '').strip()[-300:]}")
        raise StateError("; ".join(errors))

    def _typing(self, stop: threading.Event) -> None:
        while True:
            try:
                self.transport.send_chat_action(self.chat_id, "typing")
            except Exception:  # noqa: BLE001 — a missing "typing…" is cosmetic
                pass
            self.beat()
            if stop.wait(TYPING_EVERY_SECONDS):
                return

    def call_claude(self, prompt: str, session_id: str, new_session: bool) -> subprocess.CompletedProcess:
        work = self.dir / "work"
        work.mkdir(parents=True, exist_ok=True)
        stop = threading.Event()
        typing = threading.Thread(target=self._typing, args=(stop,), daemon=True)
        typing.start()
        try:
            return self.run(claude_argv(prompt, session_id, new_session), timeout=CLAUDE_TIMEOUT_SECONDS,
                            cwd=str(work), env=claude_env(), interruptible=True)
        finally:
            stop.set()
            typing.join(timeout=TYPING_EVERY_SECONDS + 1)

    def converse(self, update_id: int, text: str, started: float) -> None:
        """Step 4."""
        try:
            doc, sha = self.cycle_state()
        except StateError as exc:
            log(f"update {update_id}: cycle state unavailable: {exc}")
            self.say(update_id, "⚠️ I couldn't read the cycle from Linear, so I can't answer yet. "
                                "Try again in a minute.")
            return
        cycle = (doc.get("cycle") or {}).get("number")
        session_path = self.dir / "sessions" / f"cycle-{cycle}.json"
        session = self._read(session_path)
        notes = self.notes()

        for _ in range(2):
            new = session is None
            session_id = str(uuid.uuid4()) if new else session["session_id"]
            prompt = build_prompt(doc, sha, None if new else session.get("last_state_sha"), text, notes)
            try:
                res = self.call_claude(prompt, session_id, new)
            except subprocess.TimeoutExpired:
                if self.stopping:
                    raise _Requeue() from None
                log(f"update {update_id}: claude timed out after {CLAUDE_TIMEOUT_SECONDS}s")
                self.say(update_id, f"⏱ The PO took longer than {CLAUDE_TIMEOUT_SECONDS}s, so I stopped it. "
                                    "Nothing was written. Try again, or ask something narrower.")
                return
            if self.stopping:
                raise _Requeue()
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
            cap = message_cap()
            self.charge(cost or cap)
            if (data or {}).get("session_id"):
                self._save_session(session_path, session, session_id, last_state_sha=None)
            self.say(update_id, f"💸 That hit the ${cap:.2f} per-message cap — ask something narrower. "
                                "Nothing was written.", cost=cost or cap, session_id=session_id)
            return
        self.charge(cost)
        if data is None or res.returncode != 0 or data.get("is_error"):
            log(f"update {update_id}: claude exit {res.returncode}: {blob.strip()[-500:]}")
            self.say(update_id, "⚠️ The PO call failed (logged). Nothing was written; try again.")
            return

        out = structured(data)
        self._save_session(session_path, session, session_id, last_state_sha=sha)
        self.clear_notes()
        reply = str(out.get("reply") or "").strip() or "(no reply)"
        changes = out.get("changes")
        if isinstance(changes, dict):
            reply = f"{reply}\n\n{self.propose(changes, cycle)}"
        elif changes is not None:
            log(f"update {update_id}: ignored `changes` that is not an object")
        self.say(update_id, reply, cost=cost, session_id=session_id,
                 latency_s=round(self.clock() - started, 1))
        if isinstance(out.get("ask"), dict):
            self.ask_from_model(out["ask"])

    def _save_session(self, path: Path, session: dict | None, session_id: str, last_state_sha: str | None) -> None:
        rec = dict(session or {"session_id": session_id, "created_at": self.now().isoformat(), "turns": 0,
                               "last_state_sha": None})
        rec["turns"] = int(rec.get("turns") or 0) + 1
        if last_state_sha is not None:
            rec["last_state_sha"] = last_state_sha
        self._write(path, rec)

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
        self.chat_log(update_id, "in", text)
        if self._attempt(update_id) > MAX_ATTEMPTS:
            log(f"update {update_id}: failed {MAX_ATTEMPTS} times — dropped")
            self.say(update_id, "⚠️ I couldn't process your message, so I've dropped it. Nothing was written.")
            return
        if self.settle_pending(update_id, text):
            return
        self.answer_question(update_id, rec, text)
        if not self.within_daily_budget(update_id):
            return
        self.converse(update_id, text, started)

    def process(self, path: Path) -> None:
        """Handle one claimed message, then complete it. Every path completes it
        except a SIGTERM during the claude call, which puts it back."""
        try:
            update_id = int(path.stem)
        except ValueError:
            update_id = -1
        try:
            self.handle(path)
        except _Requeue:
            self._forget_attempt(update_id)
            inbox.requeue(path)
            log(f"update {update_id}: stopping mid-call — requeued")
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


def cmd_ask(args: argparse.Namespace) -> int:
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
    p.add_argument("--repo", default=None, help="the dotfiles clone this runs from (default: detected)")
    p.add_argument("--ref", default="main")
    p.set_defaults(func=cmd_consume)
    p = sub.add_parser("ask", help="post a PO question; a Telegram reply to it lands on the ticket")
    p.add_argument("ticket", type=_ticket)
    p.add_argument("question")
    p.set_defaults(func=cmd_ask)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
