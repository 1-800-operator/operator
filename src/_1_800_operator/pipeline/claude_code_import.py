"""Claude Code installation + MCP-config readers used at boot.

v1 keeps two narrow helpers:

  - `claude_code_installed_and_logged_in()` — preflight gate, used by
    `_run_bot` and `_run_slip` to fail loudly before any browser starts
    when the `claude` CLI is missing or not logged in.
  - `read_user_mcp_config()` — pure read of `~/.claude.json` (or the
    `~/.claude/settings.json` fallback). Used by `claude_cli` to
    translate operator's `disabledMcpjsonServers` overlay back to
    JSON-keyed names.

The wizard-era discovery helpers (CLI shell-out for hosted connectors,
project-scope `.mcp.json` reader, CLAUDE.md walker, env placeholder
appender, transport classification) were dropped in 14.19.7-F. Claude
Code reads its own configuration natively when the binary spawns; we
no longer mirror it into operator config.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from _1_800_operator.pipeline.readiness import _probe_claude_code

# Candidate paths for Claude Code's user-level MCP config. ~/.claude.json is
# the canonical location for mcpServers; ~/.claude/settings.json is a fallback
# older versions used. First hit wins.
_USER_CONFIG_CANDIDATES = [
    Path.home() / ".claude.json",
    Path.home() / ".claude" / "settings.json",
]


def claude_code_installed_and_logged_in() -> tuple[bool, str]:
    """Public wrapper over readiness._probe_claude_code.

    Returns (ok, reason_if_not_ok). ok=True iff git + claude CLI are on
    PATH and `claude auth status --json` reports loggedIn: true. 5s
    timeout upstream; safe to call from CLI or wizard.
    """
    status, detail = _probe_claude_code(check_auth=True)
    return status == "ok", detail


def user_config_path() -> Optional[Path]:
    """Return the first existing user-level Claude Code config file, or None."""
    for p in _USER_CONFIG_CANDIDATES:
        if p.is_file():
            return p
    return None


def read_user_mcp_config() -> dict:
    """Read ~/.claude.json (or fallback). Returns {} if missing or malformed.

    Does not raise — a malformed config is treated the same as a missing
    one so callers can degrade gracefully on a schema change or user
    hand-edit.
    """
    p = user_config_path()
    if p is None:
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
