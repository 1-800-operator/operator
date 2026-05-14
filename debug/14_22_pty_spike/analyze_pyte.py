#!/usr/bin/env python3
"""
Feed captured PTY bytes through a virtual terminal emulator (pyte) to
recover what claude *actually rendered* on the screen, vs. our naive
ANSI-strip.

Usage:
    python analyze_pyte.py [path/to/raw_bytes.bin]

Outputs to ./out/:
    pyte_screen.txt   — final screen contents (cols x rows)
    pyte_history.txt  — scrollback history (lines that scrolled off top)
    pyte_full.txt     — history + screen, the closest thing to a transcript

Notes:
    - We render at 120 cols x 40 rows (matches the winsize in spike_pty.py).
    - HistoryScreen keeps scrolled-off lines so we don't lose tool-use
      output that got pushed above the viewport.
    - The 'final' view is a snapshot — for live event detection you'd hook
      into pyte's Stream callbacks and watch cell changes over time.
"""

from __future__ import annotations

import pathlib
import sys

import pyte


OUT_DIR = pathlib.Path(__file__).parent / "out"
COLS, ROWS = 120, 40
HISTORY = 2000


def main() -> int:
    src = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else OUT_DIR / "raw_bytes.bin"
    if not src.exists():
        print(f"no bytes at {src}", file=sys.stderr)
        return 2

    out_dir = src.parent  # write outputs alongside the bytes file
    data = src.read_bytes()
    print(f"[analyze] feeding {len(data)} bytes through pyte ({COLS}x{ROWS})")

    screen = pyte.HistoryScreen(COLS, ROWS, history=HISTORY, ratio=0.5)
    stream = pyte.Stream(screen)
    stream.feed(data.decode("utf-8", errors="replace"))

    screen_lines = [line.rstrip() for line in screen.display]
    history_lines = []
    for entry in screen.history.top:
        line = "".join(ch.data for ch in entry).rstrip()
        history_lines.append(line)

    (out_dir / "pyte_screen.txt").write_text("\n".join(screen_lines) + "\n")
    (out_dir / "pyte_history.txt").write_text("\n".join(history_lines) + "\n")
    (out_dir / "pyte_full.txt").write_text(
        "=== history (scrolled off top) ===\n"
        + "\n".join(history_lines)
        + "\n\n=== current screen ===\n"
        + "\n".join(screen_lines)
        + "\n"
    )

    print(f"[analyze] screen: {len(screen_lines)} rows, "
          f"history: {len(history_lines)} rows → {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
