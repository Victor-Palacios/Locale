#!/bin/bash
# Local runner for the Locale ETA tracker, intended for cron (every 5 min).
# Loads secrets from .env, uses an independent state file under ~/.locale_tracker
# so it never touches the git-tracked state the GitHub Action manages.
set -uo pipefail

REPO="/Users/victor/Desktop/Cursor/Locale"
DATA="$HOME/.locale_tracker"
LOG="$DATA/run.log"

mkdir -p "$DATA"
cd "$REPO" || exit 1

# Load secrets (TRACK_URL, GMAIL_USER, GMAIL_APP_PASSWORD, SMS_TO).
set -a
# shellcheck disable=SC1091
. "$REPO/.env"
set +a

export STATE_FILE="$DATA/state.json"

{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') ====="
  "$REPO/locale_venv/bin/python" "$REPO/track.py"
  echo "exit=$?"
} >> "$LOG" 2>&1
