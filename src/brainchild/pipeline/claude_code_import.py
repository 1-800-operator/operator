"""Auto-import helpers for the `claude` bundled agent — Phase 15.9, 15.11.

Discovers the user's existing Claude Code configuration and returns
structured records the wizard or first-run bootstrap can merge into the
`claude` agent's config.yaml. Two discovery sources:

  1. `~/.claude.json` top-level `mcpServers` (locally-configured stdio
     and HTTP/SSE MCPs). Often empty — Claude Code power users tend to
     configure MCPs elsewhere.
  2. `claude mcp list` (text output). This is the authoritative source
     for claude.ai-hosted MCPs (Gmail, Drive, Linear, etc.) that live
     in the user's claude.ai account connectors, not in any local file.

Skills at `~/.claude/skills/` are NOT imported here as of Phase 15.11
— the bundled claude config ships with
`skills.external_paths: [~/.claude/skills]`, so the skills loader picks
them up live without a copy. `read_user_claude_md()` stays because
CLAUDE.md feeds `ground_rules`, not skills.

Transport handling: Claude Code's MCPs may be stdio (local subprocess
with `command`+`args`) or remote (HTTP / SSE via `url`). Brainchild's
mcp_client is stdio-only, but we already wrap hosted servers (Linear,
Sentry) with `mcp-remote` — the same bridge works for imported HTTP/SSE
entries. They get auto-wrapped rather than skipped.

Most functions are pure (no side effects). `append_env_placeholders` is
the one exception — it appends commented placeholders to the user's
.env file. Callers control when that happens.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from brainchild.pipeline.readiness import _probe_claude_code

# Mirror the mcp-remote version pinned in the bundled Linear/Sentry blocks
# so imported hosted MCPs use the same bridge we've already pressure-tested.
_MCP_REMOTE_VERSION = "0.1.38"

_ENV_REF_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")

# Candidate paths for Claude Code's user-level MCP config. ~/.claude.json is
# the canonical location for mcpServers; ~/.claude/settings.json is a fallback
# older versions used. First hit wins.
_USER_CONFIG_CANDIDATES = [
    Path.home() / ".claude.json",
    Path.home() / ".claude" / "settings.json",
]
_USER_CLAUDE_MD = Path.home() / ".claude" / "CLAUDE.md"


def claude_code_installed_and_logged_in() -> tuple[bool, str]:
    """Public wrapper over readiness._probe_claude_code.

    Returns (ok, reason_if_not_ok). ok=True iff git + claude CLI are on
    PATH and `claude auth status --json` reports loggedIn: true. 5s
    timeout upstream; safe to call from CLI or wizard.
    """
    status, detail = _probe_claude_code(check_auth=True)
    return status == "ok", detail


@dataclass
class ImportedMCP:
    """One MCP entry ready to merge into config.yaml's mcp_servers block."""
    name: str
    block: dict  # YAML-ready mapping (command, args, env, auth, etc.)
    transport: str  # "stdio" | "http" | "sse"
    env_vars_referenced: list[str] = field(default_factory=list)


def user_config_path() -> Optional[Path]:
    """Return the first existing user-level Claude Code config file, or None."""
    for p in _USER_CONFIG_CANDIDATES:
        if p.is_file():
            return p
    return None


def read_user_mcp_config() -> dict:
    """Read ~/.claude.json (or fallback). Returns {} if missing or malformed.

    Does not raise — a malformed config is treated the same as a missing
    one so the wizard can surface "no importable MCPs" without blowing up
    on a schema change or user hand-edit.
    """
    p = user_config_path()
    if p is None:
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _classify_transport(entry: dict) -> str:
    """stdio | http | sse. Claude Code treats `command`+`args` as stdio,
    and `url` (with optional `type: http|sse`) as remote. When a URL is
    present without explicit type, default to http.
    """
    t = entry.get("type")
    if t in ("http", "sse"):
        return t
    if entry.get("url"):
        return "http"
    return "stdio"


def _wrap_http_as_stdio(entry: dict, transport: str) -> dict:
    """Convert an HTTP/SSE entry into a stdio block wrapped by mcp-remote.

    Sets auth=oauth + auth_url so the existing Phase 15.7.3 OAuth token-cache
    gate (readiness.oauth_cache_exists) and the `brainchild auth <name>`
    flow work unchanged on imported hosted MCPs.
    """
    url = entry.get("url") or ""
    return {
        "enabled": True,
        "description": f"imported from ~/.claude.json ({transport} via mcp-remote)",
        "auth": "oauth",
        "auth_url": url,
        "command": "npx",
        "args": ["-y", f"mcp-remote@{_MCP_REMOTE_VERSION}", url],
        "env": {},
        "read_tools": [],
        "confirm_tools": [],
        "hints": "",
    }


def _stdio_block_from_entry(entry: dict) -> dict:
    """Convert a Claude Code stdio MCP entry to the Brainchild config shape."""
    block = {
        "enabled": True,
        "description": "imported from ~/.claude.json (stdio)",
        "auth": "env",
        "command": entry.get("command", ""),
        "args": list(entry.get("args") or []),
        "env": dict(entry.get("env") or {}),
        "read_tools": [],
        "confirm_tools": [],
        "hints": "",
    }
    return block


