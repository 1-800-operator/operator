"""Claude Code bridge — hardcoded constants for the slip/dial/deploy commands.

Replaces the wizard-era `agents/claude/config.yaml` + per-agent config-loading
machinery. v0.0.1 ships claude only; codex and gemini get their own bridge
modules later (Phase 14.20+). Bridges are per-CLI; modes (slip/dial/deploy)
are per-posture and branched in `chat_runner` / `__main__`.

Persistent state files (`auth_state.json`, `browser_profile/`, `history/`,
`debug/`) still live under `~/.operator/`, written by code only — there are
no user-editable config files in the bridge model.
"""

from __future__ import annotations

import sys


TRIGGER_PHRASE = "@claude"

# slip-mode reply attribution. Locked in 14.19.6 after side-by-side mockup
# in a real Meet — this is the leading default; brackets and italics are
# still candidates until then.
REPLY_PREFIX_SLIP = "🤖 "

# dial/deploy modes: claude is a separate participant under its own Google
# account, so attribution is intrinsic — no prefix needed.
REPLY_PREFIX_DIAL = ""
REPLY_PREFIX_DEPLOY = ""


# Base spawn argv for the claude CLI. `--yolo` on slip/dial/deploy appends
# `--dangerously-skip-permissions`. Built lazily so sys.executable is the
# venv's python at call time, not import time.
def spawn_argv(yolo: bool = False) -> list[str]:
    argv = [
        "claude",
        "-p",
        "--output-format", "stream-json",
        "--input-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
    ]
    if yolo:
        argv.append("--dangerously-skip-permissions")
    return argv


# Per-spawn MCP server registration. Operator's bundled transcript server
# (search_captions / list_captions / list_speakers) reads the meeting JSONL
# at OPERATOR_MEETING_RECORD_PATH. Claude inherits MCPs from its own
# settings hierarchy; this MCP block is appended via `--mcp-config`.
def transcript_mcp_spec(meeting_record_path: str) -> dict:
    return {
        "mcpServers": {
            "transcript": {
                "command": sys.executable,
                "args": ["-m", "_1_800_operator.mcp_servers.transcript_server"],
                "env": {
                    "OPERATOR_MEETING_RECORD_PATH": meeting_record_path,
                },
            }
        }
    }
