"""Free-text Telegram messages, recorded for a consumer that is not the gate (SB-951).

The listener owns `getUpdates`, so a message that is not a gate answer would
otherwise be read, acked and gone. SB-1089 (chat with the PO) needs those
messages, and must not become a second `getUpdates` reader to get them — two
readers on one token is a 409. So the listener records them here and the
consumer reads files.

Maildir-shaped, so the protocol is renames and nothing else:

    $GATEKEEPER_STATE/inbox/telegram/
      tmp/<update_id>.json    being written — never read
      new/<update_id>.json    recorded, unclaimed
      cur/<update_id>.json    claimed by a consumer, in progress
      done/<update_id>.json   handled

`record` writes tmp and renames into new. `claim_next` renames new -> cur, and
a rename is atomic, so two consumers can never both claim one message: the
loser's rename finds no file. `complete` moves cur -> done; `requeue` moves it
back to new, for a consumer that fails and wants the message retried.

The update_id is the filename, which makes recording idempotent: Telegram
redelivers any update the listener did not ack, and a redelivered message must
not reach the PO twice.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "gatekeeper.inbox/1"

# tmp is deliberately NOT one of the dedupe dirs. A crash between the write and
# the rename leaves a tmp file behind and no ack; if tmp counted as "already
# recorded", the redelivery would be skipped, acked, and the message lost.
RECORDED = ("new", "cur", "done")


def inbox_dir() -> Path:
    # Same root as gate.state_dir(), re-derived rather than imported: gate.py
    # imports this module, and a function (not a constant) so tests can point
    # GATEKEEPER_STATE at a tempdir.
    state = os.environ.get("GATEKEEPER_STATE", "") or Path.home() / ".local/state/cycle-runner"
    return Path(state) / "inbox" / "telegram"


def _root(root: Path | None) -> Path:
    root = root or inbox_dir()
    for sub in ("tmp",) + RECORDED:
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def exists(update_id: int, root: Path | None = None) -> bool:
    """True once `update_id` has been recorded, whatever has happened to it since."""
    root = root or inbox_dir()
    name = f"{int(update_id)}.json"
    return any((root / sub / name).exists() for sub in RECORDED)


def record(message: dict, update_id: int, root: Path | None = None) -> Path | None:
    """Record one message into new/. Returns its path, or None if this update
    was already recorded (a redelivery)."""
    root = _root(root)
    if exists(update_id, root):
        return None
    name = f"{int(update_id)}.json"
    rec = {
        "schema": SCHEMA,
        "update_id": int(update_id),
        "message_id": message.get("message_id"),
        "chat_id": (message.get("chat") or {}).get("id"),
        "from_id": (message.get("from") or {}).get("id"),
        "date": message.get("date"),
        "text": message.get("text") or "",
        "received_at": datetime.now(timezone.utc).isoformat(),
        # Added in SB-1089, so still /1. A reply to one of the PO's questions
        # is how the chat knows which ticket an answer belongs to.
        "reply_to_message_id": (message.get("reply_to_message") or {}).get("message_id"),
    }
    tmp = root / "tmp" / name
    with open(tmp, "w") as fh:
        fh.write(json.dumps(rec, indent=2))
        fh.flush()
        os.fsync(fh.fileno())
    os.rename(tmp, root / "new" / name)
    return root / "new" / name


def _update_id(path: Path) -> int:
    try:
        return int(path.stem)
    except ValueError:
        return -1


def claim_next(root: Path | None = None) -> Path | None:
    """Claim the oldest unclaimed message: rename new -> cur and return the cur
    path, or None when there is nothing to claim. Oldest by update_id, which
    Telegram assigns in order — numerically, so 10 does not sort before 9."""
    root = _root(root)
    for path in sorted((root / "new").glob("*.json"), key=_update_id):
        dest = root / "cur" / path.name
        try:
            os.rename(path, dest)
        except FileNotFoundError:
            continue  # another consumer claimed it between the glob and here
        return dest
    return None


def complete(path: Path) -> Path:
    """cur -> done. The message has been handled."""
    dest = path.parent.parent / "done" / path.name
    os.rename(path, dest)
    return dest


def requeue(path: Path) -> Path:
    """cur -> new. The consumer failed; the next claim_next sees it again."""
    dest = path.parent.parent / "new" / path.name
    os.rename(path, dest)
    return dest
