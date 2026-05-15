#!/usr/bin/env bash
# Stop hook for the v2 spike. Appends the hook payload as JSONL so the
# driver knows the turn ended and can pull last_assistant_message.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="$DIR/state"
mkdir -p "$STATE" 2>/dev/null
INPUT="$(cat)"
TS="$(python3 -c 'import time; print(f"{time.time():.3f}")')"
{ printf '{"ts": %s, "kind": "stop", "input": %s}\n' "$TS" "$INPUT" >> "$STATE/replies.jsonl"; } 2>/dev/null
exit 0
