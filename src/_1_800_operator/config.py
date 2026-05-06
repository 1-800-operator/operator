"""Operator config — paths, runtime tunables, and claude-bridge constants.

v1 (Phase 14.19.7) ships claude as the only bridge. Every wizard-era knob —
per-bot YAML loading, OPERATOR_BOT env routing, system_prompt composition,
SKILLS_*, PERMISSIONS_*, PROGRESS_NARRATION_*, MCP_SERVERS — is gone. What
remains is:

  1. The runtime tuning block (timeouts, paths) that callers across pipeline/
     and connectors/ read by name. Hardcoded; edit here to change globally.
  2. A small set of claude-bridge constants (AGENT_NAME, TRIGGER_PHRASE,
     HISTORY_MESSAGES, FIRST_CONTACT_HINT, INTRO_ON_JOIN) that LLMClient
     and ChatRunner read by name. Inlined here for now; when a second
     bridge (codex/gemini) lands, callers will pick the right bridge
     module instead.

A handful of empty-collection constants (MCP_SERVERS, PERMISSIONS_*,
SKILLS_*, PROGRESS_NARRATION_*, LLM_MODEL, SYSTEM_PROMPT) are kept as scaffolding
for callers that step F (chat_runner / mcp_client cleanup) hasn't finished
deleting yet. They evaluate to falsy / no-ops so the existing code paths
short-circuit; they go away with their consumers.
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

AGENT_NAME         = "Claude"
TRIGGER_PHRASE     = _claude_bridge.TRIGGER_PHRASE  # "@claude"
HISTORY_MESSAGES   = 40
FIRST_CONTACT_HINT = ""
INTRO_ON_JOIN      = True
CAPTIONS_ENABLED   = False

# Provider — claude_cli is the only brain in v1. Kept as a constant so the
# remaining `if config.LLM_PROVIDER != "claude_cli"` guards in chat_runner /
# mcp_client / __main__ short-circuit cleanly until step F removes them.
LLM_PROVIDER = "claude_cli"
LLM_MODEL    = ""  # claude_cli ignores it — the inner CLI picks its own model

# System prompt: claude reads its own CLAUDE.md and skills natively when the
# binary spawns. Operator no longer composes a prompt; LLMClient just appends
# its SAFETY_RULES (data-vs-instructions guard for <spoken>/<tool_result>).
SYSTEM_PROMPT = ""

# Empty in v1 — claude owns its own MCPs, skills, and permission policy via
# its own settings hierarchy (read by the spawned `claude` binary). These
# constants stay defined so the dead branches in chat_runner / mcp_client /
# __main__ that still reference them short-circuit. Step F deletes the
# consumers and these constants drop entirely.
MCP_SERVERS               = {}
DISABLED_MCP_SERVERS      = {}
PERMISSIONS_AUTO_APPROVE  = []
PERMISSIONS_ALWAYS_ASK    = []
SKILLS_ENABLED            = []
SKILLS_EXTERNAL_PATHS     = []
SKILLS_SHARED_LIBRARY     = Path.home() / ".operator" / "skills"
SKILLS_PROGRESSIVE_DISCLOSURE = True
PROGRESS_NARRATION_ENABLED       = False
PROGRESS_NARRATION_MIN_SILENCE_S = 4.0
PROGRESS_NARRATION_THROTTLE_S    = 5.0


# ── INTERNAL TUNING ───────────────────────────────────────────────────────
# Tuned-once internals — edit here to change runtime behavior globally.
#
# Tool-call timeout precedence (highest wins):
#   1. DEFAULT_TOOL_TIMEOUTS[<server_name>] — ship-level default per MCP,
#      sized to that server's typical worst-case task.
#   2. TOOL_TIMEOUT_SECONDS — global fallback for any server whose name
#      isn't in the map.
ALONE_EXIT_GRACE_SECONDS    = 60     # once we've seen a peer and they leave, exit after this many seconds
HOLD_DURATION_SECONDS       = 2.0    # min gap between "Hold for <bot>..." and the LLM-generated intro, so users register the "connecting you now" beat even when intro generation finishes fast
LOBBY_WAIT_SECONDS          = 600    # max wait in Meet waiting room for host to admit us
CAPTION_SILENCE_SECONDS     = 0.7    # dead-air gap before a buffered caption chunk commits to history
MAX_TOKENS                  = 2000   # runaway guard on LLM output; "be brief" system-prompt does the real shaping
TOOL_RESULT_MAX_CHARS       = 50000  # truncate a single tool result above this length before feeding to the LLM
TOOL_TIMEOUT_SECONDS        = 60     # global per-tool-call ceiling; per-server default beats this
LLM_STUCK_THRESHOLD_SECONDS = 45     # streaming-LLM watchdog: post a one-shot "taking longer than usual" notice if no token has arrived by this point

BROWSER_PROFILE_DIR = str(Path.home() / ".operator" / "browser_profile")     # persistent Chrome profile (cookies, Google login)
AUTH_STATE_FILE     = str(Path.home() / ".operator" / "auth_state.json")     # Playwright storageState JSON for quick re-auth
GOOGLE_ACCOUNT_FILE = str(Path.home() / ".operator" / "google_account.json") # cached {"email": "..."} for the doctor's "✓ signed in as X" detect
ENV_FILE            = str(Path.home() / ".operator" / ".env")                # shared .env for API keys
DEBUG_DIR           = str(Path.home() / ".operator" / "debug")               # screenshots + HTML dumps from save_debug() and adapter failure paths

# Ship-level default per-server timeouts. Generous enough to cover real work,
# tight enough that a truly hung call fails in bounded time.
DEFAULT_TOOL_TIMEOUTS = {
    "claude-code": 600,   # multi-minute coding delegations via `claude -p`
    "playwright":  300,   # browser automation runs
    "figma":        90,   # design-asset fetches
    "github":       60,   # large repo/code searches
    "salesforce":   60,   # heavier org queries
    "notion":       45,   # page/database fetches
    "linear":       30,
    "sentry":       30,
    "slack":        30,
    "calendar":     30,
    "gmail":        30,
    "drive":        30,
}


# Secrets from shared .env. Empty under claude-only v1 (claude_cli doesn't
# need either); kept defined so any straggler import doesn't NameError.
OPENAI_API_KEY    = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


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
