#!/usr/bin/env bash
# PreToolUse hook for the v2 spike — fires on every tool-call attempt
# (regardless of permission outcome). Logging this gives the driver a
# permission-independent count of how many times claude tried to use a
# tool this turn. PreToolUse count == 1 means claude tried once and
# (after our deny) gave up; count >= 2 means claude retried.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="$DIR/state"
mkdir -p "$STATE" 2>/dev/null
INPUT="$(cat)"
TS="$(python3 -c 'import time; print(f"{time.time():.3f}")')"
{ printf '{"ts": %s, "kind": "pretool", "input": %s}\n' "$TS" "$INPUT" >> "$STATE/tool_events.jsonl"; } 2>/dev/null
exit 0
