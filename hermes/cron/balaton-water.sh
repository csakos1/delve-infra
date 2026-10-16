#!/bin/sh
# Create the balaton-water cron job (no-agent water-level script) if it does not exist.
# Run as the agent user. Idempotent: an existing job with the same name is left untouched.
# The job state itself (~/.hermes/cron/jobs.json) is host-specific and not versioned.
set -eu

NAME=balaton-water
SCHEDULE="52 8,12,20 * * *"
SCRIPT=water_level.py
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

command -v hermes >/dev/null 2>&1 || { echo "hermes not in PATH" >&2; exit 2; }
[ -f "$HERMES_HOME/scripts/$SCRIPT" ] || { echo "missing $HERMES_HOME/scripts/$SCRIPT" >&2; exit 2; }

# Capture first: if listing fails we must not fall through to create a duplicate.
if ! LIST=$(hermes cron list 2>&1); then
    echo "hermes cron list failed:" >&2
    printf '%s\n' "$LIST" >&2
    exit 2
fi

if printf '%s\n' "$LIST" | grep -qE "^[[:space:]]*Name:[[:space:]]+$NAME[[:space:]]*$"; then
    echo "$NAME: already exists, nothing to do"
    exit 0
fi

hermes cron create --name "$NAME" --no-agent --script "$SCRIPT" \
    --deliver local --failure-deliver local "$SCHEDULE"
