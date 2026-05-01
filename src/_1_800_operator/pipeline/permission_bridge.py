"""
PreToolUse hook bridge — invoked by claude on every tool_use.

Lifecycle: claude spawns one bridge process per PreToolUse event. The
bridge gets the tool_use payload on stdin, writes it to the request
named-pipe (where the operator parent's pump thread is listening),
blocks reading the response pipe, and then prints the decision JSON to
stdout in the shape claude expects.

CLI:
    python -m _1_800_operator.pipeline.permission_bridge <req_pipe> <resp_pipe>

stdin: Claude Code's PreToolUse JSON, e.g.
    {"tool_name": "Write", "tool_input": {"file_path": "...", "content": "..."},
     "tool_use_id": "...", ...}

stdout (on exit 0): the hookSpecificOutput envelope claude expects:
    {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow" | "deny" | "ask",
        "permissionDecisionReason": "..."}}

The parent (operator) sends back the inner decision dict (without
`hookSpecificOutput` wrapping) so its handler stays focused on the
permission semantics; the bridge handles the envelope shape.

Failure modes:
  - Pipes missing: print deny, exit 0 (claude still sees a clean decision).
  - JSON parse errors on the wire: deny with parse reason.
  - Pipe write/read errors: deny with the underlying exception.

Hard-fail (exit 2) is reserved for cases where we want claude to see the
error directly; here we always emit a clean JSON allow/deny so claude's
control flow stays predictable.

Upper-bound timeout: the bridge has no Python-side watchdog. Pipe opens
and reads block indefinitely under POSIX semantics, so the ceiling on
how long this process can hang is governed by claude's own PreToolUse
hook timeout (set to 600s in `claude_cli._setup_permission_bridge`'s
settings.json `hooks.PreToolUse[].hooks[].timeout`). The CLI kills the
hook process and treats the absent decision as a deny when that fires.
"""
import json
import sys
from pathlib import Path


def _emit(decision: str, reason: str) -> None:
    """Print the hookSpecificOutput envelope claude expects, exit 0."""
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


def main(argv):
    if len(argv) < 3:
        _emit("deny", f"permission bridge misconfigured: expected 2 args, got {len(argv)-1}")
        return 0

    req_pipe = Path(argv[1])
    resp_pipe = Path(argv[2])

    if not req_pipe.exists() or not resp_pipe.exists():
        _emit(
            "deny",
            f"permission bridge: pipe(s) missing (req={req_pipe.exists()}, resp={resp_pipe.exists()})",
        )
        return 0

    try:
        payload = sys.stdin.read()
    except Exception as e:
        _emit("deny", f"permission bridge: stdin read failed: {e}")
        return 0

    # Forward the raw payload to parent. Parent's pump expects exactly one
    # newline-terminated JSON blob per write; send as-is + a trailing
    # newline if missing.
    if not payload.endswith("\n"):
        payload = payload + "\n"

    try:
        with open(req_pipe, "w") as fw:
            fw.write(payload)
    except Exception as e:
        _emit("deny", f"permission bridge: req-pipe write failed: {e}")
        return 0

    try:
        with open(resp_pipe, "r") as fr:
            response = fr.read()
    except Exception as e:
        _emit("deny", f"permission bridge: resp-pipe read failed: {e}")
        return 0

    if not response.strip():
        _emit("deny", "permission bridge: parent returned an empty response")
        return 0

    try:
        inner = json.loads(response)
    except json.JSONDecodeError as e:
        _emit("deny", f"permission bridge: parent response was not valid JSON: {e}")
        return 0

    decision = inner.get("permissionDecision", "deny")
    if decision not in ("allow", "deny", "ask"):
        _emit("deny", f"permission bridge: unknown decision {decision!r} from parent")
        return 0

    reason = inner.get("permissionDecisionReason", "")
    _emit(decision, reason)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
