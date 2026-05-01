"""Probe 1 — CLI path: PreToolUse hook + named-pipe IPC for permission decisions.

Goal: verify the architecture for track A — can a parent process running
`claude -p` as a subprocess intercept inner-claude's tool_use permission
requests and resolve them via an out-of-process decision (simulating a
Meet chat round-trip)?

Architecture:
  parent (this script)
    │
    ├─ creates a named pipe at $TMPDIR/claude-perm-<pid>.pipe
    ├─ writes a tempfile settings.json with a PreToolUse hook
    │  pointing to ./perm_bridge.sh (next to this file)
    ├─ spawns: claude -p "<task>" --settings <tempfile> --output-format
    │          stream-json --verbose
    │
    └─ (when hook fires)
       claude → bridge.sh
         bridge writes tool details to the pipe
         bridge reads decision from the pipe
         bridge prints the JSON decision to stdout, exits 0
       claude proceeds (allow) or aborts the tool (deny)

The parent reads the pipe, simulates "user said yes/no" by writing the
decision back. In production operator this round-trip is Meet chat ↔
user reply. Here we just hardcode "allow" to validate the wiring.

Captures:
  - whether the hook actually fires (stdout/stderr from claude)
  - latency of the round-trip (time from hook-spawn to decision-read)
  - whether the inner-claude tool actually executes after allow
  - whether stream-json contains the tool_use event (so we can post
    "Reading X.py..." progress alongside the permission prompt)

Usage:
  cd /Users/jojo/Desktop/operator/debug/permission_mcp_spike
  ANTHROPIC_API_KEY= python3 cli_probe_01_hook_ipc.py
                    ^ blank to force claude.ai/max auth (subscription path)
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
    # Sanity: bridge script must exist + be executable.
    if not BRIDGE.exists():
        print(f"ERROR: bridge script missing at {BRIDGE}", file=sys.stderr)
        sys.exit(2)
    os.chmod(BRIDGE, 0o755)

    # Named-pipe rendezvous between bridge and parent. mkfifo on macOS supports
    # bidirectional handshake by opening read/write modes from the two ends.
    # We use TWO pipes for cleanliness (one each direction) so there's no
    # ambiguity about who reads what.
    tmp = tempfile.mkdtemp(prefix="claude-perm-spike-")
    req_pipe = Path(tmp) / "request.pipe"   # bridge -> parent
    resp_pipe = Path(tmp) / "response.pipe"  # parent -> bridge
    os.mkfifo(req_pipe, 0o600)
    os.mkfifo(resp_pipe, 0o600)
    print(f"[parent] pipes ready at {tmp}")

    # Per-invocation settings.json with a PreToolUse hook pointing to the
    # bridge. The hook receives the tool_input on stdin (per Claude Code
    # hooks contract) and writes JSON decision to stdout.
    settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "*",  # all tools — narrow per scenario
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{BRIDGE} {req_pipe} {resp_pipe}",
                            "timeout": 120,  # generous; default is 600s
                        }
                    ],
                }
            ]
        }
    }
    settings_path = Path(tmp) / "settings.json"
    settings_path.write_text(json.dumps(settings, indent=2))
    print(f"[parent] settings.json written: {settings_path}")

    # Force a tool_use event with a tiny task. Write to /tmp so the test
    # is non-destructive and the hook target is obvious in logs.
    target = Path(tmp) / "hello.txt"
    task = (
        f"Use the Write tool to create the file {target} "
        f"with the contents 'permission_spike_ok'. "
        f"Do not read or edit anything else."
    )

    cmd = [
        "claude",
        "-p", task,
        "--settings", str(settings_path),
        "--output-format", "stream-json",
        "--verbose",
        "--include-hook-events",
        "--permission-mode", "default",  # default mode = prompts go through hooks
    ]

    print(f"[parent] spawning: {' '.join(cmd)}")
    t_start = time.monotonic()

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ},  # caller controls ANTHROPIC_API_KEY presence
    )

    # Read the request pipe in a loop. The bridge will write a JSON line
    # per tool_use event; we respond with allow/deny.
    decisions_recorded = []
    try:
        with open(req_pipe, "r") as req_in, open(resp_pipe, "w") as resp_out:
            while True:
                line = req_in.readline()
                if not line:
                    # EOF on req pipe = bridge done with all calls
                    break
                t_req = time.monotonic() - t_start
                try:
                    tool_request = json.loads(line)
                except json.JSONDecodeError:
                    print(f"[parent] non-JSON request line: {line!r}")
                    continue
                tool_name = tool_request.get("tool_name", "?")
                tool_input = tool_request.get("tool_input", {})
                print(f"[parent] [{t_req:.2f}s] tool_use request: {tool_name} input={json.dumps(tool_input)[:120]}")

                # Decision: allow everything for the happy-path probe.
                # In production operator this is the chat round-trip.
                decision = {
                    "permissionDecision": "allow",
                    "permissionDecisionReason": f"probe: auto-approved {tool_name}",
                }
                resp_out.write(json.dumps(decision) + "\n")
                resp_out.flush()
                decisions_recorded.append({"t": t_req, "tool": tool_name, "decision": "allow"})
                print(f"[parent] [{t_req:.2f}s] responded: allow")
    except FileNotFoundError:
        print("[parent] pipe gone — claude likely exited")

    stdout, stderr = proc.communicate(timeout=60)
    elapsed = time.monotonic() - t_start

    # Parse stream-json events for analysis.
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass

    # Did the file actually get written?
    file_landed = target.exists()

    # Report.
    print()
    print("=" * 60)
    print("PROBE 1 RESULTS")
    print("=" * 60)
    print(f"claude exit code:        {proc.returncode}")
    print(f"total elapsed:           {elapsed:.2f}s")
    print(f"hook calls fielded:      {len(decisions_recorded)}")
    print(f"file landed:             {file_landed}")
    print(f"target path:             {target}")
    if file_landed:
        print(f"file contents:           {target.read_text()!r}")
    print(f"stream-json event types: {sorted(set(e.get('type', '?') for e in events))}")
    print()

    # Find the tool_use event in stream-json — verify we can also surface
    # progress signals alongside the permission prompt.
    tool_use_events = []
    for e in events:
        if e.get("type") == "assistant":
            content = (e.get("message") or {}).get("content") or []
            for block in content:
                if block.get("type") == "tool_use":
                    tool_use_events.append({"name": block.get("name"), "input": block.get("input")})
    print(f"stream-json tool_use events: {len(tool_use_events)}")
    for tu in tool_use_events:
        print(f"  {tu['name']}: {json.dumps(tu['input'])[:80]}")

    # Hook events (with --include-hook-events).
    hook_events = [e for e in events if e.get("type") == "hook"]
    print(f"stream-json hook events: {len(hook_events)}")
    for h in hook_events[:5]:
        print(f"  {json.dumps(h)[:150]}")

    if stderr:
        print()
        print("--- stderr (last 30 lines) ---")
        print("\n".join(stderr.splitlines()[-30:]))

    # Persist artifacts for later inspection.
    Path(HERE / "probe1_stream.jsonl").write_text(stdout)
    Path(HERE / "probe1_stderr.txt").write_text(stderr or "")
    print(f"\nartifacts saved: {HERE/'probe1_stream.jsonl'}, {HERE/'probe1_stderr.txt'}")


if __name__ == "__main__":
    main()
