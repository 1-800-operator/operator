#!/usr/bin/env bash
# PermissionRequest hook for the v2 spike — tests whether claude
# correctly interprets the user's verbatim chat reply when handed to
# it via the deny `message` field, and whether it retries (or not)
# appropriately.
#
# Per-spawn behaviour:
#   - First invocation: deny with `message="user said: <REPLY>"`.
#     <REPLY> comes from $PERMREQ_TEST_REPLY — the driver sets this
#     per scenario. This stands in for "operator handed the user's
#     verbatim chat reply to claude via the only field the
#     PermissionRequest hook contract gives us."
#   - Subsequent invocations (claude retried): allow, so the retry
#     completes and the turn ends. This mirrors the planned operator
#     behaviour of auto-allowing same-call retries within a turn.
#
# Per-spawn state: state/permreq_counter (driver removes it between
# scenarios). All output discipline follows the operator hook contract
# — exit 0, JSON on stdout, never bare non-zero, never exit 2.

set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="$DIR/state"
mkdir -p "$STATE" 2>/dev/null

INPUT="$(cat)"
TS="$(python3 -c 'import time; print(f"{time.time():.3f}")')"
{ printf '{"ts": %s, "input": %s}\n' "$TS" "$INPUT" >> "$STATE/permreq_events.jsonl"; } 2>/dev/null

REPLY="${PERMREQ_TEST_REPLY:-yes}"
COUNTER="$STATE/permreq_counter"

if [[ ! -f "$COUNTER" ]]; then
    { printf '1' > "$COUNTER"; } 2>/dev/null
    python3 -c '
import json, sys
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PermissionRequest",
    "decision": {"behavior": "deny", "message": "user said: " + sys.argv[1]}
}}))
' "$REPLY"
    exit 0
fi

# Subsequent calls (the retry, if any) — allow so the tool runs and
# Stop fires. The driver's verdict logic distinguishes "retry exists"
# from "retry was the same call vs modified args."
python3 -c '
import json
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PermissionRequest",
    "decision": {"behavior": "allow"}
}}))
'
exit 0
