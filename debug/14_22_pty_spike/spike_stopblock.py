#!/usr/bin/env python3
"""
Approach B: Stop-block multi-turn driver.

Pre-loads all test messages into bench/state/inbox.jsonl, spawns
interactive `claude` in the bench/ workdir with a benign bootstrap
prompt, then never touches the PTY again. The Stop hook pops each
queued message and returns decision:"block" with it as the reason,
keeping the session alive turn after turn.

Usage:
    python spike_stopblock.py
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
INBOX = STATE / "inbox.jsonl"
REPLIES = STATE / "replies.jsonl"
RAW_LOG = ROOT / "out_stopblock"
RAW_LOG.mkdir(exist_ok=True)

TEST_MESSAGES = [
    "Respond with only the word ALPHA and nothing else.",
    "Now respond with only the word BRAVO and nothing else.",
    "Read /etc/hostname and tell me what it contains in 5 words or less.",
]

BOOTSTRAP = "Say READY and wait for instructions."
STARTUP_WAIT = 5.0
PER_CHAR_DELAY = 0.025
TOTAL_TIMEOUT = 240.0


def set_winsize(fd: int, rows: int = 40, cols: int = 120) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def type_message(master_fd: int, msg: str) -> None:
    for ch in msg:
        os.write(master_fd, ch.encode())
        time.sleep(PER_CHAR_DELAY)
    time.sleep(0.2)
    os.write(master_fd, b"\r")


def main() -> int:
    REPLIES.unlink(missing_ok=True)
    INBOX.unlink(missing_ok=True)
    (STATE / "tool_events.jsonl").unlink(missing_ok=True)

    # Pre-load inbox with all test messages.
    with INBOX.open("w") as f:
        for msg in TEST_MESSAGES:
            f.write(json.dumps({"message": msg}) + "\n")

    env = os.environ.copy()
    env["SPIKE_MODE"] = "stopblock"

    master_fd, slave_fd = pty.openpty()
    set_winsize(master_fd)

    proc = subprocess.Popen(
        ["claude"],
        cwd=str(BENCH),
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        preexec_fn=os.setsid,
        env=env,
    )
    os.close(slave_fd)

    raw = bytearray()
    start = time.monotonic()
    expected_replies = len(TEST_MESSAGES) + 1  # bootstrap + 3 queued
    bootstrap_sent = False
    timings: list[dict] = []
    last_reply_count = 0
    last_reply_at = start

    def drain() -> None:
        ready, _, _ = select.select([master_fd], [], [], 0.05)
        if ready:
            try:
                chunk = os.read(master_fd, 4096)
                if chunk:
                    raw.extend(chunk)
            except OSError:
                pass

    def count_replies() -> int:
        if not REPLIES.exists():
            return 0
        return sum(1 for _ in REPLIES.open())

    try:
        while time.monotonic() - start < TOTAL_TIMEOUT:
            drain()

            if not bootstrap_sent and time.monotonic() - start > STARTUP_WAIT:
                print(f"[B] sending bootstrap: {BOOTSTRAP!r}")
                type_message(master_fd, BOOTSTRAP)
                bootstrap_sent = True
                last_reply_at = time.monotonic()
                continue

            current = count_replies()
            if current > last_reply_count:
                # New replies arrived; log latest.
                with REPLIES.open() as f:
                    lines = f.readlines()
                for i in range(last_reply_count, current):
                    reply = json.loads(lines[i])
                    msg_text = reply["input"].get("last_assistant_message", "")
                    elapsed_since_prev = time.monotonic() - last_reply_at
                    label = "bootstrap" if i == 0 else f"msg{i}"
                    print(f"[B] {label}: reply in {elapsed_since_prev:.1f}s — {msg_text!r}")
                    timings.append({
                        "turn": label,
                        "elapsed_since_prev": elapsed_since_prev,
                        "last_assistant_message": msg_text,
                    })
                    last_reply_at = time.monotonic()
                last_reply_count = current

            if last_reply_count >= expected_replies:
                # Give claude a moment to exit cleanly.
                time.sleep(2)
                drain()
                break

            time.sleep(0.15)

        else:
            print(f"[B] TOTAL_TIMEOUT after {TOTAL_TIMEOUT}s with {last_reply_count}/{expected_replies} replies")

    finally:
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

    (RAW_LOG / "raw_bytes.bin").write_bytes(bytes(raw))
    (RAW_LOG / "timings.json").write_text(json.dumps(timings, indent=2))
    print(f"\n[B] got {last_reply_count}/{expected_replies} replies")
    print(f"[B] wrote {len(raw)} bytes + timings → {RAW_LOG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
