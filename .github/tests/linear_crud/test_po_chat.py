"""po_chat.py in po-agent/scripts — the PO chat over Telegram (SB-1089), offline.

The contract the chat leans on: nothing reaches Linear without a plain yes,
sent AFTER a dry run the user actually received, and about that dry run. "no",
a reply to something else, a replay of an older message, or silence writes
nothing. A question's answer is scanned before it becomes a comment, and a
flagged one goes nowhere at all. Both budget caps answer the user instead of
dropping the message. And the model runs with no tools and no interactive
config.

Every subprocess goes through a FakeRunner (claude, cycle_state.py,
cycle_apply.py, the gitleaks scan), Linear is the gatekeeper's FakeLinear
behind `gate.gql`, and Telegram is its FakeTransport. GATEKEEPER_STATE is a
fresh tempdir per test.
"""

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from _load import SKILLS_DIR, load_module

os.environ.setdefault("GATEKEEPER_QUIET_START", "0")
os.environ.setdefault("GATEKEEPER_QUIET_END", "0")

pc = load_module("po_chat", "po-agent", "po_chat.py")
sys.path.insert(0, str(SKILLS_DIR / "gatekeeper" / "tests"))
from fakes import FakeLinear, FakeTransport  # noqa: E402

gate = pc.gate
inbox = pc.inbox
NOW = datetime(2026, 9, 17, 16, 0, tzinfo=timezone.utc)


def _state(number=9, days_left=4, stalled_days=5):
    return {"schema": "po-agent.cycle_state/1", "generated_at": NOW.isoformat(),
            "cycle": {"number": number, "days_left": days_left, "days_elapsed": 10},
            "issues": [{"identifier": "SB-1", "days_since_activity": stalled_days}],
            "waiting_on_human": [{"identifier": "SB-2", "age_days": 3}],
            "at_risk": {"not_started": [],
                        "stalled": [{"identifier": "SB-1", "days_since_activity": stalled_days}]}}


def _done(stdout="", rc=0, stderr=""):
    return subprocess.CompletedProcess([], rc, stdout, stderr)


def _claude_json(reply="On track.", changes=None, ask=None, cost=0.05, session_id="s", structured=True, **extra):
    body = {"type": "result", "subtype": "success", "is_error": False, "result": "",
            "session_id": session_id, "total_cost_usd": cost, **extra}
    if structured:
        body["structured_output"] = {"reply": reply, "changes": changes, "ask": ask}
    return json.dumps(body)


CHANGES = {"issues": [{"identifier": "SB-1", "estimate": 3}]}


class FakeRunner:
    """Answers each subprocess po_chat.py starts, by what it is, and records the
    order they ran in (`events` is shared with FakeLinear's comment writes)."""

    def __init__(self, events):
        self.events = events
        self.calls = []
        self.state = [_state()]
        self.state_result = None
        self.claude = []
        self.dry_run = _done("  SB-1     estimate: 2 -> 3\n\ndry run — nothing written; pass --confirm to write")
        self.confirm = _done("  updated SB-1\n\n1 issue(s) updated")
        self.scan_rc = 0
        self.before = None

    def kind(self, argv):
        if argv[0] == "claude":
            return "claude"
        if argv[0] == "bash" and "scan_log_clean" in argv[2]:
            return "scan"
        if argv[1].endswith("cycle_state.py"):
            return "state"
        if argv[1].endswith("cycle_apply.py"):
            return "confirm" if "--confirm" in argv else "dry_run"
        raise AssertionError(f"unexpected subprocess: {argv!r}")

    def __call__(self, argv, *, timeout, cwd=None, env=None, interruptible=False, input=None):
        kind = self.kind(argv)
        self.calls.append({"kind": kind, "argv": list(argv), "env": env, "input": input})
        self.events.append(kind)
        if self.before is not None:
            self.before(kind)
        if kind == "state":
            if self.state_result is not None:
                return self.state_result
            doc = self.state[0] if len(self.state) == 1 else self.state.pop(0)
            return _done(json.dumps(doc))
        if kind == "claude":
            nxt = self.claude.pop(0) if self.claude else _done(_claude_json())
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        if kind == "scan":
            return _done(rc=self.scan_rc)
        if kind == "dry_run":
            return self.dry_run
        if isinstance(self.confirm, Exception):
            raise self.confirm
        return self.confirm

    def of(self, kind):
        return [c["argv"] for c in self.calls if c["kind"] == kind]

    def prompts(self):
        return [c["input"] for c in self.calls if c["kind"] == "claude"]


class ChatTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ, {"GATEKEEPER_STATE": self._tmp.name, "LINEAR_ASSIGNEE_ID": "user-1",
                                           "XDG_CACHE_HOME": self._tmp.name})
        env.start()
        self.addCleanup(env.stop)
        for knob in ("PO_CHAT_MODEL", "PO_CHAT_MSG_USD", "PO_CHAT_DAILY_USD", "PO_CHAT_MAX_TURNS", "PO_CHAT_POD"):
            os.environ.pop(knob, None)

        self.events = []
        self.linear = FakeLinear(ticket="SB-1", title="Do the thing")
        real_create = self.linear._comment_create

        def create(variables):
            self.events.append("comment")
            return real_create(variables)

        self.linear._comment_create = create
        p = mock.patch.object(gate, "gql", self.linear)
        p.start()
        self.addCleanup(p.stop)

        self.transport = FakeTransport()
        self.runner = FakeRunner(self.events)
        self.now = NOW
        self.chat = pc.Chat(self.transport, "42", {42}, run=self.runner, now=lambda: self.now)
        self.next_update = 600

    def message(self, text, reply_to=None, chat_id=42, from_id=42, date=None):
        """Record one message in the inbox, claim it and process it, as the loop
        does. Each message is a second later than the last, so its Telegram
        `date` orders it against anything the bot has sent."""
        self.now += timedelta(seconds=1)
        self.next_update += 1
        msg = {"message_id": self.next_update * 10, "from": {"id": from_id},
               "chat": {"id": chat_id, "type": "private"},
               "date": int(self.now.timestamp()) if date is None else date, "text": text}
        if reply_to is not None:
            msg["reply_to_message"] = {"message_id": reply_to}
        inbox.record(msg, self.next_update)
        path = inbox.claim_next()
        self.chat.process(path)
        return self.next_update

    def redeliver(self, update_id):
        """Process an already-completed message again (a crash after the post)."""
        done = inbox.inbox_dir() / "done" / f"{update_id}.json"
        cur = inbox.inbox_dir() / "cur" / f"{update_id}.json"
        os.rename(done, cur)
        self.chat.process(cur)

    @property
    def texts(self):
        return self.transport.texts

    def pending(self, key=None):
        path = pc.chat_dir() / "pending.json"
        if key is None:
            return path
        return json.loads(path.read_text())[key]

    def ledger(self):
        day = self.now.astimezone(pc.ZoneInfo(pc.LEDGER_TZ)).date().isoformat()
        return json.loads((pc.chat_dir() / "ledger" / f"{day}.json").read_text())

    def propose(self):
        self.runner.claude.append(_done(_claude_json("Estimate SB-1 at 3?", changes=CHANGES)))
        self.message("estimate SB-1 at 3")
        self.assertTrue(self.pending().exists(), "the proposal was not kept pending")


class YesMatcher(unittest.TestCase):
    def test_only_a_plain_yes_counts(self):
        table = {"yes": True, "Yes.": True, "y": True, "Y!": True, "apply": True, "yes please": True,
                 "  YES  ": True, "yes but drop SB-4": False, "yes but…": False, "yeah": False, "no": False,
                 "sure": False, "ok": False, "yes yes": False, "": False}
        for text, want in table.items():
            with self.subTest(text=text):
                self.assertEqual(bool(pc.YES.fullmatch(text.strip())), want)


