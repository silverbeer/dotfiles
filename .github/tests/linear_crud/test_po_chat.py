"""po_chat.py in po-agent/scripts — the PO chat over Telegram (SB-1089), offline.

The contract the chat leans on: nothing reaches Linear without a plain yes to
a dry run the user has seen, in a LATER message than the proposal. "no",
anything else, or silence writes nothing. A question's answer is scanned
before it becomes a comment. Both budget caps answer the user instead of
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


def _state(number=9, days_left=4):
    return {"schema": "po-agent.cycle_state/1", "generated_at": NOW.isoformat(),
            "cycle": {"number": number, "days_left": days_left},
            "at_risk": {"not_started": [], "stalled": [{"identifier": "SB-1", "title": "Do the thing"}]}}


def _done(stdout="", rc=0, stderr=""):
    return subprocess.CompletedProcess([], rc, stdout, stderr)


def _claude_json(reply="On track.", changes=None, ask=None, cost=0.05, session_id="s", **extra):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "",
                       "session_id": session_id, "total_cost_usd": cost,
                       "structured_output": {"reply": reply, "changes": changes, "ask": ask}, **extra})


CHANGES = {"cycle": None, "issues": [{"identifier": "SB-1", "estimate": 3}]}


class FakeRunner:
    """Answers each subprocess po_chat.py starts, by what it is, and records the
    order they ran in (`events` is shared with FakeLinear's comment writes)."""

    def __init__(self, events):
        self.events = events
        self.calls = []
        self.state = [_state()]
        self.claude = []
        self.dry_run = _done("  SB-1     estimate: 2 -> 3\n\ndry run — nothing written; pass --confirm to write")
        self.confirm = _done("  updated SB-1\n\n1 issue(s) updated")
        self.scan_rc = 0

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

    def __call__(self, argv, *, timeout, cwd=None, env=None, interruptible=False):
        kind = self.kind(argv)
        self.calls.append((kind, list(argv), env))
        self.events.append(kind)
        if kind == "state":
            doc = self.state[0] if len(self.state) == 1 else self.state.pop(0)
            return _done(json.dumps(doc))
        if kind == "claude":
            nxt = self.claude.pop(0) if self.claude else _done(_claude_json())
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        if kind == "scan":
            return _done(rc=self.scan_rc)
        return self.dry_run if kind == "dry_run" else self.confirm

    def of(self, kind):
        return [argv for k, argv, _ in self.calls if k == kind]


class ChatTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ, {"GATEKEEPER_STATE": self._tmp.name, "LINEAR_ASSIGNEE_ID": "user-1",
                                           "XDG_CACHE_HOME": self._tmp.name})
        env.start()
        self.addCleanup(env.stop)
        for knob in ("PO_CHAT_MODEL", "PO_CHAT_MSG_USD", "PO_CHAT_DAILY_USD"):
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

    def message(self, text, reply_to=None, chat_id=42, from_id=42):
        """Record one message in the inbox, claim it and process it, as the loop does."""
        self.next_update += 1
        msg = {"message_id": self.next_update * 10, "from": {"id": from_id}, "chat": {"id": chat_id,
               "type": "private"}, "date": 1789000000, "text": text}
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

    def pending(self):
        return pc.chat_dir() / "pending.json"

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
        pending = json.loads(self.pending().read_text())
        self.assertEqual(pending["changes"], CHANGES)
        self.assertEqual(datetime.fromisoformat(pending["expires_at"]) - NOW, timedelta(minutes=30))

    def test_yes_applies_exactly_the_pending_file_once_without_calling_the_po(self):
        self.propose()
        self.message("Yes.")
        confirms = self.runner.of("confirm")
        self.assertEqual(len(confirms), 1)
        self.assertEqual(confirms[0][confirms[0].index("--changes") + 1], str(pc.chat_dir() / "pending-changes.json"))
        self.assertEqual(len(self.runner.of("claude")), 1, "a bare yes should not cost a PO call")
        self.assertFalse(self.pending().exists())
        self.assertIn("✅ Written.", self.texts[-1])
        self.assertIn("was applied", " ".join(self.chat.notes()))

        # ...and the PO hears about it on the next turn, once.
        self.message("thanks")
        prompt = self.runner.of("claude")[-1][2]
        self.assertIn("<system_note>The user said yes", prompt)
        self.assertEqual(self.chat.notes(), [])

    def test_no_writes_nothing_and_the_po_hears_it(self):
        self.propose()
        self.message("no")
        self.assertEqual(self.runner.of("confirm"), [])
        self.assertFalse(self.pending().exists())
        self.assertIn("Nothing written.", self.texts)
        self.assertIn("not a yes", self.runner.of("claude")[-1][2])

    def test_anything_short_of_a_plain_yes_writes_nothing(self):
        for text in ("yes but drop SB-4", "yeah", "what would that change?"):
            with self.subTest(text=text):
                self.propose()
                self.message(text)
                self.assertEqual(self.runner.of("confirm"), [])
                self.assertFalse(self.pending().exists())

    def test_silence_expires_it(self):
        self.propose()
        self.now = NOW + timedelta(minutes=31)
        self.message("yes")
        self.assertEqual(self.runner.of("confirm"), [])
        self.assertTrue(any("older than 30 minutes" in t for t in self.texts))

    def test_a_changed_file_is_not_applied(self):
        self.propose()
        changes = pc.chat_dir() / "pending-changes.json"
        changes.write_text(json.dumps({"issues": [{"identifier": "SB-1", "cancel": True}]}))
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

    def test_a_reply_is_scanned_then_posted_on_the_ticket(self):
        q = self.ask()
        self.events.clear()
        self.message("3, it's small", reply_to=q["message_id"])
        self.assertEqual(self.events[:2], ["scan", "comment"], "the answer must be scanned BEFORE it is posted")
        self.assertEqual(len(self.linear.comments), 1)
        self.assertEqual(self.linear.comments[0]["body"],
                         "PO question (Telegram): Estimate SB-1? 1/2/3/5/8\n\nAnswer: 3, it's small")
        self.assertIn("Posted on SB-1 https://linear.app/silverbeer/issue/SB-1", self.texts)
        self.assertIn("answer was posted as a comment", self.runner.of("claude")[-1][2])

    def test_a_scan_that_flags_or_cannot_run_posts_nothing(self):
        for rc in (1, 2):
            with self.subTest(rc=rc):
                q = self.ask()
                self.runner.scan_rc = rc
                self.message("here is the key", reply_to=q["message_id"])
                self.assertEqual(self.linear.comments, [])
                self.assertTrue(any(t.startswith("🔒 Not posted to SB-1") for t in self.texts))
                shutil.rmtree(pc.chat_dir() / "questions")

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

    def test_a_question_on_something_that_is_not_a_ticket_is_dropped(self):
        self.runner.claude.append(_done(_claude_json("hm", ask={"ticket": "the backlog", "question": "why?"})))
        self.message("hi")
        self.assertFalse((pc.chat_dir() / "questions").exists())
        self.assertEqual(len(self.texts), 1)

    def test_the_ask_cli_posts_and_records_a_question(self):
        gk = gate.Gatekeeper(self.transport, "42", {42})
        with mock.patch.object(gate, "gatekeeper_from_env", return_value=gk), \
                mock.patch("sys.stdout"):
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
        self.assertEqual(inbox.claim_next(), None, "the message must be completed, not left for a retry")

    def test_the_ledger_day_is_eastern_time(self):
        self.now = datetime(2026, 9, 18, 3, 30, tzinfo=timezone.utc)  # 23:30 on the 17th in New York
        self.message("hi")
        self.assertTrue((pc.chat_dir() / "ledger" / "2026-09-17.json").exists())

    def test_every_call_is_charged(self):
        self.message("hi")
        self.message("again")
        led = json.loads((pc.chat_dir() / "ledger" / "2026-09-17.json").read_text())
        self.assertEqual((led["spent_usd"], led["calls"]), (0.1, 2))

    def test_the_per_message_cap_is_charged_and_the_bot_says_so(self):
        self.runner.claude.append(_done(json.dumps({"type": "result", "subtype": "error_max_budget_usd",
                                                    "is_error": True, "session_id": "s", "total_cost_usd": 0.81}),
                                        rc=1))
        self.message("summarise every ticket ever")
        self.assertEqual(self.texts[-1], "💸 That hit the $0.75 per-message cap — ask something narrower. "
                                         "Nothing was written.")
        led = json.loads((pc.chat_dir() / "ledger" / "2026-09-17.json").read_text())
        self.assertEqual(led["spent_usd"], 0.81)

    def test_a_cap_reported_only_as_text_charges_the_cap(self):
        self.runner.claude.append(_done(stderr="Error: Exceeded USD budget (0.75)", rc=1))
        self.message("summarise")
        led = json.loads((pc.chat_dir() / "ledger" / "2026-09-17.json").read_text())
        self.assertEqual(led["spent_usd"], 0.75)
        self.assertIn("per-message cap", self.texts[-1])


