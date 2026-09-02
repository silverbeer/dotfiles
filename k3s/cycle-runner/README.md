# cycle-runner in k3s

The cycle-runner runs as a CronJob every 30 minutes (SB-976), on the
rancher-desktop cluster. This directory holds the image it runs (SB-975) and
the manifests that schedule it.

## Deploy

```bash
kubectl apply -f k3s/cycle-runner/namespace.yaml
bash k3s/cycle-runner/provision-cluster-secret.sh   # reads the mini's files, not 1Password
kubectl apply -f k3s/cycle-runner/pvc.yaml -f k3s/cycle-runner/cronjob.yaml
```

The Secret carries five keys. Three are read as **files** by `env.sh` in both
the cycle-runner and gatekeeper skills, via `CYCLE_RUNNER_SECRETS_DIR=/secrets`
— `claude-token`, `telegram-token`, `telegram-chat-id`. Two are read as
**environment variables**, because `~/.zshenv` exported them on the mini and
there is no zsh in a pod — `linear-api-key`, `gh-token`.

`provision-cluster-secret.sh` never calls `op`. The three cycle-runner secrets
are already on disk from `provision-secrets.sh`; this copies them in. Re-run it
after any rotation — a CronJob reads the Secret at pod start, so the next tick
picks it up.

## What the scheduler replaced

| `run.sh` before | now |
| -- | -- |
| `acquire_lock` — mkdir-atomic, pid file, staleness grace, race window | `concurrencyPolicy: Forbid` |
| nothing; SB-965's unbounded `claude -p` held the lock 10.5h | `activeDeadlineSeconds: 1500` |
| launchd catch-up, "whatever it feels like" | `startingDeadlineSeconds: 300` |
| files a human provisioned on one machine | a Secret |

The lock was **removed**, not disabled. Two mechanisms that can disagree about
whether a run is in progress is the shape behind SB-949 and SB-952.
`.github/scripts/check-k3s-manifests.sh` fails the build if any of those YAML
lines goes missing, and `check-cycle-runner-policy.sh` fails it if the lock
comes back.

## The pod's $HOME is the PVC

`/data/home`, on `cycle-runner-home`. Four things live there and all four have
to persist:

| | |
| -- | -- |
| `.local/state/cycle-runner/` | gate JSON, run logs, `runs/<id>/pr_url`, worktrees |
| `gitrepos/<repo>/` | the primary clones worktrees are cut from |
| `.claude/` | skills, commands, agents — re-synced every tick |
| `.gitconfig` | identity and the `gh` credential helper |

`repo_dir_for_label` resolves `$HOME/gitrepos/<glob>`, so without the clones
there is nothing to cut a worktree from. `bootstrap.sh` (the initContainer)
creates any that are missing, blobless, and is a no-op afterwards.

**The skills are not baked into the image.** The image is the toolchain and
moves weekly at most (SB-978); the skills move several times a day and are what
the runner delivers. `bootstrap.sh` clones `dotfiles@main` on every tick, so a
tick always runs the merged code.

## What this does NOT buy

Availability. The cluster is rancher-desktop on Lima — a VM on the same Mac.
Mac down, k3s down. Same blast radius as launchd; this buys scheduling
semantics and isolation.

## Keeping `claude` current

The CronJob is pinned to `ghcr.io/silverbeer/cycle-runner:claude-<version>`,
never `:latest`. `:latest` with `imagePullPolicy: Always` means a rebuild lands
in the next tick with nobody having looked at it, and leaves nothing to revert
*to*. `check-k3s-manifests.sh` fails the build if the tag and the Dockerfile's
`ARG CLAUDE_VERSION` disagree.

`.github/workflows/cycle-runner-image-refresh.yml` runs weekly (Mon 07:00 UTC,
or on demand):

1. compare `npm view @anthropic-ai/claude-code version` against the pin — equal, stop
2. build the candidate; `claude-cli-contract` runs as a build step, so a
   renamed flag fails here
3. run the offline suite **inside the candidate** — `check-cycle-runner-policy`,
   `check-gatekeeper`, `check-linear-crud` — against the new CLI. Running them
   on the runner's own filesystem would prove the repo is fine, which nobody
   doubted; the question is whether the scripts still work against the new CLI
4. only then push `:claude-<new>` and open a **PR**

The cluster does not move until that PR merges. Reverting is repointing one
line; the old tag is never deleted.

`doctor.sh` reads the tag and compares it with the machine's own `claude`,
because after this migration there are two runtimes: the image's, and the one
`/work` and `/ticket` use interactively. A behaviour difference between a
headless tick and an interactive run is otherwise unattributable.

## The image

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