class NoWriteWithoutYes(ChatTestCase):
    def test_a_proposal_is_dry_run_and_never_confirmed_in_the_same_turn(self):
        self.propose()
        self.assertEqual(len(self.runner.of("dry_run")), 1)
        self.assertEqual(self.runner.of("confirm"), [])
        self.assertIn("Reply yes to write exactly this, or no.", self.texts[-1])
        self.assertIn("estimate: 2 -> 3", self.texts[-1], "the dry run was not shown verbatim")
        self.assertEqual(self.pending("changes"), CHANGES)
        # Bound to the message that showed it, and live only from then on.
        self.assertEqual(self.pending("message_id"), len(self.transport.sent))
        self.assertEqual(self.pending("expires_at") - self.pending("sent_at"), pc.PENDING_TTL_SECONDS)

    def test_yes_applies_exactly_the_pending_file_once_without_calling_the_po(self):
        self.propose()
        self.message("Yes.")
        confirms = self.runner.of("confirm")
        self.assertEqual(len(confirms), 1)
        self.assertEqual(confirms[0][confirms[0].index("--changes") + 1], str(pc.chat_dir() / "pending-changes.json"))
        self.assertEqual(len(self.runner.of("claude")), 1, "a bare yes should not cost a PO call")
        self.assertFalse(self.pending().exists())
        self.assertIn("✅ Written.", self.texts[-1])

        # ...and the PO hears about it on the next turn, once.
        self.message("thanks")
        self.assertIn("<system_note>The user said yes", self.runner.prompts()[-1])
        self.assertEqual(self.chat.notes(), [])

    def test_a_yes_replying_to_the_dry_run_message_confirms(self):
        self.propose()
        self.message("yes", reply_to=self.pending("message_id"))
        self.assertEqual(len(self.runner.of("confirm")), 1)

    # NEGATIVE: consent is about THAT proposal. A yes aimed at some other
    # message is not consent to a change list the user may never have read.
    def test_a_yes_replying_to_a_different_message_writes_nothing(self):
        self.propose()
        self.message("yes", reply_to=4242)
        self.assertEqual(self.runner.of("confirm"), [])
        self.assertFalse(self.pending().exists())
        self.assertTrue(any("a reply to a different message" in t for t in self.texts))

    # NEGATIVE: the message that CAUSED the proposal, redelivered. It was sent
    # before the dry run existed, so it can never be consent to it — the replay
    # must not confirm, and must not discard either.
    def test_replaying_the_proposing_message_neither_confirms_nor_discards(self):
        self.runner.claude.append(_done(_claude_json("Estimate SB-1 at 3?", changes=CHANGES)))
        uid = self.message("yes")
        self.assertTrue(self.pending().exists())
        sent_at = self.pending("sent_at")

        self.runner.claude.append(_done(_claude_json("Estimate SB-1 at 3?", changes=CHANGES)))
        self.redeliver(uid)
        self.assertEqual(self.runner.of("confirm"), [])
        self.assertTrue(self.pending().exists())
        self.assertGreaterEqual(self.pending("sent_at"), sent_at)

    def test_no_writes_nothing_and_the_po_hears_it(self):
        self.propose()
        self.message("no")
        self.assertEqual(self.runner.of("confirm"), [])
        self.assertFalse(self.pending().exists())
        self.assertIn("Nothing written.", self.texts)
        self.assertIn("not a yes", self.runner.prompts()[-1])

    # The note is written before the reply goes out, so a Telegram failure
    # cannot leave the PO believing a discarded proposal is still pending.
    def test_the_discard_is_recorded_even_if_the_reply_cannot_be_sent(self):
        self.propose()
        self.transport.fail_send = pc.TelegramError("Telegram is down")
        self.message("no")
        self.assertFalse(self.pending().exists())
        self.assertTrue(any("discarded and nothing was written" in n for n in self.chat.notes()))

    def test_anything_short_of_a_plain_yes_writes_nothing(self):
        for text in ("yes but drop SB-4", "yeah", "what would that change?"):
            with self.subTest(text=text):
                self.propose()
                self.message(text)
                self.assertEqual(self.runner.of("confirm"), [])
                self.assertFalse(self.pending().exists())

    def test_silence_expires_it_by_the_senders_clock(self):
        self.propose()
        self.now += timedelta(minutes=31)
        self.message("yes")
        self.assertEqual(self.runner.of("confirm"), [])
        self.assertTrue(any("older than 30 minutes" in t for t in self.texts))

    def test_a_changed_file_is_not_applied(self):
        self.propose()
        (pc.chat_dir() / "pending-changes.json").write_text(json.dumps({"issues": [{"identifier": "SB-1",
                                                                                   "cancel": True}]}))
        self.message("yes")
        self.assertEqual(self.runner.of("confirm"), [])
        self.assertTrue(any("file changed after the dry run" in t for t in self.texts))

    def test_a_refused_dry_run_keeps_nothing_pending(self):
        self.runner.dry_run = _done(stderr="REFUSING: SB-1 not found in Linear — nothing written", rc=1)
        self.runner.claude.append(_done(_claude_json("Dropping SB-1.", changes=CHANGES)))
        self.message("drop SB-1")
        self.assertFalse(self.pending().exists())
        self.assertFalse((pc.chat_dir() / "pending-changes.json").exists())
        self.assertIn("REFUSING: SB-1 not found", self.texts[-1])
        self.message("yes")
        self.assertEqual(self.runner.of("confirm"), [])

    # NEGATIVE: the dry run was produced but never delivered. Nothing is
    # pending, because the user has not seen what they would be agreeing to.
    def test_a_dry_run_that_could_not_be_sent_leaves_nothing_pending(self):
        self.runner.claude.append(_done(_claude_json("Estimate SB-1 at 3?", changes=CHANGES)))
        self.transport.fail_send = pc.TelegramError("Telegram is down")
        self.message("estimate SB-1 at 3")
        self.transport.fail_send = None
        self.assertFalse(self.pending().exists())
        self.message("yes")
        self.assertEqual(self.runner.of("confirm"), [])

    # A --confirm that started and did not finish may have written some of the
    # batch. Saying "nothing written" would be a lie.
    def test_an_interrupted_write_says_so_rather_than_claiming_nothing_happened(self):
        self.propose()
        self.runner.confirm = subprocess.TimeoutExpired("cycle_apply.py", 120)
        self.message("yes")
        self.assertIn("may be partly applied", self.texts[-1])
        self.assertNotIn("Nothing written.", self.texts)
        self.assertFalse(self.pending().exists())
        self.assertTrue(any("may be partly applied" in n for n in self.chat.notes()))

    def test_confirm_is_passed_in_exactly_one_function(self):
        tree = ast.parse(Path(pc.__file__).read_text())
        owners = set()
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(fn):
                    if isinstance(node, ast.Constant) and node.value == "--confirm":
                        owners.add(fn.name)
        literal_count = sum(1 for node in ast.walk(tree)
                            if isinstance(node, ast.Constant) and node.value == "--confirm")
        self.assertEqual(owners, {"apply_pending"})
        self.assertEqual(literal_count, 1)


