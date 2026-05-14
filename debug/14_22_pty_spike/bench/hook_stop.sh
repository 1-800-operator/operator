#!/usr/bin/env bash
# Stop hook for the 14.22 send-keys vs Stop-block comparison spike.
#
# Always: appends {ts, input} as JSONL to state/replies.jsonl so the
# driver can pick up `last_assistant_message`.
#
# When SPIKE_MODE=stopblock: also reads state/inbox.jsonl. If a message
# is queued, pops the first line and returns {"decision":"block",
# "reason":<message>} so claude continues with that as its next turn.
# When the inbox is empty, the hook returns nothing → claude is allowed
# to stop and the session ends.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="$DIR/state"
mkdir -p "$STATE"

INPUT="$(cat)"
TS="$(python3 -c 'import time; print(f"{time.time():.3f}")')"

printf '{"ts": %s, "kind": "stop", "input": %s}\n' "$TS" "$INPUT" >> "$STATE/replies.jsonl"

if [[ "${SPIKE_MODE:-sendkeys}" == "stopblock" ]]; then
    INBOX="$STATE/inbox.jsonl"
    if [[ -f "$INBOX" && -s "$INBOX" ]]; then
        FIRST="$(head -n1 "$INBOX")"
        tail -n +2 "$INBOX" > "$INBOX.tmp" && mv "$INBOX.tmp" "$INBOX"
        MSG="$(printf '%s' "$FIRST" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["message"])')"
        python3 -c 'import json,sys; print(json.dumps({"decision":"block","reason":sys.argv[1]}))' "$MSG"
        exit 0
    fi
fi
exit 0
