"""Probe 9 — CLI path: ask-decision semantics.

Question: when our PreToolUse hook returns permissionDecision: "ask" with
a synthesized reason, what does inner-claude actually do under headless
`claude -p` (no tty)?

This is the load-bearing unknown for Phase 14.19.8 mechanism 1: if
`claude -p` honors `ask` by surfacing the reason somewhere we can capture
(a stream event, the result text), we can route the question into Meet
chat and use the existing await-reply round trip. If it silently denies,
hangs, or errors, mech 1 is dead and we fall back to mech 2 (LLM authors
proposal in its own reply stream and we stall the tool call).

Test: ask inner-claude to write a file. The hook's first call returns
ask; subsequent calls allow (so we can see what claude does after a
hypothetical user-affirmative). Observe exit code, stream events, and
the result envelope.

Plausible behaviors to disambiguate:
  A. claude treats ask as deny silently — same downstream behavior as
     probe 3, no surfacing of the reason. Mech 1 dead.
  B. claude emits a new stream event (some kind of permission_request)
     carrying the reason. Mech 1 viable — wire chat surfacing to that
     event, feed user reply back via... some mechanism TBD.
  C. claude blocks the tool indefinitely waiting for an out-of-band
     answer. Mech 1 dead unless there's a documented "answer this ask"
     stdin envelope.
  D. claude errors / aborts the turn. Mech 1 dead.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRIDGE = HERE / "perm_bridge.sh"


def main():
    tmp = tempfile.mkdtemp(prefix="claude-perm-spike-ask-")
    req_pipe = Path(tmp) / "request.pipe"
    resp_pipe = Path(tmp) / "response.pipe"
    os.mkfifo(req_pipe, 0o600)
    os.mkfifo(resp_pipe, 0o600)

    settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{BRIDGE} {req_pipe} {resp_pipe}",
                            "timeout": 60,
                        }
                    ],
                }
            ]
        }
    }
    settings_path = Path(tmp) / "settings.json"
    settings_path.write_text(json.dumps(settings))

    target = Path(tmp) / "ask_target.txt"
    task = (
        f"Use the Write tool to create the file {target} "
        f"with the contents 'ask_payload'. "
        f"If the write fails or is blocked, just report the failure verbatim. "
        f"Do NOT retry. Do NOT pivot to a different approach."
    )

    cmd = [
        "claude", "-p", task,
        "--settings", str(settings_path),
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--permission-mode", "default",
    ]

    print("[parent] spawning ask probe — first hook call returns ask, subsequent allow")
    t_start = time.monotonic()
    proc = subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env={**os.environ},
    )

    decisions = []
    try:
        with open(req_pipe, "r") as req_in, open(resp_pipe, "w") as resp_out:
            while True:
                line = req_in.readline()
                if not line:
                    break
                t = time.monotonic() - t_start
                tool_request = json.loads(line)
                tool_name = tool_request.get("tool_name", "?")

                # First Write call → ask. Everything else allow.
                if tool_name == "Write" and not any(
                    d["tool"] == "Write" for d in decisions
                ):
                    decision_str = "ask"
                    reason = (
                        "Want me to create that file? "
                        "(probe9 — hook synthesized ask reason)"
                    )
                else:
                    decision_str = "allow"
                    reason = f"probe9: auto-allow {tool_name}"
                print(f"[parent] [{t:.2f}s] {tool_name} → {decision_str.upper()}")
                decisions.append({"t": t, "tool": tool_name, "decision": decision_str})
                resp_out.write(json.dumps({
                    "permissionDecision": decision_str,
                    "permissionDecisionReason": reason,
                }) + "\n")
                resp_out.flush()
    except FileNotFoundError:
        pass

    try:
        stdout, stderr = proc.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        print("[parent] !! claude -p hung past 120s — killing. mech 1 likely DEAD.")
        proc.kill()
        stdout, stderr = proc.communicate()
    elapsed = time.monotonic() - t_start

    print()
    print("=" * 60)
    print("PROBE 9 RESULTS — ask semantics")
    print("=" * 60)
    print(f"claude exit code:  {proc.returncode}")
    print(f"total elapsed:     {elapsed:.2f}s")
    print(f"hook calls:        {len(decisions)}")
    print(f"target landed:     {target.exists()}  (expected: depends on ask handling)")
    print()
    for d in decisions:
        print(f"  [{d['t']:6.2f}s] {d['tool']:15s} → {d['decision']}")

    events = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    print()
    print(f"total stream events: {len(events)}")
    event_types = {}
    for e in events:
        k = e.get("type", "?")
        sub = e.get("subtype")
        key = f"{k}/{sub}" if sub else k
        event_types[key] = event_types.get(key, 0) + 1
    print("event type histogram:")
    for k, n in sorted(event_types.items(), key=lambda x: -x[1]):
        print(f"  {n:4d}  {k}")

    # Surface anything that mentions "ask" or "permission" — that's where
    # the answer lives if claude does anything meaningful with the reason.
    print()
    print("--- events mentioning permission / ask / denial ---")
    for e in events:
        s = json.dumps(e)
        if any(k in s.lower() for k in ("ask", "permission", "denial", "rejected", "blocked")):
            # Trim huge ones
            if len(s) > 600:
                s = s[:300] + " … " + s[-200:]
            print(s)

    final_result = next((e for e in events if e.get("type") == "result"), None)
    if final_result:
        print()
        print(f"final result subtype:    {final_result.get('subtype')}")
        print(f"is_error:                {final_result.get('is_error')}")
        print(f"stop_reason:             {final_result.get('stop_reason')}")
        print(f"terminal_reason:         {final_result.get('terminal_reason')}")
        print(f"permission_denials:      {final_result.get('permission_denials')}")
        result_text = final_result.get("result", "")
        print(f"\nfinal result text (first 800 chars):")
        print(result_text[:800])

    Path(HERE / "probe9_stream.jsonl").write_text(stdout)
    Path(HERE / "probe9_stderr.txt").write_text(stderr or "")


if __name__ == "__main__":
    main()
