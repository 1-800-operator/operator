"""Tiny terminal narrator for the Operator CLI.

The user watches stderr while the bot spins up and runs a meeting. This module
emits short, colored, one-line status updates — the *narrative* layer. Detailed
diagnostics stay in /tmp/operator.log via the logging module.

Colors are auto-disabled when:
- $NO_COLOR is set (https://no-color.org)
- stderr is not a TTY (piped to a file, another process, CI, etc.)
"""
from __future__ import annotations

import os
import sys


def _enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stderr.isatty()


_COLORS = {
    "reset": "\033[0m",
    "dim":   "\033[2m",
    "red":   "\033[31m",
    "green": "\033[32m",
    "yellow":"\033[33m",
    "blue":  "\033[34m",
    "cyan":  "\033[36m",
}


def _c(color: str, text: str) -> str:
    if not _enabled():
        return text
    return f"{_COLORS[color]}{text}{_COLORS['reset']}"


def say(msg: str) -> None:
    """Neutral progress line. Dim arrow prefix."""
    print(f"{_c('dim', '▸')} {msg}", file=sys.stderr, flush=True)


def ok(msg: str) -> None:
    """Success / milestone line. Green check prefix."""
    print(f"{_c('green', '✓')} {msg}", file=sys.stderr, flush=True)


def warn(msg: str) -> None:
    """Soft warning — unexpected but not fatal."""
    print(f"{_c('yellow', '⚠')} {msg}", file=sys.stderr, flush=True)


def err(msg: str, hint_log: bool = True) -> None:
    """Error line. Appends the log-file pointer by default."""
    suffix = f"  {_c('dim', '— see /tmp/operator.log')}" if hint_log else ""
    print(f"{_c('red', '✗')} {msg}{suffix}", file=sys.stderr, flush=True)


def chat_in(sender: str, text: str) -> None:
    """Inbound chat message — rendered as 'sender: text'."""
    print(f"{_c('cyan', '→')} {_c('cyan', sender)}: {text}", file=sys.stderr, flush=True)


def chat_out(text: str) -> None:
    """Outbound reply from the bot."""
    print(f"{_c('blue', '←')} {text}", file=sys.stderr, flush=True)
