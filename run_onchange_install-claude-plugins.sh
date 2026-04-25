#!/bin/bash
set -euo pipefail

if ! command -v claude &> /dev/null; then
  echo "claude CLI not found, skipping plugin install"
  exit 0
fi

PLUGINS=(
  "JuliusBrussee/caveman caveman@caveman"
)

for entry in "${PLUGINS[@]}"; do
  read -r repo plugin <<< "$entry"
  marketplace="${plugin#*@}"
  plugin_name="${plugin%@*}"

  if ! claude plugin marketplace list 2>/dev/null | grep -q "$marketplace"; then
    echo "Adding marketplace: $repo"
    claude plugin marketplace add "$repo"
  fi

  if ! claude plugin list 2>/dev/null | grep -q "$plugin_name"; then
    echo "Installing plugin: $plugin"
    claude plugin install "$plugin"
  fi
done
