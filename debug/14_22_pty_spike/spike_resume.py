#!/usr/bin/env python3
"""
Resume semantics probe for interactive claude.

Today operator passes `--resume-session <id>` to its `claude -p` spawn so
the inner-claude inherits the user's main Claude Code session (and any
context they pre-loaded before invoking /slip). The new architecture
uses interactive claude. This probe verifies `claude --resume <id>` has
the same semantics: continues the same conversation, retains context,
hooks fire normally.

Test:
  1. Session A: spawn interactive claude, send a message establishing a
     memorable fact ("my favorite color is fuchsia"). Capture session_id
     from the Stop hook input.
  2. Tear down A.
  3. Session B: spawn `claude --resume <session_id>` (interactive). Send
     a message that requires recalling the fact ("what's my favorite
     color?"). Verify the reply contains fuchsia.
  4. Verify hooks fire in B (Stop hook delivers the reply).

Usage:
    python spike_resume.py
"""

from __future__ import annotations

import fcntl
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


def set_winsize(fd, rows=40, cols=120):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def send_d(fd, msg):
    os.write(fd, b"\x1b[200~"); time.sleep(0.05)
    os.write(fd, msg.encode()); time.sleep(0.1)
    os.write(fd, b"\x1b[201~"); time.sleep(0.2)
    os.write(fd, b"\r")


def spawn(extra_args=()):
    env = os.environ.copy()
    env["SPIKE_MODE"] = "sendkeys"
    master_fd, slave_fd = pty.openpty()
    set_winsize(master_fd)
    cmd = ["claude", "--dangerously-skip-permissions", *extra_args]
    proc = subprocess.Popen(
        cmd, cwd=str(BENCH),
        stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        preexec_fn=os.setsid, env=env,
    )
    os.close(slave_fd)
    return proc, master_fd


def teardown(proc, fd):
    try: os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError: pass
    try: proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try: os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError: pass
    try: os.close(fd)
    except OSError: pass


def drain(fd, buf):
    r, _, _ = select.select([fd], [], [], 0.05)
    if r:
        try:
            c = os.read(fd, 4096)
            if c: buf.extend(c)
        except OSError: pass


def wait_for_reply(prev, timeout, fd, buf):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        drain(fd, buf)
        if REPLIES.exists() and sum(1 for _ in REPLIES.open()) > prev:
            with REPLIES.open() as f:
                lines = f.readlines()
            return json.loads(lines[prev])
        time.sleep(0.15)
    return None


def main() -> int:
    REPLIES.unlink(missing_ok=True)

    # --- Session A ---
    print("=== Session A: establish context ===")
    proc_a, fd_a = spawn()
    buf_a = bytearray()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 5.0:
        drain(fd_a, buf_a)

    send_d(fd_a, "My favorite color is fuchsia. Acknowledge with only the word REMEMBERED.")
    reply_a = wait_for_reply(0, 60.0, fd_a, buf_a)
    if not reply_a:
        print("  A: TIMEOUT during setup")
        teardown(proc_a, fd_a)
        return 1
    session_id = reply_a["input"].get("session_id")
    text_a = reply_a["input"].get("last_assistant_message", "")
    print(f"  A reply: {text_a!r}")
    print(f"  A session_id: {session_id}")
    teardown(proc_a, fd_a)

    if not session_id:
        print("  no session_id captured — abort")
        return 1

    # Save where session B should start counting from.
    pre_count = sum(1 for _ in REPLIES.open()) if REPLIES.exists() else 0

    # --- Session B ---
    print("\n=== Session B: resume + recall ===")
    proc_b, fd_b = spawn(extra_args=("--resume", session_id))
    buf_b = bytearray()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 6.0:
        drain(fd_b, buf_b)

    send_d(fd_b, "What is my favorite color? Respond with only the color name, nothing else.")
    reply_b = wait_for_reply(pre_count, 60.0, fd_b, buf_b)
    teardown(proc_b, fd_b)

    if not reply_b:
        print("  B: TIMEOUT — resume may have failed to start")
        return 1

    text_b = reply_b["input"].get("last_assistant_message", "")
    session_id_b = reply_b["input"].get("session_id")
    print(f"  B reply: {text_b!r}")
    print(f"  B session_id: {session_id_b}")
    same_session = (session_id == session_id_b)
    recall_ok = "fuchsia" in text_b.lower()
    print(f"  same session_id? {same_session}")
    print(f"  recalled fuchsia? {recall_ok}")

    verdict = "PASS" if (same_session and recall_ok) else "FAIL"
    print(f"\n=== {verdict} ===")
    (ROOT / "out_resume_result.json").write_text(json.dumps({
        "verdict": verdict, "session_id_a": session_id, "session_id_b": session_id_b,
        "reply_a": text_a, "reply_b": text_b,
        "same_session": same_session, "recalled": recall_ok,
    }, indent=2))
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
