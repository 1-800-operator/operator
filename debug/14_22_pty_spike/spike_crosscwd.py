#!/usr/bin/env python3
"""
Cross-cwd resume probe.

Resume session 47c73465-... (originally created with cwd=bench/) from a
completely different cwd (/tmp/operator_resume_probe/) and check whether
claude finds the session by id alone, or whether it scopes the lookup
to the cwd's project dir.

If PASS: our plan to launch inner-claude from operator's managed dir
with --resume <user-main-session-id> works as-is.

If FAIL: --resume is project-scoped; we'll need a different strategy
(launch inner-claude in the user's original cwd).

Usage:
    python spike_crosscwd.py <session-id>
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

SESSION_ID = sys.argv[1] if len(sys.argv) > 1 else "47c73465-beb2-4d25-a5a6-b9ae7a7ae393"
ALIEN_CWD = "/tmp/operator_resume_probe"
ROOT = pathlib.Path(__file__).parent
REPLIES = ROOT / "bench" / "state" / "replies.jsonl"


def set_winsize(fd, rows=40, cols=120):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def send_d(fd, msg):
    os.write(fd, b"\x1b[200~"); time.sleep(0.05)
    os.write(fd, msg.encode()); time.sleep(0.1)
    os.write(fd, b"\x1b[201~"); time.sleep(0.2)
    os.write(fd, b"\r")


def main() -> int:
    pre = sum(1 for _ in REPLIES.open()) if REPLIES.exists() else 0
    print(f"[X] resuming {SESSION_ID} from alien cwd {ALIEN_CWD}")

    env = os.environ.copy()
    env["SPIKE_MODE"] = "sendkeys"
    master_fd, slave_fd = pty.openpty()
    set_winsize(master_fd)

    proc = subprocess.Popen(
        ["claude", "--dangerously-skip-permissions", "--resume", SESSION_ID],
        cwd=ALIEN_CWD,
        stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        preexec_fn=os.setsid, env=env,
    )
    os.close(slave_fd)

    raw = bytearray()
    start = time.monotonic()

    def drain():
        r, _, _ = select.select([master_fd], [], [], 0.05)
        if r:
            try:
                c = os.read(master_fd, 4096)
                if c: raw.extend(c)
            except OSError: pass

    # Settle.
    while time.monotonic() - start < 6.0:
        drain()

    send_d(master_fd, "What is my favorite color? Respond with only the color name, nothing else.")

    reply = None
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        drain()
        cur = sum(1 for _ in REPLIES.open()) if REPLIES.exists() else 0
        if cur > pre:
            with REPLIES.open() as f:
                lines = f.readlines()
            reply = json.loads(lines[pre])
            break
        time.sleep(0.15)

    try: os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError: pass
    try: proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try: os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError: pass
    try: os.close(master_fd)
    except OSError: pass

    (ROOT / "out_crosscwd_raw.bin").write_bytes(bytes(raw))

    if not reply:
        print("[X] TIMEOUT — no Stop hook fired. Either claude refused to start, "
              "the session wasn't found, or hooks didn't load.")
        # Diagnostics: check if claude even ran a turn by looking for typical TUI in raw bytes.
        if b"--resume" in raw or b"session" in raw.lower() or b"not found" in raw.lower():
            print("[X] raw bytes mention 'session' — claude likely errored on resume lookup")
        return 1

    sid_b = reply["input"].get("session_id")
    text = reply["input"].get("last_assistant_message", "")
    same = (sid_b == SESSION_ID)
    recalled = "fuchsia" in text.lower()

    print(f"[X] reply: {text!r}")
    print(f"[X] session_id matches? {same}  recalled fuchsia? {recalled}")
    verdict = "PASS" if (same and recalled) else "FAIL"
    print(f"\n=== {verdict} ===")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