class Conversation(ChatTestCase):
    def claude_argv(self, i=-1):
        return self.runner.of("claude")[i]

    def test_whats_at_risk_is_answered_from_live_cycle_state(self):
        self.message("what's at risk this cycle?")
        self.assertEqual(self.runner.of("state")[0][-2:], ["--cycle", "current"])
        prompt = self.claude_argv()[2]
        self.assertIn('"stalled":[{"identifier":"SB-1"', prompt)
        self.assertIn("<telegram_message>\nwhat's at risk this cycle?\n</telegram_message>", prompt)
        self.assertEqual(self.texts, ["On track."])

    def test_no_active_cycle_falls_back_to_plan_target(self):
        calls = []

        def run(argv, **kw):
            if argv[1].endswith("cycle_state.py"):
                calls.append(argv[-1])
                if argv[-1] == "current":
                    return _done(stderr="cycle_state: no active cycle", rc=1)
            return self.runner(argv, **kw)

        self.chat.run = run
        self.message("hi")
        self.assertEqual(calls, ["current", "plan-target"])
        self.assertEqual(self.texts, ["On track."])

    def test_the_session_is_resumed_within_a_cycle_and_rotated_on_a_new_one(self):
        self.message("one")
        first = self.claude_argv()
        self.assertIn("--session-id", first)
        sid = first[first.index("--session-id") + 1]

        self.message("two")
        second = self.claude_argv()
        self.assertNotIn("--session-id", second)
        self.assertEqual(second[second.index("--resume") + 1], sid)
        self.assertIn('unchanged="true"', second[2], "an unchanged cycle_state was sent again in full")

        self.runner.state = [_state(number=10)]
        self.message("three")
        third = self.claude_argv()
        self.assertIn("--session-id", third)
        self.assertNotEqual(third[third.index("--session-id") + 1], sid)
        self.assertTrue((pc.chat_dir() / "sessions" / "cycle-10.json").exists())

    def test_changed_numbers_are_sent_again_on_a_resumed_session(self):
        self.message("one")
        self.runner.state = [_state(days_left=3)]
        self.message("two")
        self.assertNotIn('unchanged="true"', self.claude_argv()[2])

    def test_generated_at_alone_does_not_count_as_a_change(self):
        later = dict(_state(), generated_at=(NOW + timedelta(hours=1)).isoformat())
        self.assertEqual(pc.state_sha(_state()), pc.state_sha(later))

    def test_a_lost_session_starts_a_new_one_in_the_same_message(self):
        self.message("one")
        self.runner.claude.append(_done(stderr="No conversation found with session ID: x", rc=1))
        self.message("two")
        self.assertIn("--session-id", self.claude_argv())
        self.assertEqual(self.texts[-1], "On track.")

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

    def test_the_knobs_reach_the_argv(self):
        os.environ.update(PO_CHAT_MODEL="opus", PO_CHAT_MSG_USD="0.3")
        self.message("one")
        argv = self.claude_argv()
        self.assertEqual((argv[argv.index("--model") + 1], argv[argv.index("--max-budget-usd") + 1]), ("opus", "0.30"))

    def test_claude_never_sees_the_linear_or_telegram_credentials(self):
        os.environ.update(LINEAR_API_KEY="k", GATEKEEPER_TG_TOKEN="t", CLAUDE_CODE_OAUTH_TOKEN="c")
        self.message("one")
        env = [e for k, _, e in self.runner.calls if k == "claude"][-1]
        self.assertNotIn("LINEAR_API_KEY", env)
        self.assertNotIn("GATEKEEPER_TG_TOKEN", env)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", env)

    def test_typing_is_shown_while_claude_runs(self):
        self.message("one")
        self.assertIn(("42", "typing"), self.transport.actions)
        self.assertTrue((pc.chat_dir() / "heartbeat").exists())

    def test_a_timeout_replies_and_completes(self):
        self.runner.claude.append(subprocess.TimeoutExpired("claude", 150))
        self.message("one")
        self.assertIn("longer than 150s", self.texts[-1])
        self.assertIsNone(inbox.claim_next())
        self.assertEqual(len(list((inbox.inbox_dir() / "done").glob("*.json"))), 1)

    def test_a_failed_call_replies_and_completes(self):
        self.runner.claude.append(_done(stderr="invalid_api_key", rc=1))
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

    def test_sigterm_during_the_call_requeues_the_message(self):
        def stopped(argv, **kw):
            if argv[0] == "claude":
                self.chat.stopping = True
            return self.runner(argv, **kw)

        self.chat.run = stopped
        uid = self.message("one")
        self.assertTrue((inbox.inbox_dir() / "new" / f"{uid}.json").exists())
        self.assertEqual(self.texts, [])
        self.assertFalse((pc.chat_dir() / "attempts" / str(uid)).exists())


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
