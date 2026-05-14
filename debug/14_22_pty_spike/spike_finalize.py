#!/usr/bin/env python3
"""
Finalization spike — runs the four follow-ups that need to pass before
we commit to the production refactor:

  T1. Bracketed-paste D vs. tough content (quotes, backslashes, multi-line,
      emoji, fenced code blocks).
  T2. Multi-turn D in a single session (3 back-to-back messages).
  T3. A single user message that triggers a multi-tool loop. Validate
      one PreToolUse fires per tool and Stop fires once at the end.
  T4. With --yolo, does a failing tool call still surface a hook event we
      can hand to operator's `denial`/error callback?

Each test gets a fresh claude --dangerously-skip-permissions session in
bench/ to isolate state. Hooks log into bench/state/.

Usage:
    python spike_finalize.py
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pathlib
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time

ROOT = pathlib.Path(__file__).parent
BENCH = ROOT / "bench"
STATE = BENCH / "state"
REPLIES = STATE / "replies.jsonl"
TOOL_EVENTS = STATE / "tool_events.jsonl"
EVENTS = STATE / "events.jsonl"
INBOX = STATE / "inbox.jsonl"


def set_winsize(fd: int, rows: int = 40, cols: int = 120) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def send_d(fd: int, msg: str) -> None:
    """Bracketed-paste wrap + Enter. Strategy D from spike_longmsg."""
    os.write(fd, b"\x1b[200~")
    time.sleep(0.05)
    os.write(fd, msg.encode())
    time.sleep(0.1)
    os.write(fd, b"\x1b[201~")
    time.sleep(0.2)
    os.write(fd, b"\r")


def count_replies() -> int:
    return sum(1 for _ in REPLIES.open()) if REPLIES.exists() else 0


def count_tools() -> int:
    return sum(1 for _ in TOOL_EVENTS.open()) if TOOL_EVENTS.exists() else 0


def count_events() -> int:
    return sum(1 for _ in EVENTS.open()) if EVENTS.exists() else 0


def reset_state() -> None:
    for p in (REPLIES, TOOL_EVENTS, EVENTS, INBOX):
        p.unlink(missing_ok=True)


def spawn_claude() -> tuple[subprocess.Popen, int]:
    env = os.environ.copy()
    env["SPIKE_MODE"] = "sendkeys"
    master_fd, slave_fd = pty.openpty()
    set_winsize(master_fd)
    proc = subprocess.Popen(
        ["claude", "--dangerously-skip-permissions"],
        cwd=str(BENCH),
        stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        preexec_fn=os.setsid, env=env,
    )
    os.close(slave_fd)
    return proc, master_fd


def teardown(proc: subprocess.Popen, master_fd: int) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        os.close(master_fd)
    except OSError:
        pass


def drain(master_fd: int, buf: bytearray) -> None:
    r, _, _ = select.select([master_fd], [], [], 0.05)
    if r:
        try:
            c = os.read(master_fd, 4096)
            if c:
                buf.extend(c)
        except OSError:
            pass


def wait_for_reply(prev: int, timeout: float, master_fd: int, buf: bytearray) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        drain(master_fd, buf)
        if count_replies() > prev:
            with REPLIES.open() as f:
                lines = f.readlines()
            return json.loads(lines[prev])
        time.sleep(0.15)
    return None


def settle(master_fd: int, buf: bytearray, secs: float = 5.0) -> None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < secs:
        drain(master_fd, buf)


# ============================================================================

def test_1_tough_inputs() -> dict:
    print("\n=== T1: tough inputs (quotes/backslash/multiline/emoji/code) ===")
    reset_state()

    payload = "\n".join([
        "single quote: '",
        "double quote: \"",
        'backtick: `',
        "backslash: \\",
        "literal backslash-n: \\n",
        "emoji: 🚀 ✨",
        "code block follows:",
        "```python",
        "def hello():",
        '    return "world"',
        "```",
        "end of payload",
    ])
    expected_sha = hashlib.sha256(payload.encode()).hexdigest()

    msg = (
        "Use the Bash tool to compute the SHA-256 of the EXACT text between "
        "the markers <<<BEGIN>>> and <<<END>>> below, exclusive of the marker "
        "lines themselves and exclusive of the leading/trailing newline. "
        "Then respond with ONLY the 64-character lowercase hex digest, "
        "nothing else, no prose, no markdown.\n"
        "<<<BEGIN>>>\n"
        f"{payload}\n"
        "<<<END>>>"
    )

    proc, fd = spawn_claude()
    buf = bytearray()
    settle(fd, buf, 5.0)
    send_d(fd, msg)
    reply = wait_for_reply(0, 120.0, fd, buf)
    teardown(proc, fd)

    got = (reply["input"].get("last_assistant_message", "") if reply else "").strip()
    # Pull a 64-char hex out of the reply (claude may wrap in prose despite asking).
    import re
    m = re.search(r"[0-9a-f]{64}", got)
    actual_sha = m.group(0) if m else None

    verdict = "PASS" if actual_sha == expected_sha else "FAIL"
    print(f"  expected: {expected_sha}")
    print(f"  got:      {actual_sha}")
    print(f"  reply:    {got[:120]!r}")
    return {"test": "T1_tough_inputs", "verdict": verdict,
            "expected_sha": expected_sha, "got_sha": actual_sha,
            "reply_preview": got[:200]}


def test_2_multiturn_d() -> dict:
    print("\n=== T2: multi-turn back-to-back with D ===")
    reset_state()
    msgs = [
        "Respond with only the word ALPHA and nothing else.",
        "Now respond with only the word BRAVO and nothing else.",
        "Now respond with only the word CHARLIE and nothing else.",
    ]
    proc, fd = spawn_claude()
    buf = bytearray()
    settle(fd, buf, 5.0)

    replies: list[str] = []
    for i, m in enumerate(msgs):
        prev = count_replies()
        send_d(fd, m)
        reply = wait_for_reply(prev, 60.0, fd, buf)
        if not reply:
            print(f"  msg{i+1}: TIMEOUT")
            replies.append(None)
            break
        text = reply["input"].get("last_assistant_message", "").strip()
        print(f"  msg{i+1}: {text!r}")
        replies.append(text)

    teardown(proc, fd)
    expected = ["ALPHA", "BRAVO", "CHARLIE"]
    verdict = "PASS" if replies == expected else "FAIL"
    return {"test": "T2_multiturn_d", "verdict": verdict,
            "expected": expected, "got": replies}


def test_3_tool_loop() -> dict:
    print("\n=== T3: multi-tool turn — PreToolUse count + single Stop ===")
    reset_state()
    msg = (
        "Use the Bash tool three times to: (1) print 'hello' via `echo hello`, "
        "(2) print today's date via `date`, (3) print the kernel name via `uname -s`. "
        "Run them as three separate Bash tool calls in order. Then respond with "
        "only the word DONE and nothing else."
    )

    proc, fd = spawn_claude()
    buf = bytearray()
    settle(fd, buf, 5.0)

    t0 = time.monotonic()
    send_d(fd, msg)
    reply = wait_for_reply(0, 180.0, fd, buf)
    elapsed = time.monotonic() - t0
    teardown(proc, fd)

    stop_count = count_replies()
    tool_count = count_tools()
    text = (reply["input"].get("last_assistant_message", "") if reply else "").strip()
    print(f"  PreToolUse events: {tool_count}")
    print(f"  Stop events:       {stop_count}")
    print(f"  reply:             {text[:80]!r}")
    print(f"  elapsed:           {elapsed:.1f}s")

    # Pass criteria: at least 3 PreToolUse (we asked for 3 Bash calls; claude
    # might add a Read or similar — we don't require exactly 3), exactly 1 Stop,
    # reply contains DONE.
    verdict = "PASS" if (tool_count >= 3 and stop_count == 1 and "DONE" in text) else "FAIL"
    return {"test": "T3_tool_loop", "verdict": verdict,
            "pretool_count": tool_count, "stop_count": stop_count,
            "elapsed_sec": elapsed, "reply": text[:200]}


def test_4_failure_event() -> dict:
    print("\n=== T4: tool failure under --yolo — hook visibility ===")
    reset_state()
    # Try to read a file that doesn't exist. With --yolo, the tool runs but
    # the underlying op fails. Question: does anything surface in events.jsonl
    # (PostToolUseFailure / PermissionDenied / StopFailure)?
    msg = (
        "Use the Read tool on the file /tmp/this_file_definitely_does_not_exist_xyz_12345.txt. "
        "Then respond with only the word HANDLED, nothing else."
    )
    proc, fd = spawn_claude()
    buf = bytearray()
    settle(fd, buf, 5.0)

    send_d(fd, msg)
    reply = wait_for_reply(0, 60.0, fd, buf)
    teardown(proc, fd)

    failure_events = count_events()
    tool_events = count_tools()
    text = (reply["input"].get("last_assistant_message", "") if reply else "").strip()
    print(f"  PreToolUse:                    {tool_events}")
    print(f"  events.jsonl (failure/denied): {failure_events}")
    print(f"  reply:                         {text[:100]!r}")

    if failure_events > 0:
        with EVENTS.open() as f:
            for line in f:
                d = json.loads(line)
                kind = d["input"].get("hook_event_name", "?")
                print(f"    → {kind}: {json.dumps(d['input'])[:140]}")

    # Pass: either we got a failure-class event, OR we got HANDLED back with the
    # tool attempt visible in PreToolUse (claude noted the failure in its reply).
    saw_attempt = tool_events >= 1
    saw_handling = "HANDLED" in text or failure_events > 0
    verdict = "PASS" if (saw_attempt and saw_handling) else "FAIL"
    return {"test": "T4_failure_event", "verdict": verdict,
            "tool_events": tool_events, "failure_events": failure_events,
            "reply": text[:200]}


def main() -> int:
    results = [
        test_1_tough_inputs(),
        test_2_multiturn_d(),
        test_3_tool_loop(),
        test_4_failure_event(),
    ]
    (ROOT / "out_finalize_results.json").write_text(json.dumps(results, indent=2))
    print("\n=== summary ===")
    for r in results:
        print(f"  {r['test']:24} {r['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
