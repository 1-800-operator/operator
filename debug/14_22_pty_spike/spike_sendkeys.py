#!/usr/bin/env python3
"""
Approach A: send-keys multi-turn driver.

Spawns interactive `claude` under a PTY inside bench/, then types each
test message char-by-char (to dodge the bracketed-paste submit issue we
saw with one-shot writes). Each turn's reply is extracted via the Stop
hook into bench/state/replies.jsonl.

Usage:
    python spike_sendkeys.py
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
RAW_LOG = ROOT / "out_sendkeys"
RAW_LOG.mkdir(exist_ok=True)

TEST_MESSAGES = [
    "Respond with only the word ALPHA and nothing else.",
    "Now respond with only the word BRAVO and nothing else.",
    "Read /etc/hostname and tell me what it contains in 5 words or less.",
]

STARTUP_WAIT = 5.0
PER_CHAR_DELAY = 0.025
REPLY_TIMEOUT = 90.0


def set_winsize(fd: int, rows: int = 40, cols: int = 120) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def replies_count() -> int:
    if not REPLIES.exists():
        return 0
    return sum(1 for _ in REPLIES.open())


def wait_for_reply(prev_count: int, timeout: float) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if replies_count() > prev_count:
            with REPLIES.open() as f:
                lines = f.readlines()
            return json.loads(lines[prev_count])
        time.sleep(0.2)
    return None


def type_message(master_fd: int, msg: str) -> None:
    """Char-by-char typing to dodge bracketed-paste submit issue."""
    for ch in msg:
        os.write(master_fd, ch.encode())
        time.sleep(PER_CHAR_DELAY)
    time.sleep(0.2)
    os.write(master_fd, b"\r")


def main() -> int:
    REPLIES.unlink(missing_ok=True)
    (STATE / "tool_events.jsonl").unlink(missing_ok=True)

    env = os.environ.copy()
    env["SPIKE_MODE"] = "sendkeys"

    cmd = ["claude"]
    if "--yolo" in sys.argv:
        cmd.append("--dangerously-skip-permissions")
        print("[A] launching with --dangerously-skip-permissions")

    master_fd, slave_fd = pty.openpty()
    set_winsize(master_fd)

    proc = subprocess.Popen(
        cmd,
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
    timings: list[dict] = []
    sent = 0
    last_reply_count = 0

    def drain() -> None:
        ready, _, _ = select.select([master_fd], [], [], 0.05)
        if ready:
            try:
                chunk = os.read(master_fd, 4096)
                if chunk:
                    raw.extend(chunk)
            except OSError:
                pass

    try:
        # Let claude render.
        while time.monotonic() - start < STARTUP_WAIT:
            drain()

        for i, msg in enumerate(TEST_MESSAGES):
            print(f"[A] msg{i+1}: sending {msg!r}")
            t0 = time.monotonic()
            type_message(master_fd, msg)
            sent += 1

            deadline = t0 + REPLY_TIMEOUT
            reply = None
            while time.monotonic() < deadline:
                drain()
                if replies_count() > last_reply_count:
                    with REPLIES.open() as f:
                        lines = f.readlines()
                    reply = json.loads(lines[last_reply_count])
                    last_reply_count += 1
                    break
                time.sleep(0.15)

            elapsed = time.monotonic() - t0
            if reply is None:
                print(f"[A] msg{i+1}: TIMEOUT after {elapsed:.1f}s")
                timings.append({"msg": msg, "elapsed": elapsed, "reply": None})
                break

            last_msg = reply["input"].get("last_assistant_message", "")
            print(f"[A] msg{i+1}: got reply in {elapsed:.1f}s — {last_msg!r}")
            timings.append({"msg": msg, "elapsed": elapsed,
                            "last_assistant_message": last_msg})

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
    print(f"\n[A] sent={sent}, got={len(timings) - (1 if timings and timings[-1].get('reply') is None else 0)}")
    print(f"[A] wrote {len(raw)} bytes + timings → {RAW_LOG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