def _collect_mcp_servers_from_cfg(cfg: dict, cwd: Optional[Path] = None) -> dict:
    """Merge user-scope `mcpServers` with project-scope mcpServers for the
    current cwd. Project scope wins on collision — `claude mcp list` follows
    the same precedence, and `claude mcp add` defaults entries to project
    scope, so most user-added MCPs live there.
    """
    merged = dict(cfg.get("mcpServers") or {})
    cwd_str = str(cwd if cwd is not None else Path.cwd())
    proj = (cfg.get("projects") or {}).get(cwd_str) or {}
    for name, entry in (proj.get("mcpServers") or {}).items():
        merged[name] = entry
    return merged


def extract_imported_mcps(cfg: dict, cwd: Optional[Path] = None) -> tuple[list[ImportedMCP], int]:
    """Pull mcpServers out of the claude-code config, classify transport,
    and wrap HTTP/SSE entries with mcp-remote.

    Walks both user-scope (`mcpServers`) and project-scope
    (`projects.<cwd>.mcpServers`) entries. `cwd` defaults to the process
    cwd; tests can pin it.

    Returns (mcps, http_sse_wrapped_count). The count is informational —
    callers can surface "N hosted MCPs wrapped via mcp-remote" in the
    wizard summary. No entries are silently dropped; malformed entries
    (non-dict, no command and no url) are skipped.
    """
    servers = _collect_mcp_servers_from_cfg(cfg, cwd)
    out: list[ImportedMCP] = []
    wrapped = 0
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        # Skip entries with neither a command nor a URL — nothing to run.
        if not (entry.get("command") or entry.get("url")):
            continue
        transport = _classify_transport(entry)
        if transport in ("http", "sse"):
            block = _wrap_http_as_stdio(entry, transport)
            wrapped += 1
        else:
            block = _stdio_block_from_entry(entry)
        env_refs: list[str] = []
        for v in (block.get("env") or {}).values():
            if isinstance(v, str):
                env_refs.extend(_ENV_REF_RE.findall(v))
        out.append(ImportedMCP(
            name=name,
            block=block,
            transport=transport,
            env_vars_referenced=sorted(set(env_refs)),
        ))
    return out, wrapped


# Matches one MCP line in `claude mcp list` output, e.g.:
#   "claude.ai Linear: https://mcp.linear.app/sse - ✓ Connected"
#   "claude.ai Gmail: https://gmailmcp.googleapis.com/mcp/v1 - ! Needs authentication"
#   "sentry: https://mcp.sentry.dev/mcp (HTTP) - ✓ Connected"
# The `(HTTP)` annotation is optional — claude-code prints it for
# HTTP-not-SSE remote MCPs. Tolerant of format drift via non-match skip.
_CLAUDE_MCP_LIST_RE = re.compile(
    r"^(?P<name>.+?):\s+(?P<url>https?://\S+)(?:\s+\([A-Z]+\))?\s+-\s+(?P<status>.+?)\s*$"
)

_CLAUDE_MCP_LIST_TIMEOUT = 10.0


def _slugify_mcp_name(raw: str) -> str:
    """Convert a display name like 'claude.ai Linear' to a YAML key like
    'claude-ai-linear'. Lowercased, non-alnum runs collapsed to a single
    hyphen, leading/trailing hyphens stripped.
    """
    s = raw.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-") or "imported"


