#!/usr/bin/env python3
"""
Long-message typing fragility probe.

In the earlier framing spike, a ~400-char counter-instruction failed to
submit and dropped a space ("a meeting" → "ameeting") when typed
char-by-char at 25ms. We hypothesise two root causes:
  - char-drop: 25ms is too tight and the terminal swallows chars under
    rapid PTY writes.
  - submit-failure: when input wraps to multiple lines in claude's TUI,
    a raw `\\r` is treated as an embedded newline, not a submit.

This probe tests four input strategies against a single ~280-char
message that asks claude to "respond with only SUBMITTED." We use
Stop hooks to grab the reply.

    A. char-by-char @ 25ms             (control — what spike_sendkeys does)
    B. char-by-char @ 50ms             (slower → maybe fewer dropped chars)
    C. disable bracketed-paste, then A (send `\\x1b[?2004l` first)
    D. bracketed-paste wrap            (send `\\x1b[200~` + msg + `\\x1b[201~\\r`)

Each strategy spawns a fresh claude session in bench/ with --yolo.
A strategy "wins" if its reply matches SUBMITTED exactly.

Usage:
    python spike_longmsg.py
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

PADDING = (
    "Padding to make this long enough to wrap in the input box: "
    "lorem ipsum dolor sit amet consectetur adipiscing elit sed do "
    "eiusmod tempor incididunt ut labore et dolore magna aliqua ut "
    "enim ad minim veniam quis nostrud exercitation ullamco laboris."
)
MESSAGE = f"Respond with only the word SUBMITTED and nothing else. {PADDING}"

STARTUP_WAIT = 5.0
REPLY_TIMEOUT = 60.0


def set_winsize(fd: int, rows: int = 40, cols: int = 120) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def type_a(fd: int, msg: str) -> None:
    for ch in msg:
        os.write(fd, ch.encode()); time.sleep(0.025)
    time.sleep(0.2); os.write(fd, b"\r")


def type_b(fd: int, msg: str) -> None:
    for ch in msg:
        os.write(fd, ch.encode()); time.sleep(0.050)
    time.sleep(0.2); os.write(fd, b"\r")


def type_c(fd: int, msg: str) -> None:
    os.write(fd, b"\x1b[?2004l"); time.sleep(0.1)
    for ch in msg:
        os.write(fd, ch.encode()); time.sleep(0.025)
    time.sleep(0.2); os.write(fd, b"\r")


def type_d(fd: int, msg: str) -> None:
    os.write(fd, b"\x1b[200~"); time.sleep(0.05)
    os.write(fd, msg.encode()); time.sleep(0.1)
    os.write(fd, b"\x1b[201~"); time.sleep(0.2)
    os.write(fd, b"\r")


STRATEGIES = [
    ("A_chars_25ms", type_a),
    ("B_chars_50ms", type_b),
    ("C_paste_disabled", type_c),
    ("D_bracketed_paste", type_d),
]


def run_one(name: str, sender) -> dict:
    print(f"\n[{name}] starting…")
    REPLIES.unlink(missing_ok=True)

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

    raw = bytearray()
    start = time.monotonic()
    sent = False
    reply = None
    t_sent = None

    def drain() -> None:
        r, _, _ = select.select([master_fd], [], [], 0.05)
        if r:
            try:
                c = os.read(master_fd, 4096)
                if c: raw.extend(c)
            except OSError: pass

    try:
        while time.monotonic() - start < STARTUP_WAIT + REPLY_TIMEOUT + 5:
            drain()
            if not sent and time.monotonic() - start > STARTUP_WAIT:
                t_sent = time.monotonic()
                sender(master_fd, MESSAGE)
                sent = True
                continue
            if sent and REPLIES.exists():
                with REPLIES.open() as f:
                    lines = f.readlines()
                if lines:
                    reply = json.loads(lines[0])
                    break
            if sent and time.monotonic() - t_sent > REPLY_TIMEOUT:
                break
            time.sleep(0.15)
    finally:
        try: os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError: pass
        try: proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try: os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError: pass
        try: os.close(master_fd)
        except OSError: pass

    out_dir = ROOT / f"out_longmsg_{name}"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "raw_bytes.bin").write_bytes(bytes(raw))

    if reply is None:
        verdict = "TIMEOUT — no reply"
        msg_text = None
    else:
        msg_text = reply["input"].get("last_assistant_message", "")
        verdict = "PASS" if msg_text.strip() == "SUBMITTED" else f"FAIL (got: {msg_text!r})"
    print(f"[{name}] {verdict}")
    return {"strategy": name, "verdict": verdict, "reply": msg_text}


def main() -> int:
    results = [run_one(name, sender) for name, sender in STRATEGIES]
    (ROOT / "out_longmsg_results.json").write_text(json.dumps(results, indent=2))
    print("\n=== summary ===")
    for r in results:
        print(f"  {r['strategy']:24} {r['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
