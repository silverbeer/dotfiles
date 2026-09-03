"""Fakes shared by test_gate.py and test_tg.py — no socket, no subprocess.

Not itself a test file (no `test_` prefix), so `unittest discover` skips it.
"""

from __future__ import annotations

from typing import Any


class FakeTransport:
    """Records every Bot API call; replays queued getUpdates batches. Same
    shape as trd's tests/test_bot.py FakeTransport, extended for the two calls
    the gatekeeper adds (callback answers, keyboards)."""

    def __init__(self, batches: list[list[dict[str, Any]]] | None = None) -> None:
        self.batches = batches or []
        self.sent: list[tuple[str, str, dict | None]] = []
        self.offsets: list[int] = []
        self.answered: list[tuple[str, str]] = []
        # Set to an Exception instance to make every send_message raise it —
        # simulates a Telegram-side failure (SB-508).
        self.fail_send: Exception | None = None
        # One entry per send_message call, aligned with self.sent (SB-982).
        self.entities: list[list[dict] | None] = []
        # Same, for disable_notification (SB-985).
        self.silent: list[bool] = []
        # Same, for answerCallbackQuery: a callback id expires ~1 min after the
        # tap and this poller runs on a 30-min tick, so in production the answer
        # nearly always fails. SB-950 — that failure used to destroy the
        # decision, so it needs to be reachable from a test.
        self.fail_answer: Exception | None = None

    def send_message(
        self,
        chat_id: str,
        text: str,
        reply_markup: dict | None = None,
        entities: list[dict] | None = None,
        silent: bool = False,
    ) -> dict:
        if self.fail_send is not None:
            raise self.fail_send
        self.sent.append((chat_id, text, reply_markup))
        # Recorded separately so the existing 3-tuple assertions keep working;
        # SB-982 reads entities, SB-985 reads silent.
        self.entities.append(entities)
        self.silent.append(silent)
        return {"message_id": len(self.sent)}

    def get_updates(self, offset: int, timeout: int, allowed_updates: list[str]) -> list[dict[str, Any]]:
        self.offsets.append(offset)
        return self.batches.pop(0) if self.batches else []

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        if self.fail_answer is not None:
            raise self.fail_answer
        self.answered.append((callback_query_id, text))

    def get_me(self) -> dict:
        return {"id": 1, "is_bot": True, "username": "gatekeeper_bot"}

    @property
    def texts(self) -> list[str]:
        return [text for _, text, _ in self.sent]


class FakeLinear:
    """Minimal fake of linear_api.gql that answers gate.py's exact query
    shapes, matched by substring (the query strings are stable and distinct —
    see gate.py's issue_info/create_comment/issue_comments/set_gate_label/
    assignee_id). One ticket per instance is enough for these tests.
    """

    def __init__(
        self,
        ticket: str = "SB-1",
        title: str = "Do the thing",
        url: str = "https://linear.app/silverbeer/issue/SB-1",
        assignee_id: str = "user-1",
        labels: list[str] | None = None,
        state_name: str = "In Progress",
        state_type: str = "started",
    ) -> None:
        self.ticket = ticket
        self.issue_id = "issue-uuid-1"
        self.title = title
        self.url = url
        self.assignee_id = assignee_id
        # A gate whose ticket has already reached Done has no question left to
        # ask (SB-949). Mutable so a test can close the ticket mid-flight.
        self.state_name = state_name
        self.state_type = state_type
        self.labels: list[str] = list(labels if labels is not None else ["type:feature"])
        self.label_catalog: dict[str, str] = {
            "type:feature": "lbl-type-feature",
            "gate:awaiting-approval": "lbl-awaiting",
            "gate:approved": "lbl-approved",
            "gate:rejected": "lbl-rejected",
            "gate:needs-human": "lbl-needs-human",
        }
        self.comments: list[dict[str, Any]] = []
        self.queries: list[tuple[str, dict]] = []
        self._next_comment_id = 1
        self._next_ts = 1
        # Set to raise `issueUpdate` (the label rewrite) as failed — simulates
        # a Linear-side label-update failure (SB-508).
        self.fail_issue_update: SystemExit | None = None

    def __call__(self, query: str, variables: dict | None = None) -> dict:
        variables = variables or {}
        self.queries.append((query, variables))
        # Order matters: "comments(first" and "issue(id: $key)" both appear in
        # issue_comments' query text, so the more specific check goes first.
        if "commentCreate" in query:
            return self._comment_create(variables)
        if "comments(first" in query:
            return self._issue_comments()
        if "issueLabels(first" in query:
            return {"issueLabels": {"nodes": [{"id": i, "name": n} for n, i in self.label_catalog.items()]}}
        if "issueUpdate(id: $id" in query:
            return self._issue_update(variables)
        if "viewer { id }" in query:
            return {"viewer": {"id": self.assignee_id}}
        if "issue(id: $key)" in query:
            return self._issue_info()
        raise AssertionError(f"FakeLinear: unrecognised query: {query!r}")

    # -------------------------------------------------------------- helpers

    def add_comment(self, body: str, user_id: str | None = None, created_at: str | None = None) -> str:
        """Test helper: simulate a comment posted through the Linear app
        (i.e. NOT going through create_comment, which is the agent's own)."""
        cid = f"c{self._next_comment_id}"
        self._next_comment_id += 1
        ts = created_at or self._tick()
        self.comments.append({"id": cid, "body": body, "createdAt": ts, "user": {"id": user_id or self.assignee_id}})
        return cid

    def _tick(self) -> str:
        ts = f"2026-01-01T00:00:{self._next_ts:02d}Z"
        self._next_ts += 1
        return ts

    # ------------------------------------------------------------ handlers

    def _issue_info(self) -> dict:
        return {
            "issue": {
                "id": self.issue_id,
                "title": self.title,
                "url": self.url,
                "assignee": {"id": self.assignee_id},
                "state": {"name": self.state_name, "type": self.state_type},
                "labels": {"nodes": [{"id": self.label_catalog.get(n, n), "name": n} for n in self.labels]},
            }
        }

    def _comment_create(self, variables: dict) -> dict:
        cid = self.add_comment(variables["body"], user_id=self.assignee_id)
        return {"commentCreate": {"success": True, "comment": {"id": cid}}}

    def _issue_comments(self) -> dict:
        return {"issue": {"comments": {"nodes": list(self.comments)}}}

    def _issue_update(self, variables: dict) -> dict:
        if self.fail_issue_update is not None:
            raise self.fail_issue_update
        by_id = {i: n for n, i in self.label_catalog.items()}
        self.labels = [by_id[i] for i in variables["labelIds"] if i in by_id]
        return {"issueUpdate": {"success": True}}
