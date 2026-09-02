# The image the cycle-runner CronJob runs (SB-975, deployed by SB-976).
#
# Everything is pinned by version AND by sha256, the same standard ci.yml holds
# its own tool downloads to. A release asset swapped underneath us would run
# with a Claude OAuth token and a GitHub PAT in its environment.
#
# The other half of "pinned" is claude-cli-contract.sh, run as a build step
# below. Pinning alone only defers the breakage to the day someone bumps the
# version; the contract is what makes that bump fail loudly instead of at 2am
# in a run log. `doctor.sh` already records that 2.1.251 has no `--max-turns` —
# that flag was assumed once and the run failed.
#
# ---------------------------------------------------------------- no `op`
#
# There is deliberately NO 1Password CLI in this image.
#
# SB-974 already took `op` off the tick path: run.sh reads its secrets as files
# under $CYCLE_RUNNER_SECRETS_DIR, because once a desktop account exists in
# ~/.config/op/config, `op read` prefers the desktop app and raises a Touch ID
# prompt even with a valid service-account token — proven with a logging shim,
# one call wedged for twelve minutes, and a wedged `op` stops the runner
# entirely (SB-868, SB-953, SB-972).
#
# In the pod those files come from a Secret. With no binary here that class of
# failure is not fixed, it is unreachable — which is why claude-cli-contract.sh
# asserts `command -v op` FAILS.
#
# ------------------------------------------------------------- arm64 only
#
# The cluster is rancher-desktop on Lima on an Apple-silicon Mac. Building the
# amd64 leg would double the build for an architecture nothing runs, and would
# need a second set of checksums that nothing verifies in practice. The arch
# guard below fails with that reason rather than fetching a 404.

FROM debian:bookworm-20250811-slim

# One place per tool: version and the sha256 of the asset that version names.
ARG NODE_VERSION=22.23.2
ARG NODE_SHA256_ARM64=fff4078c5def658577f92c88db7db3bc0072924bfb93fe52c1e744a54e94abb8
ARG CLAUDE_VERSION=2.1.258
ARG GH_VERSION=2.97.0
ARG GH_SHA256_ARM64=73ea440ecad9c9e284429997ee6f93577bc6f7bc6fba357ef62c53ad8fb641a5
ARG GITLEAKS_VERSION=8.30.1
ARG GITLEAKS_SHA256_ARM64=e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    npm_config_update_notifier=false

RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    if [ "$arch" != "arm64" ]; then \
      echo "cycle-runner is built for arm64 only (the k3s node is Lima on Apple silicon); got $arch" >&2; \
      exit 1; \
    fi

# ca-certificates and curl fetch the pinned tarballs below and are used by
# `git`/`gh` at runtime. python3 and jq are called directly by run.sh (pick.py,
# triage.py, and every `jq` on the summary path). ripgrep is what claude's
# search tools shell out to.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      git \
      jq \
      python3 \
      python3-venv \
      ripgrep \
      tini \
      xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Node from the nodejs.org tarball rather than the apt repo: an exact version,
# and one artefact to checksum. The apt repo floats within a major, which is
# the thing this image exists not to do.
RUN set -eux; \
    curl -fsSL -o /tmp/node.tar.xz \
      "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-arm64.tar.xz"; \
    echo "${NODE_SHA256_ARM64}  /tmp/node.tar.xz" | sha256sum -c -; \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
      --exclude CHANGELOG.md --exclude LICENSE --exclude README.md; \
    rm /tmp/node.tar.xz

RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_VERSION}" \
    && npm cache clean --force

RUN set -eux; \
    curl -fsSL -o /tmp/gh.tar.gz \
      "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_arm64.tar.gz"; \
    echo "${GH_SHA256_ARM64}  /tmp/gh.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/gh.tar.gz -C /tmp; \
    install -m 0755 "/tmp/gh_${GH_VERSION}_linux_arm64/bin/gh" /usr/local/bin/gh; \
    rm -rf /tmp/gh.tar.gz "/tmp/gh_${GH_VERSION}_linux_arm64"

# gitleaks is load-bearing, not optional. SB-943 made the run-log secret scan
# fail CLOSED — a missing scanner now blocks the Telegram summary rather than
# silently disabling the scan — so an image without it produces a runner that
# can never report anything at all.
RUN set -eux; \
    curl -fsSL -o /tmp/gitleaks.tar.gz \
      "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_arm64.tar.gz"; \
    echo "${GITLEAKS_SHA256_ARM64}  /tmp/gitleaks.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/gitleaks.tar.gz -C /tmp gitleaks; \
    install -m 0755 /tmp/gitleaks /usr/local/bin/gitleaks; \
    rm -f /tmp/gitleaks.tar.gz /tmp/gitleaks

# Every version in one file in the image, so a run log can be correlated with
# an exact build after the fact (SB-978) instead of re-derived from the tag.
RUN set -eux; \
    { \
      echo "claude=$(claude --version | awk '{print $1}')"; \
      echo "node=$(node --version)"; \
      echo "gh=$(gh --version | head -1 | awk '{print $3}')"; \
      echo "gitleaks=$(gitleaks version)"; \
      echo "python3=$(python3 --version | awk '{print $2}')"; \
      echo "jq=$(jq --version)"; \
      echo "git=$(git --version | awk '{print $3}')"; \
    } >/etc/cycle-runner-versions; \
    cat /etc/cycle-runner-versions

# The build fails HERE, not at 2am in a run log, when the CLI surface moves.
COPY k3s/cycle-runner/claude-cli-contract.sh /usr/local/bin/claude-cli-contract
RUN chmod 0755 /usr/local/bin/claude-cli-contract && claude-cli-contract

# tini reaps what `claude` spawns. Without a reaping pid 1 a pod accumulates
# zombies for the whole activeDeadlineSeconds window.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["claude-cli-contract"]