class Questions(ChatTestCase):
    def ask(self):
        self.runner.claude.append(_done(_claude_json("SB-1 has no estimate.",
                                                     ask={"ticket": "SB-1", "question": "Estimate SB-1? 1/2/3/5/8"})))
        self.message("what's not ready?")
        files = list((pc.chat_dir() / "questions").glob("*.json"))
        self.assertEqual(len(files), 1, "the question was not recorded")
        return json.loads(files[0].read_text())

    def test_the_po_can_ask_and_the_question_is_its_own_message(self):
        q = self.ask()
        self.assertEqual(q["ticket"], "SB-1")
        self.assertIsNone(q["answered_update_id"])
        text = self.texts[q["message_id"] - 1]
        self.assertTrue(text.startswith("❓ SB-1 — Do the thing\nEstimate SB-1? 1/2/3/5/8\n"))
        self.assertIn("Reply to this message to answer", text)
        self.assertIn("https://linear.app/silverbeer/issue/SB-1", text)

    def test_a_reply_is_scanned_then_posted_on_the_ticket_with_the_echo_marker(self):
        q = self.ask()
        self.events.clear()
        self.message("3, it's small", reply_to=q["message_id"])
        self.assertEqual(self.events[:2], ["scan", "comment"], "the answer must be scanned BEFORE it is posted")
        self.assertEqual(len(self.linear.comments), 1)
        # The marker keeps gate.py's Linear reader from forwarding our own
        # comment back to Telegram as discussion on an open gate.
        self.assertEqual(self.linear.comments[0]["body"],
                         f"{gate.ECHO_MARKER}\nPO question (Telegram): Estimate SB-1? 1/2/3/5/8"
                         "\n\nAnswer: 3, it's small")
        self.assertIn("Posted on SB-1 https://linear.app/silverbeer/issue/SB-1", self.texts)
        self.assertIn("answer was posted as a comment", self.runner.prompts()[-1])

    # A flagged answer goes NOWHERE: not to Linear, not to claude, and not into
    # the chat log on the PVC.
    def test_a_scan_that_flags_or_cannot_run_posts_nothing_and_forwards_nothing(self):
        for rc in (1, 2):
            with self.subTest(rc=rc):
                q = self.ask()
                self.runner.scan_rc = rc
                before = len(self.runner.of("claude"))
                # Assembled at runtime: no credential-shaped literal may exist
                # in this repo, which test_meta.sh enforces.
                secret = "AKIA" + "IOSFODNN7EXAMPL3"
                self.message(f"here is the key {secret}", reply_to=q["message_id"])
                self.assertEqual(self.linear.comments, [])
                self.assertEqual(len(self.runner.of("claude")), before, "a withheld answer reached claude")
                self.assertTrue(any(t.startswith("🔒 Not posted to SB-1") for t in self.texts))
                logged = (pc.chat_dir() / "log").glob("*.jsonl")
                body = "".join(p.read_text() for p in logged)
                self.assertNotIn(secret, body)
                self.assertIn("answer withheld", body)
                shutil.rmtree(pc.chat_dir() / "questions")
                self.runner.scan_rc = 0

    def test_the_same_answer_is_posted_once(self):
        q = self.ask()
        uid = self.message("3", reply_to=q["message_id"])
        self.redeliver(uid)
        self.assertEqual(len(self.linear.comments), 1)

    def test_a_message_that_is_not_a_reply_never_comments(self):
        self.ask()
        self.message("3")
        self.assertEqual(self.runner.of("scan"), [])
        self.assertEqual(self.linear.comments, [])

    # A question reply is not an answer to a proposal, either way.
    def test_a_reply_to_a_question_neither_confirms_nor_discards_a_proposal(self):
        q = self.ask()
        self.runner.claude.append(_done(_claude_json("Estimate SB-1 at 3?", changes=CHANGES)))
        self.message("what should I do?")
        self.assertTrue(self.pending().exists())
        self.message("yes", reply_to=q["message_id"])
        self.assertEqual(self.runner.of("confirm"), [], "a question reply confirmed a proposal")
        self.assertTrue(self.pending().exists(), "a question reply discarded a proposal")
        self.assertEqual(len(self.linear.comments), 1)

    def test_a_question_on_something_that_is_not_a_ticket_is_dropped(self):
        self.runner.claude.append(_done(_claude_json("hm", ask={"ticket": "the backlog", "question": "why?"})))
        self.message("hi")
        self.assertFalse((pc.chat_dir() / "questions").exists())
        self.assertEqual(len(self.texts), 1)

    # A send failure after the reply landed is logged, not reported as "something
    # broke" — the user's answer did arrive.
    def test_a_question_that_cannot_be_sent_does_not_fail_the_message(self):
        self.runner.claude.append(_done(_claude_json("SB-1 has no estimate.",
                                                     ask={"ticket": "SB-1", "question": "Estimate SB-1?"})))
        real_send = self.transport.send_message
        calls = []

        def send(*a, **kw):
            calls.append(a)
            if len(calls) > 1:
                raise OSError("connection reset")
            return real_send(*a, **kw)

        self.transport.send_message = send
        self.message("what's not ready?")
        self.assertEqual(self.texts, ["SB-1 has no estimate."])
        self.assertFalse(any("Something broke" in t for t in self.texts))

    def test_the_ask_cli_refuses_outside_the_pod(self):
        gk = gate.Gatekeeper(self.transport, "42", {42})
        with mock.patch.object(gate, "gatekeeper_from_env", return_value=gk):
            with self.assertRaises(SystemExit) as ctx:
                pc.main(["ask", "SB-1", "Drop SB-1 from cycle 9?"])
        self.assertIn("run inside the po-chat pod", str(ctx.exception.code))
        self.assertIn("kubectl exec deploy/po-chat", str(ctx.exception.code))
        self.assertEqual(self.texts, [])

    def test_the_ask_cli_posts_and_records_a_question_in_the_pod(self):
        gk = gate.Gatekeeper(self.transport, "42", {42})
        os.environ["PO_CHAT_POD"] = "1"
        with mock.patch.object(gate, "gatekeeper_from_env", return_value=gk), mock.patch("sys.stdout"):
            rc = pc.main(["ask", "SB-1", "Drop SB-1 from cycle 9?"])
        self.assertEqual(rc, 0)
        q = json.loads((pc.chat_dir() / "questions" / "1.json").read_text())
        self.assertEqual((q["ticket"], q["question"], q["issue_id"]),
                         ("SB-1", "Drop SB-1 from cycle 9?", "issue-uuid-1"))

    def test_the_real_scan_sources_run_sh_without_running_it(self):
        """Through bash for real, with a stub gitleaks: proves the invocation
        reaches scan_log_clean and that its exit code comes back, and that
        sourcing run.sh stops at its guard instead of starting a tick."""
        root = Path(self._tmp.name)
        stub, home, secrets = root / "bin", root / "home", root / "no-secrets"
        for d in (stub, home, secrets):
            d.mkdir()
        # Hermetic even if the guard breaks: an empty HOME and secrets dir, no
        # tokens in the environment, and claude/gh stubs that only leave a
        # marker. A tick that started would die on the missing token before
        # any network call, and the run log directory would give it away.
        marker = root / "tick-ran"
        for name in ("claude", "gh"):
            (stub / name).write_text(f"#!/usr/bin/env bash\ntouch {marker}\nexit 97\n")
            (stub / name).chmod(0o755)
        env = {"PATH": f"{stub}:{os.environ['PATH']}", "HOME": str(home), "CYCLE_RUNNER_SECRETS_DIR": str(secrets)}
        chat = pc.Chat(self.transport, "42")
        for rc in (0, 1):
            with self.subTest(rc=rc):
                (stub / "gitleaks").write_text(f"#!/usr/bin/env bash\nexit {rc}\n")
                (stub / "gitleaks").chmod(0o755)
                with mock.patch.dict(os.environ, env):
                    for var in ("CLAUDE_CODE_OAUTH_TOKEN", "GATEKEEPER_TG_TOKEN", "LINEAR_API_KEY", "GH_TOKEN"):
                        os.environ.pop(var, None)
                    self.assertEqual(chat.scan("PO question (Telegram): x\n\nAnswer: y"), rc)
                self.assertFalse((root / "logs").exists(), "sourcing run.sh started a tick")
                self.assertFalse(marker.exists(), "sourcing run.sh started a tick")


