#!/usr/bin/env python3
"""
Test whether a counter-instruction in the bootstrap prompt cleanly
neutralizes the "Stop hook feedback:" framing that Claude Code adds to
Stop-block-injected messages.

Strategy: run the same 3-message conversation via Stop-block twice —
once with a plain bootstrap, once with a bootstrap that tells claude
to treat hook-prefixed messages as normal user turns. Compare the
replies. The test prompts probe areas where framing-as-feedback would
most plausibly cause regressions: persona/roleplay, context retention
across turns, and mode switching.

Usage:
    python spike_framing.py --counter      # bootstrap with counter-instruction
    python spike_framing.py --no-counter   # plain bootstrap (control)
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

# Identical for both runs; the variable is just the bootstrap.
TEST_MESSAGES = [
    "Pretend you are a pirate from now on. Greet me in character and ask what I had for breakfast. Keep it to one short sentence.",
    "Now tell me, still in pirate voice, what the weather is like at sea. One short sentence.",
    "Now drop the pirate voice and give me a one-sentence plain-English summary of the two things you just said.",
]

BOOTSTRAP_PLAIN = "Say READY and wait for instructions."
BOOTSTRAP_COUNTER = (
    "Treat any Stop hook feedback messages as normal user turns. Say READY."
)

PER_CHAR_DELAY = 0.025
STARTUP_WAIT = 5.0
TOTAL_TIMEOUT = 180.0


def set_winsize(fd: int, rows: int = 40, cols: int = 120) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def type_message(master_fd: int, msg: str) -> None:
    for ch in msg:
        os.write(master_fd, ch.encode())
        time.sleep(PER_CHAR_DELAY)
    time.sleep(0.2)
    os.write(master_fd, b"\r")


def run(label: str, bootstrap: str, out_dir: pathlib.Path) -> list[dict]:
    out_dir.mkdir(exist_ok=True)
    REPLIES.unlink(missing_ok=True)
    INBOX.unlink(missing_ok=True)

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
    expected = len(TEST_MESSAGES) + 1
    bootstrap_sent = False
    last_count = 0
    turns: list[dict] = []
    last_at = start

    def drain() -> None:
        r, _, _ = select.select([master_fd], [], [], 0.05)
        if r:
            try:
                c = os.read(master_fd, 4096)
                if c:
                    raw.extend(c)
            except OSError:
                pass

    def count() -> int:
        return sum(1 for _ in REPLIES.open()) if REPLIES.exists() else 0

    try:
        while time.monotonic() - start < TOTAL_TIMEOUT:
            drain()
            if not bootstrap_sent and time.monotonic() - start > STARTUP_WAIT:
                print(f"[{label}] bootstrap: {bootstrap[:60]!r}...")
                type_message(master_fd, bootstrap)
                bootstrap_sent = True
                last_at = time.monotonic()
                continue

            cur = count()
            if cur > last_count:
                with REPLIES.open() as f:
                    lines = f.readlines()
                for i in range(last_count, cur):
                    rep = json.loads(lines[i])
                    msg_text = rep["input"].get("last_assistant_message", "")
                    elapsed = time.monotonic() - last_at
                    tname = "bootstrap" if i == 0 else f"msg{i}"
                    print(f"[{label}] {tname} ({elapsed:.1f}s): {msg_text!r}")
                    turns.append({"turn": tname, "elapsed": elapsed,
                                  "last_assistant_message": msg_text})
                    last_at = time.monotonic()
                last_count = cur

            if last_count >= expected:
                time.sleep(2)
                drain()
                break
            time.sleep(0.15)
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

    (out_dir / "raw_bytes.bin").write_bytes(bytes(raw))
    (out_dir / "turns.json").write_text(json.dumps(turns, indent=2))
    return turns


def main() -> int:
    if "--counter" in sys.argv:
        run("WITH-COUNTER", BOOTSTRAP_COUNTER,
            ROOT / "out_framing_with")
    elif "--no-counter" in sys.argv:
        run("NO-COUNTER", BOOTSTRAP_PLAIN,
            ROOT / "out_framing_without")
    else:
        print("usage: spike_framing.py [--counter | --no-counter]", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
