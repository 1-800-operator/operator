"""Probe 2 — CLI path: sub-agent (Task tool) permission visibility.

Question: when inner-claude dispatches a sub-agent via the Task tool, do
the sub-agent's inner tool_use events fire OUR PreToolUse hook?

This matters: operator's claude bot will routinely produce sub-agent
dispatches (Plan / Explore / general-purpose). If those run with
parent's permission mode but skip our hook, the bot can silently write
files via sub-agents while we think we're gating writes.

Test: ask inner-claude to dispatch a sub-agent that creates two files.
If our hook fires for both, sub-agent calls ARE visible. If our hook
fires only for the parent's `Task` tool_use (and not the sub-agent's
inner Write calls), they are NOT visible.

Same IPC mechanism as probe 1 (pipe-based bridge).
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
    tmp = tempfile.mkdtemp(prefix="claude-perm-spike-")
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
                            "timeout": 120,
                        }
                    ],
                }
            ]
        }
    }
    settings_path = Path(tmp) / "settings.json"
    settings_path.write_text(json.dumps(settings, indent=2))

    target_a = Path(tmp) / "alpha.txt"
    target_b = Path(tmp) / "beta.txt"
    task = (
        f"Use the Task tool to dispatch a general-purpose sub-agent "
        f"with this instruction: 'Create two files. First, use Write to "
        f"create {target_a} with contents `from-subagent-A`. Then use "
        f"Write to create {target_b} with contents `from-subagent-B`. "
        f"That is the entire task. Report success.' Do NOT do the "
        f"writes yourself in the parent agent — only dispatch the "
        f"sub-agent and report what it returns."
    )

    cmd = [
        "claude", "-p", task,
        "--settings", str(settings_path),
        "--output-format", "stream-json",
        "--verbose",
        "--include-hook-events",
        "--permission-mode", "default",
    ]

    print(f"[parent] spawning subagent probe...")
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
                inp_preview = json.dumps(tool_request.get("tool_input", {}))[:120]
                print(f"[parent] [{t:.2f}s] hook FIRED for {tool_name}: {inp_preview}")
                decisions.append({"t": t, "tool": tool_name, "input": tool_request.get("tool_input", {})})
                resp_out.write(json.dumps({
                    "permissionDecision": "allow",
                    "permissionDecisionReason": "probe2: auto-allow",
                }) + "\n")
                resp_out.flush()
    except FileNotFoundError:
        pass

    stdout, stderr = proc.communicate(timeout=180)
    elapsed = time.monotonic() - t_start

    print()
    print("=" * 60)
    print("PROBE 2 RESULTS — sub-agent permission visibility")
    print("=" * 60)
    print(f"claude exit code:    {proc.returncode}")
    print(f"total elapsed:       {elapsed:.2f}s")
    print(f"hook calls fielded:  {len(decisions)}")
    print(f"target_a landed:     {target_a.exists()}")
    print(f"target_b landed:     {target_b.exists()}")
    print()
    print("Hook calls in order:")
    for d in decisions:
        print(f"  [{d['t']:6.2f}s] {d['tool']:20s} {json.dumps(d['input'])[:100]}")

    # Look at stream-json for sub-agent activity
    events = [json.loads(l) for l in stdout.splitlines() if l.strip()]
    tool_use_events = []
    for e in events:
        if e.get("type") == "assistant":
            for block in (e.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    tool_use_events.append({
                        "name": block.get("name"),
                        "parent_tool_use_id": e.get("parent_tool_use_id"),
                    })
    print()
    print("Stream-json tool_use events (with parent_tool_use_id):")
    for tu in tool_use_events:
        nest = " [SUBAGENT]" if tu["parent_tool_use_id"] else " [TOP-LEVEL]"
        print(f"  {tu['name']}{nest} (parent_id={tu['parent_tool_use_id']})")

    # Verdict
    print()
    print("VERDICT:")
    subagent_writes_visible_in_hooks = sum(
        1 for d in decisions if d["tool"] == "Write"
    )
    subagent_writes_in_stream = sum(
        1 for tu in tool_use_events
        if tu["name"] == "Write" and tu["parent_tool_use_id"]
    )
    if subagent_writes_visible_in_hooks >= 2:
        print("  ✓ Sub-agent's Write calls DID fire our PreToolUse hook")
    elif subagent_writes_in_stream >= 1 and subagent_writes_visible_in_hooks < subagent_writes_in_stream:
        print("  ✗ Sub-agent's Write calls did NOT fire our PreToolUse hook")
        print("    (visible in stream-json but bypassed our gate)")
    else:
        print("  ? Inconclusive — sub-agent may not have actually dispatched")
        print(f"    hook Write count: {subagent_writes_visible_in_hooks}")
        print(f"    stream-json sub-agent Write count: {subagent_writes_in_stream}")

    Path(HERE / "probe2_stream.jsonl").write_text(stdout)
    Path(HERE / "probe2_stderr.txt").write_text(stderr or "")


if __name__ == "__main__":
    main()
