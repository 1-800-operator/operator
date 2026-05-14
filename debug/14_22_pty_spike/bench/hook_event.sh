#!/usr/bin/env bash
# Generic event logger. Reads hook JSON on stdin, appends to
# state/events.jsonl keyed by the event's hook_event_name. Used for
# PostToolUseFailure, StopFailure, PermissionDenied — anything where
# we want a record without per-event handler logic.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="$DIR/state"
mkdir -p "$STATE"

INPUT="$(cat)"
TS="$(python3 -c 'import time; print(f"{time.time():.3f}")')"

printf '{"ts": %s, "input": %s}\n' "$TS" "$INPUT" >> "$STATE/events.jsonl"
exit 0
