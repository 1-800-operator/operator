"""Claude Code bridge — hardcoded constants for the claude CLI.

Replaces the wizard-era `agents/claude/config.yaml` + per-agent config-loading
machinery (deleted in Phase 14.19.7). v1 ships claude only; codex/gemini
bridges would be sibling modules under `bridges/`.

Persistent state files (`history/`, `debug/`) live under `~/.operator/`,
written by code only — there are no user-editable config files in the
bridge model.
"""

from __future__ import annotations


TRIGGER_PHRASE = "@claude"

# slip-mode reply attribution. Locked S193 (Phase 14.19.6) after side-by-side
# eyeball test in a real Meet against three candidates: brackets-only
# (`[Claude] `), emoji-only (`🤖 `), and italics (`_…_`). Hybrid won — brackets
# give at-a-glance scannability, the emoji gives instant visual ID without
# reading, and the literal "Claude" disambiguates which bot when codex/gemini
# bridges land. No user-facing knob; one prefix, hardcoded.
REPLY_PREFIX_SLIP = "[🤖 Claude] "

# There is no separate operator voice. Everything the bot says in a
# meeting goes out under REPLY_PREFIX_SLIP — operator is invisible
# plumbing, not a switchboard persona. The old REPLY_PREFIX_OPERATOR
# ("[☎️ Operator] ") and its send-without-prefix path were removed in
# S228 along with the operator-side narration callbacks.
