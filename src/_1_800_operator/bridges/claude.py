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

# Operator-voice prefix for status / observability messages that operator
# itself authors and posts to chat — connection drops, mid-turn tool
# narration, permission-denial hints. Distinct from REPLY_PREFIX_SLIP so
# meeting participants can tell at a glance whether a chat message is
# Claude speaking (model output) or the operator stepping in to keep the
# room informed. The phone emoji aligns with the 1-800-Operator brand
# metaphor — the operator is the human-style switchboard that connects
# the call and intervenes when something goes sideways.
REPLY_PREFIX_OPERATOR = "[☎️ Operator] "
