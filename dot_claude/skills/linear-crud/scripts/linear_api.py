"""Shared Linear GraphQL access for the python helpers (SB-923).

One place for the two things every script was re-implementing: calling the
sibling linear-gql.sh (which owns the key and never prints it) and warning when
a page came back full. Import with the scripts dir on sys.path:

    sys.path.insert(0, str(Path.home() / ".claude/skills/linear-crud/scripts"))
    from linear_api import gql, warn_if_capped
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _gql_script() -> Path:
    """Deployed it is linear-gql.sh; in the chezmoi source tree it carries the
    executable_ prefix. Resolve so both work (mirrors sibling() in linear.sh)."""
    for name in ("linear-gql.sh", "executable_linear-gql.sh"):
        p = HERE / name
        if p.exists():
            return p
    sys.exit(f"linear_api: linear-gql.sh not found next to {HERE} (run doctor.sh)")


def gql(query: str, variables: dict | None = None) -> dict:
    """Run a query/mutation and return payload["data"].

    A non-zero exit from the wrapper becomes a one-line SystemExit carrying
    its stderr — never a CalledProcessError, whose repr would echo the query.
    GraphQL-level errors in an otherwise-200 payload also exit with the
    messages."""
    cmd = ["bash", str(_gql_script()), query]
    if variables is not None:
        cmd.append(json.dumps(variables))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        err = r.stderr.strip()
        prefix = "" if err.startswith("linear-gql:") else "linear-gql: "
        raise SystemExit(f"{prefix}{err}")
    try:
        payload = json.loads(r.stdout)
    except ValueError:
        raise SystemExit("linear-gql: non-JSON response from Linear") from None
    if payload.get("errors"):
        msgs = "; ".join(e.get("message", str(e)) for e in payload["errors"])
        raise SystemExit(f"Linear API error: {msgs}")
    return payload["data"]


def warn_if_capped(nodes: list, cap: int, what: str) -> None:
    """A full page is indistinguishable from a truncated one; say so."""
    if len(nodes) >= cap:
        print(f"warn: {what} hit the first:{cap} cap; results may be truncated", file=sys.stderr)