class Budget(ChatTestCase):
    def test_over_the_daily_cap_the_bot_says_so_and_never_calls_claude(self):
        os.environ["PO_CHAT_DAILY_USD"] = "1"
        ledger = pc.chat_dir() / "ledger" / "2026-09-17.json"
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps({"spent_usd": 1.2, "calls": 9}))
        self.message("what's at risk?")
        self.assertEqual(self.runner.of("claude"), [])
        self.assertEqual(self.texts[-1], "Daily chat budget reached ($1.20 of $1.00); resend after midnight ET.")
        self.assertIsNone(inbox.claim_next(), "the message must be completed, not left for a retry")

    def test_the_ledger_day_is_eastern_time(self):
        self.now = datetime(2026, 9, 18, 3, 30, tzinfo=timezone.utc)  # 23:30 on the 17th in New York
        self.message("hi")
        self.assertTrue((pc.chat_dir() / "ledger" / "2026-09-17.json").exists())

    def test_every_call_is_charged(self):
        self.message("hi")
        self.message("again")
        self.assertEqual((self.ledger()["spent_usd"], self.ledger()["calls"]), (0.1, 2))

    def test_the_per_message_cap_is_charged_and_the_bot_says_so(self):
        self.runner.claude.append(_done(json.dumps({"type": "result", "subtype": "error_max_budget_usd",
                                                    "is_error": True, "session_id": "s", "total_cost_usd": 0.81}),
                                        rc=1))
        self.message("summarise every ticket ever")
        self.assertIn("per-message cap — ask something narrower", self.texts[-1])
        self.assertEqual(self.ledger()["spent_usd"], 0.81)

    def test_a_cap_reported_only_as_text_charges_the_cap(self):
        self.runner.claude.append(_done(stderr="Error: Exceeded USD budget (0.75)", rc=1))
        self.message("summarise")
        self.assertEqual(self.ledger()["spent_usd"], 0.75)
        self.assertIn("per-message cap", self.texts[-1])

    # A call whose cost cannot be read still spends money. Charging nothing
    # would let repeated failures run all day against a daily cap that never moves.
    def test_a_timeout_a_kill_and_unreadable_output_are_each_charged_the_cap(self):
        for outcome in (subprocess.TimeoutExpired("claude", 150), _done("not json at all", rc=1), _done("", rc=-9)):
            with self.subTest(outcome=type(outcome).__name__):
                before = self.ledger()["spent_usd"] if (pc.chat_dir() / "ledger").exists() else 0
                self.runner.claude.append(outcome)
                self.message("one")
                self.assertAlmostEqual(self.ledger()["spent_usd"] - before, 0.75)

    def test_repeated_timeouts_reach_the_daily_cap_instead_of_running_all_day(self):
        os.environ["PO_CHAT_DAILY_USD"] = "2"
        for _ in range(5):
            self.runner.claude.append(subprocess.TimeoutExpired("claude", 150))
            self.message("one")
        self.assertEqual(len(self.runner.of("claude")), 3, "the daily cap did not stop the timeouts")
        self.assertIn("Daily chat budget reached", self.texts[-1])