def discover_hosted_mcps_via_cli() -> list[ImportedMCP]:
    """Shell out to `claude mcp list` and wrap each hosted MCP via mcp-remote.

    The CLI is the only authoritative source for claude.ai-hosted
    connectors (Gmail, Drive, Linear, etc. that come from the user's
    claude.ai account, not from any local file). We parse the text
    output line by line and skip anything that doesn't match the regex —
    if Claude Code's format drifts we degrade to zero results, not a
    traceback.

    Connection status is not surfaced — every hosted MCP goes through
    Brainchild's own `brainchild auth <name>` flow regardless. If Claude
    Code says "Connected", that's a claude.ai-side token; Brainchild
    needs its own mcp-remote OAuth cache.

    Returns [] if the CLI isn't available, times out, or exits non-zero.
    """
    try:
        r = subprocess.run(
            ["claude", "mcp", "list"],
            capture_output=True,
            text=True,
            timeout=_CLAUDE_MCP_LIST_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []

    out: list[ImportedMCP] = []
    seen_keys: set[str] = set()
    for line in r.stdout.splitlines():
        m = _CLAUDE_MCP_LIST_RE.match(line)
        if not m:
            continue
        name = _slugify_mcp_name(m.group("name"))
        if name in seen_keys:
            continue
        seen_keys.add(name)
        url = m.group("url")
        transport = "sse" if url.endswith("/sse") else "http"
        block = _wrap_http_as_stdio({"url": url, "type": transport}, transport)
        block["description"] = (
            f"imported from `claude mcp list` ({transport} via mcp-remote, "
            f"originally: {m.group('name').strip()})"
        )
        out.append(ImportedMCP(
            name=name,
            block=block,
            transport=transport,
            env_vars_referenced=[],
        ))
    return out


def discover_mcp_health() -> list[tuple[str, str, str, bool]]:
    """Run `claude mcp list` and return (name, url, status, healthy) per
    parseable line. Healthy iff the status field starts with the ✓ marker
    claude-code uses for connected.

    Use this to surface pre-flight MCP health on the claude-agent boot
    path: an MCP that needs reauth or has failed to connect would
    otherwise only manifest in-meeting as tools silently not working.

    Shells out a second time after `discover_hosted_mcps_via_cli` —
    accepted cost for v1; can be unified with a parsed-once cache later.
    Returns [] on missing/failing CLI; stdio entries (no URL in CLI
    output) are not included.
    """
    try:
        r = subprocess.run(
            ["claude", "mcp", "list"],
            capture_output=True,
            text=True,
            timeout=_CLAUDE_MCP_LIST_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []

    out: list[tuple[str, str, str, bool]] = []
    for line in r.stdout.splitlines():
        m = _CLAUDE_MCP_LIST_RE.match(line)
        if not m:
            continue
        name = m.group("name").strip()
        url = m.group("url")
        status = m.group("status").strip()
        healthy = status.startswith("✓")
        out.append((name, url, status, healthy))
    return out


def discover_all_mcps() -> tuple[list[ImportedMCP], int]:
    """Full auto-import: merge `~/.claude.json#mcpServers` with
    `claude mcp list` output. Dedup by slugified name (local config wins
    on collision because it usually has richer metadata).

    Returns (mcps, http_sse_wrapped_count). The count is informational
    for wizard / first-run summary strings.
    """
    from_json, wrapped_json = extract_imported_mcps(read_user_mcp_config())
    from_cli = discover_hosted_mcps_via_cli()

    out: list[ImportedMCP] = []
    seen: set[str] = set()
    for m in from_json:
        key = _slugify_mcp_name(m.name)
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    wrapped_cli = 0
    for m in from_cli:
        if m.name in seen:
            continue
        seen.add(m.name)
        out.append(m)
        if m.transport in ("http", "sse"):
            wrapped_cli += 1
    return out, wrapped_json + wrapped_cli


def read_user_claude_md(cwd: Optional[Path] = None) -> Optional[str]:
    """Return concatenated CLAUDE.md content from user-scope and
    project-scope, or None if no source exists.

    Walks (in order):
      1. ~/.claude/CLAUDE.md (user-scope)
      2. <cwd>/CLAUDE.md (project root)
      3. <cwd>/.claude/CLAUDE.md (project Claude Code dir)

    Each present source is included with a section header so the wizard's
    merge into ground_rules carries full provenance. Mirrors how Claude
    Code itself walks both scopes — without this the wizard's CLAUDE.md
    merge view would only show user-scope rules even when the project
    has its own CLAUDE.md sitting next to the bot.

    `cwd` defaults to Path.cwd(); tests can pin it.
    """
    if cwd is None:
        cwd = Path.cwd()
    sources = [
        ("user (~/.claude/CLAUDE.md)", _USER_CLAUDE_MD),
        ("project (./CLAUDE.md)", cwd / "CLAUDE.md"),
        ("project (./.claude/CLAUDE.md)", cwd / ".claude" / "CLAUDE.md"),
    ]
    found: list[tuple[str, str]] = []
    for label, path in sources:
        if not path.is_file():
            continue
        try:
            found.append((label, path.read_text()))
        except OSError:
            continue
    if not found:
        return None
    if len(found) == 1:
        # Single source: return bare content (preserves old contract;
        # callers like the wizard can show provenance separately).
        return found[0][1]
    return "\n\n".join(f"# CLAUDE.md — {label}\n{content}" for label, content in found)


def append_env_placeholders(var_names: Iterable[str], env_file: Path) -> list[str]:
    """Idempotently append `# VAR_NAME=` placeholders for each var that is
    not already present in env_file (either set or already placeheld).
    Creates env_file + parent dir if missing.

    Returns the sorted list of newly-added var names (empty if nothing
    was added). A header comment is written once per invocation that
    actually adds vars — not per var — so repeat runs that add more
    leave two separate commented sections, which is useful provenance.
    """
    env_file = Path(env_file)
    existing: set[str] = set()
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            stripped = line.strip().lstrip("#").strip()
            m = re.match(r"([A-Z_][A-Z0-9_]*)\s*=", stripped)
            if m:
                existing.add(m.group(1))

    to_add: list[str] = []
    for v in var_names:
        if v in existing:
            continue
        to_add.append(v)
        existing.add(v)

    if not to_add:
        return []

    env_file.parent.mkdir(parents=True, exist_ok=True)
    existing_bytes = env_file.read_bytes() if env_file.is_file() else b""
    needs_leading_nl = bool(existing_bytes) and not existing_bytes.endswith(b"\n")
    with env_file.open("a", encoding="utf-8") as f:
        if needs_leading_nl:
            f.write("\n")
        f.write("\n# Added by brainchild — claude-agent MCP import\n")
        for v in sorted(to_add):
            f.write(f"# {v}=\n")
    return sorted(to_add)
