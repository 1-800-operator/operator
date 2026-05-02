"""
Codex CLI preflight checks for the codex agent.

Mirrors `claude_code_import.py:claude_code_installed_and_logged_in` in
shape and contract. v1 scope: only the install + login + version probe.
The `~/.codex/config.toml` MCP-import scanner is deferred to v2 (see
docs/codex-agent-implementation-plan.md phase 0 / probe 5).

What this module does NOT do:
  - Sync MCPs out of `~/.codex/config.toml` (deferred to v2).
  - Manage subprocesses — the codex MCP server is plumbed via the
    standard `mcp_servers.codex` block and started by MCPClient.
"""
import logging
import shutil
import subprocess

log = logging.getLogger(__name__)


# Tested-against version. Codex CLI is pre-1.0; behavior may shift across
# minor versions. WARN-not-fail on mismatch so users don't get blocked
# by a benign upgrade.
PINNED_VERSION_PREFIX = "0.128."

# `codex login status` typical timeout. The command shouldn't hit network
# in normal cases (reads local credential store), so 5s is generous.
LOGIN_STATUS_TIMEOUT = 5


def codex_installed_and_logged_in() -> tuple[bool, str]:
    """Public preflight gate for `operator run codex`.

    Returns (ok, reason_if_not_ok). ok=True iff:
      1. `codex` is on PATH.
      2. `codex login status` exits 0 with auth-mode in stdout/stderr.
      3. Auth mode is ChatGPT subscription (NOT API key) — the codex
         agent is subscription-only, mirroring claude_cli's posture.

    A version-mismatch (codex CLI != PINNED_VERSION_PREFIX) does NOT
    fail the gate; it logs a WARNING so the user sees the drift in
    /tmp/operator.log.
    """
    if shutil.which("codex") is None:
        return False, (
            "codex CLI not on PATH — install with `npm install -g @openai/codex`"
        )

    # Version probe — log-only, never blocks. Soft warning if drift.
    try:
        v = subprocess.run(
            ["codex", "--version"],
            capture_output=True, text=True, timeout=LOGIN_STATUS_TIMEOUT,
        )
        version_text = (v.stdout or "").strip() + (v.stderr or "").strip()
        if PINNED_VERSION_PREFIX not in version_text:
            log.warning(
                f"codex CLI version drift — operator was tested against "
                f"{PINNED_VERSION_PREFIX}x; running {version_text!r}"
            )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning(f"codex --version probe failed: {e}")

    try:
        r = subprocess.run(
            ["codex", "login", "status"],
            capture_output=True, text=True, timeout=LOGIN_STATUS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, (
            f"`codex login status` did not respond within "
            f"{LOGIN_STATUS_TIMEOUT}s — try running it manually"
        )
    except OSError as e:
        return False, f"could not run `codex login status`: {e}"

    if r.returncode != 0:
        return False, (
            "`codex login status` exited non-zero — run `codex login` to "
            "sign in with your ChatGPT account"
        )

    # `codex login status` writes the auth banner to STDERR (verified phase 0).
    combined = (r.stdout or "") + (r.stderr or "")

    if "ChatGPT" in combined:
        return True, ""

    if "API key" in combined or "API" in combined:
        return False, (
            "codex is logged in with an API key — operator's codex agent "
            "is subscription-only. Run `codex logout` then `codex login` "
            "to switch to ChatGPT subscription auth."
        )

    return False, (
        f"codex not logged in — run `codex login`. (status output: "
        f"{combined.strip()[:200]!r})"
    )