class Conversation(ChatTestCase):
    def claude_argv(self, i=-1):
        return self.runner.of("claude")[i]

    def test_whats_at_risk_is_answered_from_live_cycle_state(self):
        self.message("what's at risk this cycle?")
        self.assertEqual(self.runner.of("state")[0][-2:], ["--cycle", "current"])
        prompt = self.runner.prompts()[-1]
        self.assertIn('"stalled":[{"identifier":"SB-1"', prompt)
        self.assertIn("<telegram_message>\nwhat's at risk this cycle?\n</telegram_message>", prompt)
        self.assertEqual(self.texts, ["On track."])

    # The prompt is a whole cycle_state document; argv is not the place for it.
    def test_the_prompt_goes_on_stdin_not_argv(self):
        self.message("what's at risk this cycle?")
        self.assertIn("<cycle_state", self.runner.prompts()[-1])
        self.assertFalse([a for a in self.claude_argv() if "<telegram_message>" in a])

    # NEGATIVE: no quiet fallback to another cycle. If `--cycle current` cannot
    # be read, the PO is not asked, and the user is told why.
    def test_a_cycle_state_failure_says_so_and_calls_nobody(self):
        self.runner.state_result = _done(stderr="cycle_state: no cycles found", rc=1)
        self.message("what's at risk?")
        self.assertEqual(self.runner.of("claude"), [])
        self.assertEqual(len(self.runner.of("state")), 1, "a second cycle was consulted")
        self.assertEqual(self.texts[-1],
                         "Couldn't read cycle state right now (cycle_state: no cycles found); try again shortly.")

    def test_the_session_is_resumed_within_a_cycle_and_rotated_on_a_new_one(self):
        self.message("one")
        first = self.claude_argv()
        self.assertIn("--session-id", first)
        sid = first[first.index("--session-id") + 1]

        self.message("two")
        second = self.claude_argv()
        self.assertNotIn("--session-id", second)
        self.assertEqual(second[second.index("--resume") + 1], sid)
        self.assertIn('unchanged="true"', self.runner.prompts()[-1], "an unchanged cycle_state was resent in full")

        self.runner.state = [_state(number=10)]
        self.message("three")
        third = self.claude_argv()
        self.assertIn("--session-id", third)
        self.assertNotEqual(third[third.index("--session-id") + 1], sid)
        self.assertTrue((pc.chat_dir() / "sessions" / "cycle-10.json").exists())

    def test_changed_numbers_are_sent_again_on_a_resumed_session(self):
        self.message("one")
        moved = _state()
        moved["at_risk"]["not_started"] = [{"identifier": "SB-7", "estimate": 3}]
        self.runner.state = [moved]
        self.message("two")
        self.assertNotIn('unchanged="true"', self.runner.prompts()[-1])

    # Only the clock moved: resending the whole document every day would grow
    # the session for nothing. The fresh day counts ride on the unchanged tag.
    def test_time_derived_fields_alone_do_not_count_as_a_change(self):
        later = _state(days_left=3, stalled_days=6)
        later["generated_at"] = (NOW + timedelta(days=1)).isoformat()
        later["cycle"]["days_elapsed"] = 11
        later["waiting_on_human"][0]["age_days"] = 4
        self.assertEqual(pc.state_sha(_state()), pc.state_sha(later))

        self.message("one")
        self.runner.state = [later]
        self.message("two")
        prompt = self.runner.prompts()[-1]
        self.assertIn('unchanged="true"', prompt)
        self.assertIn('days_left="3"', prompt)

    def test_a_lost_session_starts_a_new_one_in_the_same_message(self):
        self.message("one")
        self.runner.claude.append(_done(stderr="No conversation found with session ID: x", rc=1))
        self.message("two")
        self.assertIn("--session-id", self.claude_argv())
        self.assertEqual(self.texts[-1], "On track.")

    # claude records a session's first system prompt and reuses it on resume, so
    # an edited cycle.md or chat.md would otherwise never reach a live chat.
    def test_an_edited_system_prompt_starts_a_new_session(self):
        chat_md = SKILLS_DIR / "po-agent" / "chat.md"
        original = chat_md.read_text()
        self.addCleanup(chat_md.write_text, original)
        self.message("one")
        sid = self.claude_argv()[self.claude_argv().index("--session-id") + 1]

        chat_md.write_text(original + "\n- a rule added after the session started\n")
        self.message("two")
        argv = self.claude_argv()
        self.assertIn("--session-id", argv)
        self.assertNotEqual(argv[argv.index("--session-id") + 1], sid)
        self.assertIn("a rule added after the session started",
                      argv[argv.index("--append-system-prompt") + 1])

    def test_a_long_session_is_rotated_before_it_grows_too_large(self):
        os.environ["PO_CHAT_MAX_TURNS"] = "2"
        self.message("one")
        sid = self.claude_argv()[self.claude_argv().index("--session-id") + 1]
        self.message("two")
        self.assertIn("--resume", self.claude_argv())
        self.message("three")
        argv = self.claude_argv()
        self.assertIn("--session-id", argv)
        self.assertNotEqual(argv[argv.index("--session-id") + 1], sid)

    # A resumed session that hits the per-message cap is usually one whose own
    # history costs that much; "ask something narrower" would be useless advice.
    def test_the_cap_on_a_resumed_session_rotates_it_and_says_so(self):
        self.message("one")
        self.runner.claude.append(_done(json.dumps({"type": "result", "subtype": "error_max_budget_usd",
                                                    "is_error": True, "session_id": "s", "total_cost_usd": 0.8}),
                                        rc=1))
        self.message("two")
        self.assertIn("started a fresh conversation", self.texts[-1])
        self.assertFalse((pc.chat_dir() / "sessions" / "cycle-9.json").exists())

    def test_the_system_prompt_is_cycle_md_read_at_call_time(self):
        cycle_md = pc.cycle_md()
        self.assertEqual(cycle_md, SKILLS_DIR.parent / "commands" / "cycle.md")
        self.message("one")
        appended = self.claude_argv()[self.claude_argv().index("--append-system-prompt") + 1]
        self.assertIn(cycle_md.read_text(), appended)
        self.assertIn((SKILLS_DIR / "po-agent" / "chat.md").read_text(), appended)

        original = cycle_md.read_text()
        self.addCleanup(cycle_md.write_text, original)
        cycle_md.write_text(original + "\n- a rule added after the process started\n")
        self.message("two")
        appended = self.claude_argv()[self.claude_argv().index("--append-system-prompt") + 1]
        self.assertIn("a rule added after the process started", appended)

    def test_chat_md_adapts_cycle_md_and_copies_none_of_its_rules(self):
        rules = {line.strip() for line in pc.cycle_md().read_text().splitlines() if len(line.strip()) > 30}
        chat = (SKILLS_DIR / "po-agent" / "chat.md").read_text()
        copied = [line for line in chat.splitlines() if line.strip() in rules]
        self.assertEqual(copied, [])
        self.assertLess(len(chat.splitlines()), 40, "chat.md is an adapter; the rules live in cycle.md")

    def test_the_model_runs_with_no_tools_no_mcp_no_skills_no_settings_and_a_budget(self):
        self.message("one")
        argv = self.claude_argv()
        self.assertEqual(argv[:2], ["claude", "-p"])
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "0.75")
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        for flag in ("--strict-mcp-config", "--disable-slash-commands", "--json-schema"):
            self.assertIn(flag, argv)
        self.assertFalse([a for a in argv if "dangerously" in a or a in ("--allowedTools", "--permission-mode")])

    # The model's `changes` is the cycle_apply changes file, enforced as a
    # schema rather than described in prose the model may improvise around.
    def test_the_changes_schema_is_the_changes_file_schema(self):
        self.message("one")
        argv = self.claude_argv()
        schema = json.loads(argv[argv.index("--json-schema") + 1])
        changes = schema["properties"]["changes"]["anyOf"][0]
        self.assertFalse(changes["additionalProperties"])
        self.assertEqual(sorted(changes["properties"]), ["cycle", "issues"])
        issue = changes["properties"]["issues"]["items"]
        self.assertFalse(issue["additionalProperties"])
        self.assertEqual(sorted(issue["properties"]), ["cancel", "cycle", "estimate", "identifier", "priority"])
        self.assertEqual(issue["properties"]["identifier"]["pattern"], r"^SB-\d+$")
        self.assertEqual(issue["required"], ["identifier"])

    def test_an_answer_with_no_structured_output_is_an_error_not_prose(self):
        self.runner.claude.append(_done(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                                    "result": "I would drop SB-4", "session_id": "s",
                                                    "total_cost_usd": 0.02})))
        self.message("what's at risk?")
        self.assertIn("unreadable answer", self.texts[-1])
        self.assertNotIn("I would drop SB-4", self.texts)

    def test_the_knobs_reach_the_argv(self):
        os.environ.update(PO_CHAT_MODEL="opus", PO_CHAT_MSG_USD="0.3")
        self.message("one")
        argv = self.claude_argv()
        self.assertEqual((argv[argv.index("--model") + 1], argv[argv.index("--max-budget-usd") + 1]), ("opus", "0.30"))

    def test_claude_never_sees_the_linear_or_telegram_credentials(self):
        os.environ.update(LINEAR_API_KEY="k", GATEKEEPER_TG_TOKEN="t", CLAUDE_CODE_OAUTH_TOKEN="c")
        self.message("one")
        env = [c["env"] for c in self.runner.calls if c["kind"] == "claude"][-1]
        self.assertNotIn("LINEAR_API_KEY", env)
        self.assertNotIn("GATEKEEPER_TG_TOKEN", env)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", env)

    def test_typing_is_shown_while_claude_runs(self):
        self.message("one")
        self.assertIn(("42", "typing"), self.transport.actions)

    # The liveness probe reads one file. A slow dry run or Linear call is not a
    # hang, and must not be restarted as one.
    def test_the_heartbeat_keeps_beating_through_a_slow_step_that_is_not_claude(self):
        beats = []
        self.chat.beat = lambda: beats.append(1)
        with mock.patch.object(pc, "HEARTBEAT_EVERY_SECONDS", 0.01):
            def slow(kind):
                if kind == "state":
                    import time as _t
                    _t.sleep(0.3)

            self.runner.before = slow
            self.message("one")
        self.assertGreater(len(beats), 3, f"only {len(beats)} heartbeat(s) during a 0.3s step")

    def test_a_timeout_replies_and_completes(self):
        self.runner.claude.append(subprocess.TimeoutExpired("claude", 150))
        self.message("one")
        self.assertIn("longer than 150s", self.texts[-1])
        self.assertIsNone(inbox.claim_next())
        self.assertEqual(len(list((inbox.inbox_dir() / "done").glob("*.json"))), 1)

    def test_a_failed_call_replies_and_completes(self):
        self.runner.claude.append(_done(_claude_json(), rc=1))
        self.message("one")
        self.assertIn("The PO call failed", self.texts[-1])
        self.assertEqual(len(list((inbox.inbox_dir() / "done").glob("*.json"))), 1)

    def test_a_message_from_another_chat_is_dropped(self):
        self.message("hi", chat_id=99, from_id=99)
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.texts, [])

    def test_a_message_that_keeps_crashing_the_process_is_dropped_after_two_tries(self):
        inbox.record({"message_id": 1, "from": {"id": 42}, "chat": {"id": 42}, "text": "boom"}, 700)
        attempts = pc.chat_dir() / "attempts" / "700"
        attempts.parent.mkdir(parents=True)
        attempts.write_text("2")
        self.chat.process(inbox.claim_next())
        self.assertEqual(self.runner.calls, [])
        self.assertIn("couldn't process your message", self.texts[-1])
        self.assertFalse(attempts.exists())

    # A message we abandon for retry is not charged: the process that answers it
    # pays for that call. Charging here too would let a rollout loop, or a
    # liveness kill, spend the day's budget on messages nobody ever answered.
    def test_sigterm_during_the_call_requeues_the_message_without_charging(self):
        def stopped(kind):
            if kind == "claude":
                self.chat.stopping = True

        self.runner.before = stopped
        uid = self.message("one")
        self.assertTrue((inbox.inbox_dir() / "new" / f"{uid}.json").exists())
        self.assertEqual(self.texts, [])
        self.assertFalse((pc.chat_dir() / "attempts" / str(uid)).exists(), "a stop counted as a failed attempt")
        self.assertFalse((pc.chat_dir() / "ledger").exists(), "an abandoned message was charged")

    def test_a_timeout_we_caused_by_stopping_is_not_charged_either(self):
        def stopped(kind):
            if kind == "claude":
                self.chat.stopping = True
                raise subprocess.TimeoutExpired("claude", 150)

        self.runner.before = stopped
        uid = self.message("one")
        self.assertTrue((inbox.inbox_dir() / "new" / f"{uid}.json").exists())
        self.assertFalse((pc.chat_dir() / "ledger").exists())

    # SIGTERM before the child exists: nothing was spent, and the message is
    # still there for the next process.
    def test_sigterm_before_the_call_requeues_without_spending(self):
        self.chat.stopping = True
        uid = self.message("one")
        self.assertTrue((inbox.inbox_dir() / "new" / f"{uid}.json").exists())
        self.assertEqual(self.runner.of("claude"), [])
        self.assertFalse((pc.chat_dir() / "ledger").exists())
        self.assertFalse((pc.chat_dir() / "attempts" / str(uid)).exists())


