#!/usr/bin/env bash
# PermissionRequest hook for the 14.24 yolo-off spike.
#
# This is the hook under test. In a real "yolo off" operator build it
# would bridge the permission question into meeting chat and block for a
# human reply. Here it stands in for that, with behaviour selected by
# the $PERMREQ_MODE env var the driver sets per test:
#
#   allow       — log the firing, return decision.behavior=allow at once.
#   block_allow — log the firing, write a request line to
#                 state/permreq_requests.jsonl, then BLOCK polling for
#                 state/permreq_answer.json (the driver writes it after a
#                 simulated "human reply" delay). Returns the answered
#                 behaviour. This is the real operator round-trip in
#                 miniature — it proves a synchronous, blocking hook
#                 resolves the dialog without hanging the TUI.
#   deny_json   — log the firing, return decision.behavior=deny + message.
#   deny_exit2  — log the firing, exit 2 (the fail-safe deny path).
#
# REGARDLESS of mode it always appends the firing (full hook input, so
# tool_name + tool_input are captured) to state/permreq_events.jsonl —
# that file being non-empty is the driver's proof that PermissionRequest
# fired at all in interactive PTY mode.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="$DIR/state"
mkdir -p "$STATE"

INPUT="$(cat)"
TS="$(python3 -c 'import time; print(f"{time.time():.3f}")')"
printf '{"ts": %s, "input": %s}\n' "$TS" "$INPUT" >> "$STATE/permreq_events.jsonl"

MODE="${PERMREQ_MODE:-allow}"

emit_allow() {
  python3 -c 'import json; print(json.dumps({"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow"}}}))'
}
emit_deny() {
  python3 -c 'import json,sys; print(json.dumps({"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"deny","message":sys.argv[1]}}}))' "$1"
}

case "$MODE" in
  allow)
    emit_allow
    exit 0
    ;;
  deny_json)
    emit_deny "denied by spike (deny_json mode)"
    exit 0
    ;;
  deny_exit2)
    echo "permreq spike: denying via exit 2 (fail-safe path)" >&2
    exit 2
    ;;
  block_allow)
    # Simulate operator's chat round-trip: announce the request, then
    # block until the driver writes an answer (or a ceiling is hit).
    TOOL="$(printf '%s' "$INPUT" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_name",""))')"
    python3 -c 'import json,sys; print(json.dumps({"ts": float(sys.argv[1]), "tool_name": sys.argv[2]}))' \
      "$TS" "$TOOL" >> "$STATE/permreq_requests.jsonl"
    ANSWER="$STATE/permreq_answer.json"
    # ~20s ceiling at 0.2s/iter — generous; a real chat reply is faster,
    # and the hook's own command timeout (600s default) is the real cap.
    for _ in $(seq 1 100); do
      if [[ -f "$ANSWER" ]]; then
        BEHAVIOR="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("behavior","deny"))' "$ANSWER")"
        if [[ "$BEHAVIOR" == "allow" ]]; then
          emit_allow
        else
          emit_deny "denied via simulated chat round-trip"
        fi
        exit 0
      fi
      sleep 0.2
    done
    echo "permreq spike: round-trip timed out — fail-safe deny" >&2
    exit 2
    ;;
  *)
    echo "permreq spike: unknown PERMREQ_MODE=$MODE" >&2
    exit 2
    ;;
esac
