"""
Codex CLI preflight checks + read-only inheritance discovery for the codex agent.

`codex_installed_and_logged_in` mirrors
`claude_code_import.py:claude_code_installed_and_logged_in` in shape and
contract. The discovery helpers (`discover_codex_mcps`,
`discover_codex_skills`) feed the setup/edit wizard's read-only
"Codex CLI inheritance" panel — they let the user *see* what
codex will load at runtime without operator pretending to own it.

Architectural posture: codex IS the harness for this agent (operator
spawns `codex mcp-server` and relays chat). Codex's own MCPs and skills
load inside that subprocess from `~/.codex/config.toml` and
`~/.codex/skills/`. Operator does NOT mirror them into its own
mcp_servers block — toggling there wouldn't actually disable anything
in the codex brain. Discovery here is for visibility only.

What this module does NOT do:
  - Mirror codex MCPs into `mcp_servers` for operator-side toggles
    (would be lies; codex still loads them internally).
  - Manage subprocesses — the codex MCP server is plumbed via the
    standard `mcp_servers.codex` block and started by MCPClient.
"""
import json
import logging
import shutil
import subprocess
from pathlib import Path

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


# ── Read-only inheritance discovery (wizard panel only) ───────────────────

# Where codex stores user-managed skills. The `.system/` subdir holds
# codex's own built-ins (skill-creator, openai-docs, imagegen, etc.) —
# we surface those too so the inheritance panel reflects what codex
# actually has loaded, not just what the user added.
_CODEX_HOME = Path.home() / ".codex"
_CODEX_SKILLS_DIR = _CODEX_HOME / "skills"
_CODEX_SYSTEM_SKILLS_DIR = _CODEX_SKILLS_DIR / ".system"

_MCP_LIST_TIMEOUT = 5


def discover_codex_mcps() -> list[tuple[str, str]]:
    """Discover MCP servers configured via `codex mcp add`.

    Returns ``[(name, command_summary)]`` sorted by name. Empty list on
    any failure or when no servers are configured. Read-only — purely
    for the wizard's inheritance display panel.

    `command_summary` is a one-line argv-ish hint
    (e.g. ``"npx -y @modelcontextprotocol/server-foo"``) drawn from the
    server's command + first few args, truncated for terminal width.
    Falls back to a short label when the JSON shape is unfamiliar.
    """
    if shutil.which("codex") is None:
        return []
    try:
        r = subprocess.run(
            ["codex", "mcp", "list", "--json"],
            capture_output=True, text=True, timeout=_MCP_LIST_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning(f"codex mcp list probe failed: {e}")
        return []
    if r.returncode != 0:
        return []

    try:
        payload = json.loads((r.stdout or "").strip() or "[]")
    except json.JSONDecodeError:
        return []

    # codex 0.128 emits a JSON array of {name, command, args, env, ...}
    # objects. Defensive against shape drift across pre-1.0 minor bumps.
    rows: list[tuple[str, str]] = []
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict):
        # Fallback shape: {name: spec} dict.
        entries = [{"name": k, **(v if isinstance(v, dict) else {})}
                   for k, v in payload.items()]
    else:
        return []

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("id") or ""
        if not name:
            continue
        cmd = entry.get("command", "") or ""
        args = entry.get("args") or []
        if not isinstance(args, list):
            args = []
        summary = " ".join([cmd, *args[:3]]).strip() or "(unknown command)"
        rows.append((str(name), summary))

    rows.sort(key=lambda t: t[0])
    return rows


def discover_codex_skills() -> list[tuple[str, str, str]]:
    """Discover skills under `~/.codex/skills/` and `~/.codex/skills/.system/`.

    Returns ``[(name, description, source_label)]`` sorted by name. Last-wins
    dedup so a user-authored skill at `~/.codex/skills/<x>/` shadows a
    `.system/<x>/` built-in of the same name.

    `source_label` is one of:
      - ``"~/.codex/skills/"`` — user-installed
      - ``"~/.codex/skills/.system/"`` — codex built-in

    Read-only; feeds the wizard's inheritance panel only.
    """
    from _1_800_operator.pipeline.skills import _scan_skills_dir

    rows: dict[str, tuple[str, str]] = {}

    # System skills first, so user skills of the same name win in the dedup.
    if _CODEX_SYSTEM_SKILLS_DIR.is_dir():
        for sk in _scan_skills_dir(_CODEX_SYSTEM_SKILLS_DIR):
            rows[sk.name] = (sk.description, "~/.codex/skills/.system/")

    # `_scan_skills_dir` walks one level deep and skips children without a
    # direct SKILL.md, so `.system/` (which is a dir of dirs, not a skill)
    # is silently ignored — exactly what we want.
    if _CODEX_SKILLS_DIR.is_dir():
        for sk in _scan_skills_dir(_CODEX_SKILLS_DIR):
            rows[sk.name] = (sk.description, "~/.codex/skills/")

    return sorted(
        [(name, desc, src) for name, (desc, src) in rows.items()],
        key=lambda t: t[0],
    )