class OwnMessages(ChatTestCase):
    """The chat records its own message ids where gate.py reads them, so a reply
    to a question or a dry run is never taken as a gate answer (SB-1089)."""

    def test_every_message_the_chat_sends_is_recorded_for_the_listener(self):
        self.message("hi")
        sent = json.loads((pc.chat_dir() / "outbox.json").read_text())
        self.assertEqual(sent, [1])
        self.message("again")
        self.assertEqual(json.loads((pc.chat_dir() / "outbox.json").read_text()), [1, 2])

    def test_the_record_is_bounded(self):
        path = pc.chat_dir() / "outbox.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(list(range(-pc.OUTBOX_LIMIT, 0))))
        self.message("hi")
        kept = json.loads(path.read_text())
        self.assertEqual(len(kept), pc.OUTBOX_LIMIT)
        self.assertEqual(kept[-1], 1)

    # A long dry run is chunked, and the yes comes back on whichever chunk the
    # user was reading — not necessarily the last.
    def test_a_yes_replying_to_an_earlier_chunk_of_the_dry_run_confirms(self):
        import tg

        with mock.patch.object(tg, "MAX_MESSAGE", 60):
            self.runner.claude.append(_done(_claude_json("Estimate SB-1 at 3? " * 20, changes=CHANGES)))
            self.message("estimate SB-1 at 3")
        shown_on = json.loads(self.pending().read_text())["message_ids"]
        self.assertGreater(len(shown_on), 1, "the fixture did not chunk the dry run")
        self.message("yes", reply_to=shown_on[0])
        self.assertEqual(len(self.runner.of("confirm")), 1)


