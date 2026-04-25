#!/bin/bash
set -euo pipefail

if command -v uv &>/dev/null; then
  echo "uv already installed: $(uv --version)"
  exit 0
fi

echo "Installing uv from astral.sh"
curl -LsSf https://astral.sh/uv/install.sh | sh
