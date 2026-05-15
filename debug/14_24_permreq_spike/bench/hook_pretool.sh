#!/usr/bin/env bash
# PreToolUse hook for the 14.24 PermissionRequest spike.
# Logs every tool-call attempt to state/tool_events.jsonl. This is a
# permission-INDEPENDENT signal that claude tried to use a tool — it
# fires "before tool execution regardless of permission status"
# (hooks.md), so the driver can tell "claude attempted a tool" apart
# from "PermissionRequest fired".

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="$DIR/state"
mkdir -p "$STATE"

INPUT="$(cat)"
TS="$(python3 -c 'import time; print(f"{time.time():.3f}")')"

printf '{"ts": %s, "kind": "pretool", "input": %s}\n' "$TS" "$INPUT" >> "$STATE/tool_events.jsonl"
exit 0