class Loop(ChatTestCase):
    def test_a_message_left_in_progress_is_requeued_and_handled_on_start(self):
        inbox.record({"message_id": 1, "from": {"id": 42}, "chat": {"id": 42}, "text": "left over"}, 650)
        inbox.claim_next()
        handled = []

        def process(path):
            handled.append(path.name)
            inbox.complete(path)
            self.chat.stopping = True

        self.chat.process = process
        self.assertEqual(self.chat.run_loop(), 0)
        self.assertEqual(handled, ["650.json"])

    def test_main_moving_exits_75_between_messages(self):
        chat = pc.Chat(self.transport, "42", run=self.runner, now=lambda: NOW, sleep=lambda s: None,
                       start_sha="a" * 40, ls_remote=lambda: "b" * 40, update_every=0)
        self.assertEqual(chat.run_loop(), pc.EXIT_MOVED)

    def test_main_not_moving_or_github_down_keeps_consuming(self):
        rounds = []

        def sleep(s):
            rounds.append(s)
            if len(rounds) == 3:
                chat.stopping = True

        def down():
            raise OSError("github unreachable")

        for ls_remote in (lambda: "a" * 40, down):
            rounds.clear()
            chat = pc.Chat(self.transport, "42", run=self.runner, now=lambda: NOW, sleep=sleep,
                           start_sha="a" * 40, ls_remote=ls_remote, update_every=0)
            self.assertEqual(chat.run_loop(), 0)
            self.assertEqual(rounds, [pc.POLL_SECONDS] * 3)


if __name__ == "__main__":
    unittest.main()
