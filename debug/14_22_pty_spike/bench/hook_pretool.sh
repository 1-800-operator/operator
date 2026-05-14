#!/usr/bin/env bash
# PreToolUse hook for the 14.22 spike. Just logs the tool name +
# input so we can confirm tool-use events fire cleanly through hooks.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="$DIR/state"
mkdir -p "$STATE"

INPUT="$(cat)"
TS="$(python3 -c 'import time; print(f"{time.time():.3f}")')"

printf '{"ts": %s, "kind": "pretool", "input": %s}\n' "$TS" "$INPUT" >> "$STATE/tool_events.jsonl"
exit 0
