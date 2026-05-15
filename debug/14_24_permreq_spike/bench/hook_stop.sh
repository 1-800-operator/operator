#!/usr/bin/env bash
# Stop hook for the 14.24 PermissionRequest spike.
# Appends {ts, kind, input} as JSONL to state/replies.jsonl so the driver
# knows the turn completed (and can read last_assistant_message).

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="$DIR/state"
mkdir -p "$STATE"

INPUT="$(cat)"
TS="$(python3 -c 'import time; print(f"{time.time():.3f}")')"

printf '{"ts": %s, "kind": "stop", "input": %s}\n' "$TS" "$INPUT" >> "$STATE/replies.jsonl"
exit 0
