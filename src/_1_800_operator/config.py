"""Operator config — paths, runtime tunables, and claude-bridge constants.

v1 (Phase 14.19.7) ships claude as the only bridge. Wizard-era knobs
(per-bot YAML loading, OPERATOR_BOT routing, system_prompt composition,
SKILLS_*, PERMISSIONS_*, PROGRESS_NARRATION_*, MCP_SERVERS, OpenAI /
Anthropic API keys) are all gone — claude reads its own ~/.claude/
hierarchy natively when the binary spawns. What remains is:

  1. The runtime tuning block (timeouts, paths) that callers across
     pipeline/ and connectors/ read by name. Hardcoded; edit here to
     change runtime behavior globally.
  2. A small set of claude-bridge constants (AGENT_NAME, TRIGGER_PHRASE,
     HISTORY_MESSAGES) that LLMClient and ChatRunner read by name.
     Inlined here for now; when a second bridge (codex/gemini) lands,
     callers will pick the right bridge module instead.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load API keys from the shared user-home .env. Always an absolute path —
# never CWD-relative (pre-session-158 was default find_dotenv() which walked
# up from CWD; surprises when run from the wrong directory).
load_dotenv(Path.home() / ".operator" / ".env")


# ── Claude bridge constants ──────────────────────────────────────────────
# v1 ships claude only. TRIGGER_PHRASE is sourced from bridges/claude.py
# (the single source of truth for claude-specific values); the other names
# are hardcoded here. When codex/gemini bridges land these split into per-
# bridge modules and callers pick the right one.
from _1_800_operator.bridges import claude as _claude_bridge

AGENT_NAME       = "Claude"
TRIGGER_PHRASE   = _claude_bridge.TRIGGER_PHRASE  # "@claude"
HISTORY_MESSAGES = 40


# ── INTERNAL TUNING ───────────────────────────────────────────────────────
# Tuned-once internals — edit here to change runtime behavior globally.
ALONE_EXIT_GRACE_SECONDS    = 60     # once we've seen a peer and they leave, exit after this many seconds
LOBBY_WAIT_SECONDS          = 600    # max wait in Meet waiting room for host to admit us
MAX_TOKENS                  = 2000   # runaway guard on LLM output; "be brief" system-prompt does the real shaping

ENV_FILE            = str(Path.home() / ".operator" / ".env")                # shared .env for API keys
DEBUG_DIR           = str(Path.home() / ".operator" / "debug")               # screenshots + HTML dumps from save_debug() and adapter failure paths


def relativize_home(p):
    """Return path with $HOME replaced by `~`, else unchanged.

    Used when rendering local paths into strings that flow to the LLM or
    meeting chat. Keeps the absolute path off the wire so it doesn't leak
    the user's directory layout.
    """
    if not p:
        return p
    p = str(p)
    home = str(Path.home())
    if p == home:
        return "~"
    if p.startswith(home + os.sep):
        return "~" + p[len(home):]
    return p
