#!/usr/bin/env python3
"""The gatekeeper bot's one Telegram reader (SB-951).

Runs for ever, as the `gatekeeper-listener` Deployment in k3s. Each loop:

  1. long-polls `getUpdates` and routes each update (gate.Gatekeeper
     .handle_update): a button tap is recorded on its gate, answered, then
     applied; a message is a gate answer or goes to the inbox (inbox.py)
  2. retries any decision that was recorded but did not land (Linear down)
  3. writes a heartbeat, which the liveness probe reads
  4. every five minutes, asks GitHub whether dotfiles@main moved; if so it
     fetches and resets its clone, then re-executes itself on the new code
     in the same process

THIS IS THE ONLY CALLER OF `get_updates`. Telegram allows one `getUpdates`
reader per bot token; a second one gets HTTP 409 and so does the first. Before
SB-951 the reader was the runner's `gate.py poll`, on a 30-minute tick, which
is why a tap spun for up to half an hour and its answer usually expired
(SB-950). Anything else that needs Telegram input reads what this records —
gate state, or the inbox. Sending (`sendMessage`, `editMessageText`) from other
processes is fine: only reading conflicts. test_listen.py fails if another
caller appears.

Acking. An update's offset is committed only after its handling is on disk.
A transport failure or a failed disk write stops the batch unacked, and
Telegram redelivers it — every handler is idempotent on update_id, so a
redelivery is a no-op for whatever already landed. An update that raises
anything else is a bug in our handling, not weather: it is logged and acked,
so it cannot block every update behind it for 24h.

A 409 is not weather either: it means a second reader exists. Exit 3
(EXIT_CONFLICT), loudly, and nothing else exits 3 — doctor.sh reads the last
exit code, so "second reader" and "crashed" stay distinguishable.

Self-update is in-process (execv), NOT by exiting for the kubelet to restart.
Every container exit bumps restartCount for good and is subject to the
kubelet's restart backoff (10s doubling to 5 min, reset only after 10 clean
minutes), so two merges in quick succession would stretch the window with no
reader, and a 409 would hide behind the self-update exit that followed it.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import gate  # noqa: E402  (tests monkeypatch gate.gql)
from tg import TelegramConflict, TelegramError  # noqa: E402

LONG_POLL_SECONDS = 25
BACKOFF_START_SECONDS = 5
BACKOFF_MAX_SECONDS = 60
UPDATE_CHECK_SECONDS = 300
ALLOWED_UPDATES = ["callback_query", "message"]

# The container's exit code when another process is reading this bot token.
# Documented in SKILL.md and read by doctor.sh; nothing else may exit 3.
EXIT_CONFLICT = 3

# Failures that are weather: back off, do not ack. URLError and ConnectionError
# are OSErrors. tg.py converts HTTP and URL errors into TelegramError, but a
# response cut off or garbled mid-body escapes it as http.client or json errors.
TRANSIENT = (TelegramError, OSError, http.client.HTTPException)


def log(msg: str) -> None:
    print(f"listen: {msg}", file=sys.stderr, flush=True)


class _Stop(Exception):
    """Raised out of a blocking wait by SIGTERM, so the pod stops in well under
    its 10-second grace period instead of finishing a 25-second long poll."""


class Listener:
    def __init__(
        self,
        gk: gate.Gatekeeper,
        start_sha: str | None = None,
        ls_remote=None,
        fetch_reset=None,
        reexec=None,
        update_every: float = UPDATE_CHECK_SECONDS,
        clock=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        self.gk = gk
        self.start_sha = start_sha
        self.ls_remote = ls_remote
        self.fetch_reset = fetch_reset
        self.reexec = reexec or _reexec
        self.update_every = update_every
        self.clock = clock
        self.sleep = sleep
        self.stopping = False
        self._waiting = False
        # The same offset file the runner's poll used, so the first deploy
        # carries on where the last tick left off rather than replaying 24h.
        self.offset_path = gate.state_dir() / "telegram-offset"
        self.heartbeat_path = gate.state_dir() / "listener" / "heartbeat"

    # ----------------------------------------------------------- offset

    def _offset(self) -> int:
        try:
            return int(self.offset_path.read_text().strip())
        except (OSError, ValueError):
            return 0

    def _remember(self, offset: int) -> None:
        gate._write_atomic(self.offset_path, str(offset))

    # ------------------------------------------------------------ drain

    def drain(self, timeout: int) -> None:
        """Fetch one batch and handle it in order. Raises TelegramError or
        OSError, unacked, for the caller to back off on."""
        self._waiting = True
        try:
            updates = self.gk.transport.get_updates(
                offset=self._offset(), timeout=timeout, allowed_updates=ALLOWED_UPDATES
            )
        except (http.client.HTTPException, json.JSONDecodeError) as exc:
            name = exc.__class__.__name__
            raise TelegramError(f"Telegram getUpdates response was cut off or garbled ({name})") from None
        finally:
            self._waiting = False
        for update in updates:
            if self.stopping:
                return
            update_id = update.get("update_id")
            try:
                self.gk.handle_update(update)
            except TRANSIENT as exc:
                # The decision may not be recorded. Do NOT ack: Telegram
                # redelivers for 24h, which is exactly the safety net wanted,
                # and the rest of the batch is retried in order behind it
                # (SB-950: acking through a failure destroyed an approval).
                log(f"update {update_id} not acked, will be redelivered: {exc}")
                raise
            except Exception as exc:  # noqa: BLE001 — poison-pill guard
                log(f"dropping unprocessable update {update_id}: {exc!r}")
            else:
                kinds = [k for k in ALLOWED_UPDATES if k in update] or ["other"]
                log(f"update {update_id} ({kinds[0]}) handled")
            self._remember(int(update["update_id"]) + 1)

    # ------------------------------------------------------------- loop

    def beat(self) -> None:
        try:
            gate._write_atomic(self.heartbeat_path, gate.now_utc().isoformat())
        except OSError as exc:
            # The probe will notice a stale heartbeat and restart the pod,
            # which is the right response to a volume that stopped writing.
            log(f"could not write heartbeat: {exc}")

    def run_once(self, timeout: int = LONG_POLL_SECONDS) -> None:
        """One iteration. Pending decisions are retried even when Telegram is
        unreachable — a Linear outage and a Telegram outage are independent.
        A transport error is re-raised after that, for run() to back off on."""
        self.beat()
        error: Exception | None = None
        try:
            self.drain(timeout)
        except TelegramConflict:
            raise
        except TRANSIENT as exc:
            error = exc
        if not self.stopping:
            self.gk.retry_pending()
        self.beat()
        if error is not None:
            raise error

    def moved_to(self) -> str | None:
        """The new main SHA if it differs from the one this started on, else None."""
        if not (self.start_sha and self.ls_remote):
            return None
        try:
            remote = self.ls_remote()
        except Exception as exc:  # noqa: BLE001 — GitHub down is not a reason to stop listening
            log(f"self-update check failed, carrying on: {exc}")
            return None
        return remote if remote and remote != self.start_sha else None

    def self_update(self, remote: str) -> None:
        """Fetch and reset the clone, then re-exec in place. Only returns if the
        update failed, in which case the current code keeps running and the
        next check tries again."""
        old = (self.start_sha or "?")[:7]
        if not self.fetch_reset:
            return
        try:
            new = self.fetch_reset()
        except Exception as exc:  # noqa: BLE001 — a failed update must not stop the reader
            log(f"self-update {old} -> {remote[:7]} failed, still running {old}: {exc}")
            return
        if self.stopping:
            return
        log(f"dotfiles {old} -> {(new or remote)[:7]}: re-executing on the new code")
        try:
            self.reexec()
        except OSError as exc:
            log(f"re-exec failed, still running {old} (the clone is already at {(new or remote)[:7]}): {exc}")

    def request_stop(self, *_: object) -> None:
        """SIGTERM/SIGINT handler. Interrupts a long poll or a backoff sleep;
        anything else finishes the update in hand first."""
        self.stopping = True
        if self._waiting:
            raise _Stop()

    def _wait(self, seconds: float) -> None:
        self._waiting = True
        try:
            self.sleep(seconds)
        finally:
            self._waiting = False

    def run(self, timeout: int = LONG_POLL_SECONDS) -> int:
        backoff = 0.0
        next_update_check = self.clock() + self.update_every
        sha = self.start_sha[:7] if self.start_sha else "sha unknown"
        log(f"listening (dotfiles {sha}, offset {self._offset()})")
        try:
            while not self.stopping:
                try:
                    self.run_once(timeout)
                    backoff = 0.0
                except TelegramConflict as exc:
                    log(f"{exc} Exiting {EXIT_CONFLICT}.")
                    return EXIT_CONFLICT
                except TRANSIENT as exc:
                    backoff = min(max(backoff * 2, BACKOFF_START_SECONDS), BACKOFF_MAX_SECONDS)
                    delay = backoff * random.uniform(0.9, 1.1)
                    log(f"{exc} — backing off {delay:.0f}s")
                    self._wait(delay)
                    continue
                if self.clock() >= next_update_check:
                    next_update_check = self.clock() + self.update_every
                    remote = self.moved_to()
                    if remote:
                        # Between batches: everything handled is acked, so the
                        # re-executed process starts from the saved offset.
                        self.self_update(remote)
        except _Stop:
            pass
        log("stopping")
        return 0


# ------------------------------------------------------------------ wiring


def _reexec() -> None:
    """Replace this process with a fresh listen.py on the updated clone. Same
    pid (still PID 1 in the pod), same argv, same environment — so env.sh's
    exports carry over without re-sourcing it."""
    sys.stdout.flush()
    sys.stderr.flush()
    script = str(Path(__file__).resolve())
    os.execv(sys.executable, [sys.executable, script, *sys.argv[1:]])


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30, check=True)
    return out.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="listen.py", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo",
        default=None,
        help="the dotfiles clone this runs from, for self-update (default: detected; none = no self-update)",
    )
    parser.add_argument("--ref", default="main")
    parser.add_argument("--timeout", type=int, default=LONG_POLL_SECONDS, help="getUpdates long-poll seconds")
    args = parser.parse_args(argv)

    gk = gate.gatekeeper_from_env()

    # HERE is <clone>/dot_claude/skills/gatekeeper/scripts. Run from a deployed
    # ~/.claude instead and there is no clone to compare, so no self-update.
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

    def fetch_reset() -> str:
        _git(repo, "fetch", "--quiet", "--depth", "1", "origin", args.ref)
        _git(repo, "reset", "--quiet", "--hard", "FETCH_HEAD")
        return _git(repo, "rev-parse", "HEAD")

    listener = Listener(
        gk,
        start_sha=start_sha,
        ls_remote=ls_remote if start_sha else None,
        fetch_reset=fetch_reset if start_sha else None,
    )
    # PID 1 in the container gets no default SIGTERM handling, so without this
    # the pod would sit out its whole grace period and be killed.
    signal.signal(signal.SIGTERM, listener.request_stop)
    signal.signal(signal.SIGINT, listener.request_stop)
    return listener.run(args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
