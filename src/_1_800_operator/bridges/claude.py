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

# slip-mode reply attribution. Locked S193 (Phase 14.19.6) after side-by-side
# eyeball test in a real Meet against three candidates: brackets-only
# (`[Claude] `), emoji-only (`🤖 `), and italics (`_…_`). Hybrid won — brackets
# give at-a-glance scannability, the emoji gives instant visual ID without
# reading, and the literal "Claude" disambiguates which bot when codex/gemini
# bridges land. No user-facing knob; one prefix, hardcoded.
REPLY_PREFIX_SLIP = "[🤖 Claude] "

# dial/deploy modes: claude is a separate participant under its own Google
# account, so attribution is intrinsic — no prefix needed.
REPLY_PREFIX_DIAL = ""
REPLY_PREFIX_DEPLOY = ""


# Base spawn argv for the claude CLI. `--yolo` on slip/dial/deploy appends
# `--dangerously-skip-permissions`. Built lazily so sys.executable is the
# venv's python at call time, not import time.
#
# DUPLICATION — for 14.19.7's implementer:
# These flags are also hardcoded inside `pipeline/providers/claude_cli.py:_spawn`
# (lines ~244-258). Both produce the same argv today, so nothing breaks; but
# they are parallel sources of truth that *will* drift if a flag is added in
# one place and not the other. The intended end state after 14.19.7 is that
# the wizard-era `pipeline/providers/claude_cli.py` is deleted entirely and
# only this function builds claude's argv. Until then, keep the two in sync
# manually.
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
#
# DUPLICATION — for 14.19.7's implementer:
# Same dict shape lives in `pipeline/providers/claude_cli.py:_maybe_write_mcp_config`
# (~line 338-348). Same drift risk as spawn_argv above. Same intended end
# state — wizard-era provider deletes; this function becomes the single
# source of truth.
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
