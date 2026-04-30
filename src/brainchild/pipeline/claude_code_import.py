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

# Cache the `claude mcp list` shell-out result for the lifetime of the
# process. The CLI's output is stable once started (no live MCP add/remove
# from inside operator), so paying ~3s per call when boot makes 3 of them
# (sync discovery + sync health + config.py runtime view) is wasteful —
# total boot cost was ~9s pre-cache. None means "not yet fetched"; an
# empty CompletedProcess is a valid cached result (CLI missing/failed).
_CLAUDE_MCP_LIST_CACHE: subprocess.CompletedProcess | None = None


def _claude_mcp_list_cached() -> subprocess.CompletedProcess | None:
    """Run `claude mcp list` once per process and cache the result.

    Returns None if the CLI isn't on PATH or the call times out;
    otherwise returns the CompletedProcess so callers can inspect
    returncode + stdout. Subsequent calls reuse the cached result.

    To bust the cache (e.g. tests adding MCPs mid-process), set
    _CLAUDE_MCP_LIST_CACHE to None.
    """
    global _CLAUDE_MCP_LIST_CACHE
    if _CLAUDE_MCP_LIST_CACHE is not None:
        return _CLAUDE_MCP_LIST_CACHE
    try:
        r = subprocess.run(
            ["claude", "mcp", "list"],
            capture_output=True,
            text=True,
            timeout=_CLAUDE_MCP_LIST_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    _CLAUDE_MCP_LIST_CACHE = r
    return r


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
    r = _claude_mcp_list_cached()
    if r is None or r.returncode != 0:
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

    Shares the cached `claude mcp list` invocation with
    `discover_hosted_mcps_via_cli` (and the runtime view in `config.py`),
    so a single shell-out covers all three call sites — used to be
    ~9s of cold MCP discovery per boot before the cache.
    Returns [] on missing/failing CLI; stdio entries (no URL in CLI
    output) are not included.
    """
    r = _claude_mcp_list_cached()
    if r is None or r.returncode != 0:
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


def read_project_mcp_config(cwd: Optional[Path] = None) -> dict:
    """Read `<cwd>/.mcp.json`. Returns {} if missing or malformed.

    `.mcp.json` is the project-shared scope used by `claude mcp add -s project`
    — it gets checked into the repo so collaborators share an MCP set. Same
    `mcpServers: {...}` shape as `~/.claude.json` (no `projects` nesting).
    """
    base = cwd if cwd is not None else Path.cwd()
    p = base / ".mcp.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def discover_all_mcps() -> tuple[list[ImportedMCP], int]:
    """Full auto-import: merge MCPs from three Claude Code sources.

      1. `~/.claude.json#mcpServers` (user-scope) plus
         `~/.claude.json#projects.<cwd>.mcpServers` (local-scope, `-s local`).
      2. `<cwd>/.mcp.json#mcpServers` (project-shared scope, `-s project` —
         lives in the repo so collaborators share the MCP set).
      3. `claude mcp list` output (claude.ai-hosted connectors).

    Dedup by slugified name; first source wins on collision. Order matches
    the CLI's own precedence sense (local > project > user > hosted).

    Returns (mcps, http_sse_wrapped_count). The wrapped count is the number
    of HTTP/SSE entries we routed through mcp-remote, used for wizard /
    first-run summary strings.
    """
    from_json, wrapped_json = extract_imported_mcps(read_user_mcp_config())
    from_project, wrapped_project = extract_imported_mcps(read_project_mcp_config())
    from_cli = discover_hosted_mcps_via_cli()

    out: list[ImportedMCP] = []
    seen: set[str] = set()
    for m in from_json:
        key = _slugify_mcp_name(m.name)
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    for m in from_project:
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
    return out, wrapped_json + wrapped_project + wrapped_cli


def normalize_path_for_storage(p: Path, cwd: Optional[Path] = None) -> str:
    """Render `p` in the smartest portable form for config storage:

      - Under `$HOME` → `~/relative/...`  (survives cross-machine config sync)
      - Under `cwd` → `./relative/...`    (re-resolves against current launch dir)
      - Otherwise → absolute path string  (brittle, won't survive moves)

    Mirrors the resolution rules in `config._resolve_claude_md_path`. The
    wizard uses this when persisting user-chosen paths into config so the
    same path round-trips cleanly across machines and project moves.
    """
    if cwd is None:
        cwd = Path.cwd()
    p = p.resolve()
    home = Path.home().resolve()
    cwd = cwd.resolve()
    try:
        rel = p.relative_to(home)
        return f"~/{rel}" if str(rel) else "~"
    except ValueError:
        pass
    try:
        rel = p.relative_to(cwd)
        return f"./{rel}" if str(rel) else "."
    except ValueError:
        pass
    return str(p)


def discover_claude_md_sources(
    cwd: Optional[Path] = None,
) -> list[tuple[str, str]]:
    """Return present CLAUDE.md sources as `(short_label, content)` tuples,
    in walk order. Empty list if none exist.

    Walks (in order):
      1. ~/.claude/CLAUDE.md (user-scope)
      2. <cwd>/CLAUDE.md (project root)
      3. <cwd>/.claude/CLAUDE.md (project Claude Code dir)

    Mirrors how Claude Code itself walks both scopes. Returning the list
    (vs. just merged content) lets callers show accurate provenance in
    UI prompts — the wizard previously hardcoded `~/.claude/CLAUDE.md`
    in its append prompt even when content came from project-scope.

    `cwd` defaults to Path.cwd(); tests can pin it.
    """
    if cwd is None:
        cwd = Path.cwd()
    candidates = [
        ("~/.claude/CLAUDE.md", _USER_CLAUDE_MD),
        ("./CLAUDE.md", cwd / "CLAUDE.md"),
        ("./.claude/CLAUDE.md", cwd / ".claude" / "CLAUDE.md"),
    ]
    found: list[tuple[str, str]] = []
    for label, path in candidates:
        if not path.is_file():
            continue
        try:
            found.append((label, path.read_text()))
        except OSError:
            continue
    return found


def read_user_claude_md(cwd: Optional[Path] = None) -> Optional[str]:
    """Return concatenated CLAUDE.md content from user-scope and
    project-scope, or None if no source exists.

    Single source returns bare content (preserves old contract); multiple
    sources are joined with `# CLAUDE.md — <label>` section headers so
    provenance survives into ground_rules.
    """
    found = discover_claude_md_sources(cwd)
    if not found:
        return None
    if len(found) == 1:
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
