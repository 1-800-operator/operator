"""Claude Code installation preflight — used at boot.

Thin public wrapper over `readiness._probe_claude_code` so `__main__`
doesn't have to reach into a private helper. Used by `_run_bot` and
`_run_slip` to fail loudly before any browser starts when the `claude`
CLI is missing or not logged in.

Module name is historical (this used to host the wizard's MCP / CLAUDE.md
discovery helpers; the last batch was dropped in 14.19.7-F). Kept as the
preflight home so `__main__`'s import shape stays stable.
"""
from __future__ import annotations

from _1_800_operator.pipeline.readiness import _probe_claude_code


def claude_code_installed_and_logged_in() -> tuple[bool, str]:
    """Returns (ok, reason_if_not_ok).

    ok=True iff git + claude CLI are on PATH and `claude auth status
    --json` reports loggedIn: true. 5s timeout upstream; safe to call
    from CLI or wizard.
    """
    status, detail = _probe_claude_code(check_auth=True)
    return status == "ok", detail
