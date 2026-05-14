#!/usr/bin/env python3
"""
PTY-drive interactive claude spike (Phase 14.22).

Goal: prove we can spawn interactive `claude` (no -p) under a PTY, send a
prompt, and capture the TUI byte stream for offline analysis. This spike
does NOT yet parse out reply/tool-use/denial signals — first we need real
bytes to look at so we can see what conventions claude's TUI uses for each.

Usage:
    python spike_pty.py "your prompt here"
    python spike_pty.py "your prompt" --timeout 60

Outputs to ./out/:
    raw_bytes.bin   — every byte read from the PTY
    raw_bytes.hex   — hex+ASCII dump, easy to grep
    clean_text.txt  — ANSI-stripped best effort
    summary.txt     — what we sent, elapsed time, exit code

Caveats:
    - Assumes claude is already authenticated.
    - Cwd matters: claude reads CLAUDE.md from where it's launched.
      Run from a dir where claude will behave normally.
    - Quiet-detection is a heuristic; long-running tool loops may trip
      the timeout. Bump --timeout / QUIET_THRESHOLD if needed.
"""

from __future__ import annotations

import datetime
import fcntl
import os
import pathlib
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time


# Strip CSI/OSC/two-byte ESC sequences. Not exhaustive — TUIs are messy.
ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[=>()][\x20-\x7e]?")

OUT_DIR = pathlib.Path(__file__).parent / "out"
OUT_DIR.mkdir(exist_ok=True)


def set_winsize(fd: int, rows: int = 40, cols: int = 120) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def main() -> int:
    if len(sys.argv) < 2:
        print('usage: spike_pty.py "your prompt" [--timeout N]', file=sys.stderr)
        return 2

    prompt = sys.argv[1]
    hard_timeout = 120.0
    if "--timeout" in sys.argv:
        i = sys.argv.index("--timeout")
        hard_timeout = float(sys.argv[i + 1])

    startup_wait = 4.0      # let TUI render before typing
    quiet_threshold = 6.0   # idle gap → assume turn finished
    if "--quiet" in sys.argv:
        i = sys.argv.index("--quiet")
        quiet_threshold = float(sys.argv[i + 1])

    master_fd, slave_fd = pty.openpty()
    set_winsize(master_fd)

    proc = subprocess.Popen(
        ["claude"],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        preexec_fn=os.setsid,
        env=os.environ.copy(),
    )
    os.close(slave_fd)

    raw = bytearray()
    start = time.monotonic()
    last_byte_at = start
    sent_prompt = False

    try:
        while True:
            elapsed = time.monotonic() - start
            if elapsed > hard_timeout:
                print(f"[spike] hard timeout at {elapsed:.1f}s", file=sys.stderr)
                break

            ready, _, _ = select.select([master_fd], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                raw.extend(chunk)
                last_byte_at = time.monotonic()

            if not sent_prompt and elapsed > startup_wait:
                os.write(master_fd, prompt.encode() + b"\r")
                sent_prompt = True
                last_byte_at = time.monotonic()
                print(f"[spike] sent prompt at t={elapsed:.1f}s", file=sys.stderr)

            if sent_prompt and (time.monotonic() - last_byte_at) > quiet_threshold:
                print(
                    f"[spike] {quiet_threshold:.0f}s of quiet → assuming turn done",
                    file=sys.stderr,
                )
                break

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

    (OUT_DIR / "raw_bytes.bin").write_bytes(bytes(raw))

    with (OUT_DIR / "raw_bytes.hex").open("w") as f:
        for i in range(0, len(raw), 16):
            chunk = raw[i : i + 16]
            hex_part = " ".join(f"{b:02x}" for b in chunk)
            ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            f.write(f"{i:08x}  {hex_part:<48}  {ascii_part}\n")

    clean = ANSI_RE.sub(b"", bytes(raw))
    clean = bytes(b for b in clean if b >= 0x20 or b in (0x09, 0x0a))
    (OUT_DIR / "clean_text.txt").write_bytes(clean)

    (OUT_DIR / "summary.txt").write_text(
        f"timestamp:        {datetime.datetime.now().isoformat()}\n"
        f"prompt:           {prompt!r}\n"
        f"bytes_captured:   {len(raw)}\n"
        f"elapsed_sec:      {time.monotonic() - start:.2f}\n"
        f"claude_returncode: {proc.returncode}\n"
    )

    print(f"[spike] {len(raw)} bytes captured → {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
