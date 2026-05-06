"""Probe 10 — system-prompt steering for pre-tool chat replies.

Question: can we get inner-claude to RELIABLY emit a chat reply BEFORE
each tool_use, so that the user sees a bot-voice "I'm about to write
that file — okay?" message right before the templated permission card?

This is mech 1.5: keep the existing permission-bridge plumbing
(deny/allow round-trip via PreToolUse hook), but recover the pre-pivot
UX where the bot's voice was visible in a message adjacent to the
sterile confirmation card.

What we measure per trial:
  - Did the model emit non-empty text via content_block_delta BEFORE
    the first tool_use block? (timestamps)
  - How long was that pre-tool text? (chars)
  - Did the text describe the tool / read like bot voice, or did it
    just say "I'll do that" generically?

Test: 3 trials, each with a different task that should trigger one or
more tools. System prompt steers explicit pre-tool narration.
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


SYSTEM_STEERING = (
    "You are the meeting assistant 'operator'. Speak in a warm, concise voice.\n"
    "\n"
    "CRITICAL TOOL UX RULE: before EVERY tool_use, you MUST first emit a "
    "short chat reply (one or two sentences) describing the action you're "
    "about to take. Phrase it conversationally, in your own voice — e.g. "
    "'Pulling the open Linear issues for ENG now.' or 'I'll write that "
    "file at /tmp/notes.txt — let me know if that's the wrong path.' Never "
    "call a tool silently. The pre-tool message and the tool call should "
    "always be in the SAME assistant turn — the message comes first, then "
    "the tool_use block."
)


TASKS = [
    "Use the Write tool to create the file {target1} with the contents "
    "'hello from probe 10'. After it lands, briefly confirm what you did. "
    "Do not retry on failure.",

    "Use the Read tool to read /etc/hosts. Then in one short sentence, "
    "report whether 'localhost' appears in it.",

    "Use the Bash tool to run `echo probe10-bash-trial`. Then briefly "
    "report what it printed.",
]


def run_trial(trial_num, task, target_path):
    tmp = tempfile.mkdtemp(prefix=f"claude-spike-probe10-t{trial_num}-")
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

    cmd = [
        "claude", "-p", task,
        "--settings", str(settings_path),
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--permission-mode", "default",
        "--append-system-prompt", SYSTEM_STEERING,
    ]

    print(f"\n[trial {trial_num}] task: {task[:80]}…")
    t_start = time.monotonic()
    proc = subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env={**os.environ},
    )

    # Auto-allow every tool — we want clean execution to observe the
    # full reply pattern, not deny-side branching.
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
                decisions.append({"t": t, "tool": tool_name})
                resp_out.write(json.dumps({
                    "permissionDecision": "allow",
                    "permissionDecisionReason": "probe10: auto-allow",
                }) + "\n")
                resp_out.flush()
                print(f"[trial {trial_num}] [{t:.2f}s] tool {tool_name} → ALLOW")
    except FileNotFoundError:
        pass

    try:
        stdout, stderr = proc.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()

    elapsed = time.monotonic() - t_start
    events = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    # Walk events in order. Track "did we see text_delta before any
    # tool_use block was finalized?" The text_delta has parent
    # content_block_start, but we don't need to track block index
    # carefully — we only care whether ANY text streams arrive before
    # the FIRST tool_use is dispatched.
    pre_tool_text = []
    saw_first_tool_at = None
    text_before_tool_buf = []
    for e in events:
        if saw_first_tool_at is None and e.get("type") == "stream_event":
            inner = e.get("event") or {}
            if inner.get("type") == "content_block_delta":
                delta = inner.get("delta") or {}
                if delta.get("type") == "text_delta":
                    text_before_tool_buf.append(delta.get("text") or "")
        # The most reliable "first tool" marker is the assistant event
        # that contains a tool_use content block. (Note: the tool args
        # also stream as input_json_delta on a content_block of type
        # tool_use, but for "did the user see ANY narration first" the
        # text_delta path above is what matters.)
        if saw_first_tool_at is None and e.get("type") == "assistant":
            content = (e.get("message") or {}).get("content") or []
            if any(b.get("type") == "tool_use" for b in content):
                saw_first_tool_at = "assistant_event"
                break

    pre_tool_text = "".join(text_before_tool_buf).strip()

    print(f"\n--- trial {trial_num} summary ---")
    print(f"  exit code:           {proc.returncode}")
    print(f"  elapsed:             {elapsed:.2f}s")
    print(f"  hook calls:          {len(decisions)}")
    print(f"  saw_first_tool:      {saw_first_tool_at}")
    print(f"  pre-tool text chars: {len(pre_tool_text)}")
    print(f"  pre-tool text:       {pre_tool_text!r}")

    out_path = HERE / f"probe10_t{trial_num}_stream.jsonl"
    out_path.write_text(stdout)
    return {
        "trial": trial_num,
        "task": task,
        "elapsed": elapsed,
        "hook_calls": len(decisions),
        "tools": [d["tool"] for d in decisions],
        "pre_tool_chars": len(pre_tool_text),
        "pre_tool_text": pre_tool_text,
    }


def main():
    target1 = Path(tempfile.mkdtemp(prefix="probe10-target-")) / "out.txt"
    tasks_with_targets = [
        TASKS[0].format(target1=target1),
        TASKS[1],
        TASKS[2],
    ]

    results = []
    for i, task in enumerate(tasks_with_targets, start=1):
        results.append(run_trial(i, task, target1))

    print()
    print("=" * 60)
    print("PROBE 10 SUMMARY — pre-tool voice steering")
    print("=" * 60)
    for r in results:
        ok = "YES" if r["pre_tool_chars"] > 0 else "NO"
        print(
            f"  trial {r['trial']}: pre-tool voice = {ok:3s} "
            f"({r['pre_tool_chars']} chars) | tools={r['tools']}"
        )
        if r["pre_tool_text"]:
            print(f"      → {r['pre_tool_text'][:150]!r}")

    # Verdict
    n_with_voice = sum(1 for r in results if r["pre_tool_chars"] > 0)
    print()
    print(
        f"Pre-tool voice present in {n_with_voice}/{len(results)} trials. "
        f"Mech 1.5 viable: {'YES' if n_with_voice == len(results) else 'PARTIAL — needs harder steering'}."
    )


if __name__ == "__main__":
    main()
