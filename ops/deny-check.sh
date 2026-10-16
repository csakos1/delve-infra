#!/bin/sh
# Run the deterministic deny-list matrix (tests/deny-matrix.py) as the agent user,
# with the Hermes Python, against the live Hermes approval code.
# Run from the operator account (needs sudo for machinectl). The agent user cannot
# read the operator's home, so the test is handed over through /tmp and removed after.
set -eu

REPO=$(cd "$(dirname "$0")/.." && pwd)
TMP=$(mktemp /tmp/deny-matrix.XXXXXX)
trap 'rm -f "$TMP"' EXIT INT TERM
command cp -f "$REPO/tests/deny-matrix.py" "$TMP"
chmod 644 "$TMP"

sudo machinectl shell agent@ /usr/bin/bash -lc "
  PY=\$(sed -n 2p ~/.hermes/hermes-agent/.hermes/bin/hermes | awk '{print \$2}')
  \$PY -I $TMP; echo exit=\$?
" 2>&1 | grep -v 'local host'
