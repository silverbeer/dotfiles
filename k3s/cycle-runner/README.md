# cycle-runner image

The container the cycle-runner CronJob runs (SB-975). The CronJob, Secret and
PVC that use it are SB-976; nothing here is applied to the cluster.

```bash
# from the repo root — the build context is the repo, not this directory
docker build -f k3s/cycle-runner/Dockerfile -t cycle-runner:dev .
docker run --rm cycle-runner:dev cat /etc/cycle-runner-versions
```

## What is in it

`claude`, `git`, `gh`, `node`, `python3`, `jq`, `gitleaks`, `ripgrep` — every
binary `run.sh` checks for on startup, plus the one `claude`'s search tools
shell out to. All pinned by version *and* sha256; `arm64` only, because the
k3s node is rancher-desktop on Lima on Apple silicon.

`gitleaks` is not optional. SB-943 made the run-log secret scan fail closed, so
an image without it produces a runner that can never post a summary at all.

## What is deliberately not in it

`op`. SB-974 took 1Password off the tick path — the runner reads its secrets as
files, because `op read` prefers the desktop app once a desktop account exists
and raises a Touch ID prompt that wedges an unattended run (SB-868, SB-953,
SB-972). In the pod those files come from a Secret. With no binary here the
whole class is unreachable rather than merely fixed, and
`claude-cli-contract.sh` asserts `command -v op` fails.

## The CLI contract

`claude-cli-contract.sh` runs as a **build step**. It asserts every flag
`run.sh` and `triage-run.sh` pass to `claude` still exists in `claude --help`,
so a renamed or removed flag fails the build instead of a 2am tick. `--max-turns`
is deliberately absent from the list: it does not exist, was assumed once, and
the run failed — `doctor.sh` carries the same note.

`.github/scripts/check-claude-cli-contract.sh` closes the loop from the other
side, in ci.yml: it greps the runner for the flags it passes and fails if one
is missing from `REQUIRED_FLAGS`. The build cannot see that direction.

Bumping `CLAUDE_VERSION` is what SB-978 automates, weekly, behind a green
suite and a PR.
