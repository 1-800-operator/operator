import os
import sys
import yaml
from pathlib import Path
from dotenv import load_dotenv

# Load API keys from the shared user-home .env. Always an absolute path —
# never CWD-relative (pre-session-158 this was default find_dotenv() which
# walked up from CWD; surprises when run from the wrong directory).
load_dotenv(Path.home() / ".brainchild" / ".env")
_AGENTS_DIR = Path.home() / ".brainchild" / "agents"

BOT_NAME = os.environ.get("BRAINCHILD_BOT", "").strip()
if not BOT_NAME:
    sys.stderr.write(
        "ERROR: BRAINCHILD_BOT env var is not set.\n"
        "Run via the CLI: `brainchild run <name> [url]`.\n"
    )
    raise SystemExit(2)

BOT_DIR = _AGENTS_DIR / BOT_NAME
_cfg_path = BOT_DIR / "config.yaml"
if not _cfg_path.exists():
    available = sorted(
        p.name for p in _AGENTS_DIR.iterdir()
        if p.is_dir() and (p / "config.yaml").exists()
    ) if _AGENTS_DIR.exists() else []
    sys.stderr.write(
        f"ERROR: no config found at {_cfg_path}.\n"
        f"Available bots: {', '.join(available) if available else '(none)'}\n"
    )
    raise SystemExit(2)

_config = yaml.safe_load(_cfg_path.read_text())


def _validate_config(cfg, source: Path) -> None:
    """Sanity-check the shape of the loaded agent config.

    Catches the common ways `brainchild edit` or a hand-edit can produce
    a structurally-valid YAML that violates type expectations — e.g. a
    missing newline that turns a list of strings into a dict (the case
    that bit the user in 14.13). Each violation is a single line naming
    the key and what was expected; all violations are surfaced together
    so one round of editing fixes everything.

    Errors here exit the process with a clear stderr message instead of
    letting the bad shape leak into runtime code where it surfaces as a
    `TypeError` ten layers down.
    """
    errors: list[str] = []

    def _require_dict(key, val):
        if val is None:
            errors.append(f"`{key}` is missing — expected a mapping (dict).")
        elif not isinstance(val, dict):
            errors.append(f"`{key}` must be a mapping (dict), got {type(val).__name__}.")

    def _require_str(key, val, *, allow_empty=False):
        if not isinstance(val, str):
            errors.append(f"`{key}` must be a string, got {type(val).__name__}.")
        elif not allow_empty and not val.strip():
            errors.append(f"`{key}` is empty — provide a non-empty string.")

    def _require_list_of_str(key, val):
        if val is None:
            return
        if not isinstance(val, list):
            errors.append(f"`{key}` must be a list of strings, got {type(val).__name__}.")
            return
        for i, item in enumerate(val):
            if not isinstance(item, str):
                errors.append(
                    f"`{key}[{i}]` must be a string, got {type(item).__name__} "
                    f"({item!r}). A common cause is a missing newline or wrong "
                    f"indentation between this entry and the next block."
                )

    if not isinstance(cfg, dict):
        errors.append(f"top-level config must be a mapping, got {type(cfg).__name__}.")
        cfg = {}

    # agent
    agent = cfg.get("agent")
    _require_dict("agent", agent)
    if isinstance(agent, dict):
        _require_str("agent.name", agent.get("name"))

    # llm
    llm = cfg.get("llm")
    _require_dict("llm", llm)
    if isinstance(llm, dict):
        provider = llm.get("provider")
        _require_str("llm.provider", provider)
        if isinstance(provider, str) and provider not in ("openai", "anthropic", "claude_cli"):
            errors.append(
                f"`llm.provider` must be one of openai, anthropic, claude_cli — "
                f"got {provider!r}."
            )
        if isinstance(provider, str) and provider != "claude_cli":
            _require_str("llm.model", llm.get("model"))

    # transcript (optional block)
    transcript = cfg.get("transcript")
    if transcript is not None:
        _require_dict("transcript", transcript)
        if isinstance(transcript, dict):
            ce = transcript.get("captions_enabled")
            if ce is not None and not isinstance(ce, bool):
                errors.append(
                    f"`transcript.captions_enabled` must be true or false, got "
                    f"{type(ce).__name__} ({ce!r})."
                )

    # permissions (optional block)
    permissions = cfg.get("permissions")
    if permissions is not None:
        _require_dict("permissions", permissions)
        if isinstance(permissions, dict):
            _require_list_of_str("permissions.auto_approve", permissions.get("auto_approve"))
            _require_list_of_str("permissions.always_ask", permissions.get("always_ask"))

    # skills (optional block)
    skills = cfg.get("skills")
    if skills is not None:
        _require_dict("skills", skills)
        if isinstance(skills, dict):
            _require_list_of_str("skills.enabled", skills.get("enabled"))
            ext = skills.get("external_paths")
            if ext is not None and "paths" not in skills:
                _require_list_of_str("skills.external_paths", ext)
            pd = skills.get("progressive_disclosure")
            if pd is not None and not isinstance(pd, bool):
                errors.append(
                    f"`skills.progressive_disclosure` must be true or false, got "
                    f"{type(pd).__name__}."
                )

    # mcp_servers (optional block; per-server validation is light — full
    # spawn-time validation lives in mcp_client where the actual connect
    # happens. We just guard against the type-mixing cases.)
    servers = cfg.get("mcp_servers")
    if servers is not None and not isinstance(servers, dict):
        errors.append(
            f"`mcp_servers` must be a mapping of name → server-config, got "
            f"{type(servers).__name__}."
        )
    elif isinstance(servers, dict):
        for name, block in servers.items():
            if not isinstance(block, dict):
                errors.append(
                    f"`mcp_servers.{name}` must be a mapping, got "
                    f"{type(block).__name__}."
                )
                continue
            if "command" in block and not isinstance(block["command"], str):
                errors.append(f"`mcp_servers.{name}.command` must be a string.")
            args = block.get("args")
            if args is not None and not isinstance(args, list):
                errors.append(f"`mcp_servers.{name}.args` must be a list.")
            env = block.get("env")
            if env is not None and not isinstance(env, dict):
                errors.append(f"`mcp_servers.{name}.env` must be a mapping.")

    # personality / ground_rules (optional strings)
    for key in ("personality", "ground_rules"):
        v = cfg.get(key)
        if v is not None and not isinstance(v, str):
            errors.append(f"`{key}` must be a string, got {type(v).__name__}.")

    if errors:
        sys.stderr.write(f"\nCONFIG ERROR in {source}:\n")
        for e in errors:
            sys.stderr.write(f"  - {e}\n")
        sys.stderr.write(
            f"\nFix with `brainchild edit {BOT_NAME}` (or any text editor) "
            f"and re-run.\n"
        )
        raise SystemExit(2)


_validate_config(_config, _cfg_path)

# ── User-facing config (read from agents/<name>/config.yaml) ──────────────

# Agent
AGENT_NAME           = _config["agent"]["name"]
TRIGGER_PHRASE       = _config["agent"].get("trigger_phrase", "@brainchild")
FIRST_CONTACT_HINT   = _config["agent"].get("first_contact_hint", "")
AGENT_TAGLINE        = _config["agent"].get("tagline", "")
INTRO_ON_JOIN        = _config["agent"].get("intro_on_join", True)

# LLM
LLM_PROVIDER           = _config["llm"]["provider"]
# `model` is required for the openai/anthropic providers; claude_cli ignores
# it (claude picks its own model) so we accept absence there. `history_messages`
# governs the JSONL tail size for prompt rebuilds — also meaningless under
# claude_cli (inner-claude owns its own context loop).
LLM_MODEL              = _config["llm"].get("model") if LLM_PROVIDER == "claude_cli" else _config["llm"]["model"]
HISTORY_MESSAGES       = _config["llm"].get("history_messages", 40)

# System prompt is authored as two top-level blocks — `personality` (who the
# bot is, its voice) and `ground_rules` (always-true constraints). They're
# concatenated here with personality first and ground_rules last; rules-last
# gains adherence because LLMs weight end-of-prompt content more heavily.
# Either block may be absent/empty — omitted blocks just drop out.
PERSONALITY   = (_config.get("personality") or "").strip()
GROUND_RULES  = (_config.get("ground_rules") or "").strip()
SYSTEM_PROMPT = "\n\n".join(b for b in (PERSONALITY, GROUND_RULES) if b)

# Skills
#
# Shape (Phase 15.11):
#   skills:
#     enabled: [name1, name2]          # skill names to activate for this agent
#     external_paths: ["~/my-skills"]  # optional extra dirs (tilde-prefixed or absolute)
#     progressive_disclosure: true
#
# Skills are resolved against the shared library at ~/.brainchild/skills/
# plus each external_paths entry. See pipeline.skills.load_skills.
#
# Legacy `skills.paths: [...]` shape (pre-15.11) is still accepted — we
# translate it in-memory: external_paths = the legacy list, enabled =
# every skill name discovered by scanning those paths. A one-line INFO
# nudges the user to re-run `brainchild build` to update the file.
_skills = _config.get("skills") or {}
SKILLS_SHARED_LIBRARY         = Path.home() / ".brainchild" / "skills"
SKILLS_PROGRESSIVE_DISCLOSURE = _skills.get("progressive_disclosure", True)

_legacy_paths = _skills.get("paths")
if "enabled" in _skills or "external_paths" in _skills:
    # New shape — honor explicitly (absent keys default to empty lists).
    SKILLS_ENABLED        = list(_skills.get("enabled") or [])
    SKILLS_EXTERNAL_PATHS = list(_skills.get("external_paths") or [])
elif _legacy_paths:
    # Legacy shape — translate. Derive enabled names by scanning the paths.
    from brainchild.pipeline.skills import _resolve_external_path, _scan_skills_dir
    import logging as _skills_logging
    _derived_names: list[str] = []
    for _p in _legacy_paths:
        # Legacy entries could be relative (e.g. "agents/pm/skills"); accept
        # them here so upgrade doesn't break existing users. New shape
        # (external_paths) rejects relative entries at load time.
        _expanded = Path(os.path.expanduser(str(_p))).resolve()
        for _sk in _scan_skills_dir(_expanded):
            if _sk.name not in _derived_names:
                _derived_names.append(_sk.name)
    SKILLS_ENABLED = _derived_names
    # Keep only tilde/absolute legacy entries as external_paths; relative
    # ones can't be re-resolved reliably at runtime — drop them. The
    # derived-enabled names above still work because the library copy
    # (seeded on first run) will surface them.
    SKILLS_EXTERNAL_PATHS = [
        str(p) for p in _legacy_paths
        if isinstance(p, str) and (p.startswith("~") or p.startswith("/"))
    ]
    _skills_logging.getLogger("config").info(
        f"SKILLS: legacy 'paths' config shape detected — translating in-memory "
        f"to enabled={SKILLS_ENABLED} + external_paths={SKILLS_EXTERNAL_PATHS}. "
        f"Re-run `brainchild build` to update the file."
    )
else:
    SKILLS_ENABLED        = []
    SKILLS_EXTERNAL_PATHS = []

# Transcript (captions)
_transcript = _config.get("transcript", {})
CAPTIONS_ENABLED        = _transcript.get("captions_enabled", False)

# Permissions (track A — claude_cli)
#
# Two lists of native Claude Code tool names. `auto_approve` runs silently
# without bothering the user in chat; `always_ask` always pauses for chat
# confirmation. Tools not in either list also default to confirmation. Used
# by the chat-runner permission handler (step 5c) when it's wired into the
# ClaudeCLIProvider's PreToolUse hook. Empty / absent on track-B bots —
# they go through the existing READ_TOOLS / confirm_tools path instead.
_permissions = _config.get("permissions") or {}
PERMISSIONS_AUTO_APPROVE = list(_permissions.get("auto_approve") or [])
PERMISSIONS_ALWAYS_ASK   = list(_permissions.get("always_ask") or [])

# Voice — controls how the bot communicates across three surfaces:
#   - progress narrator ("Working: …")
#   - confirmation prompts ("Run? …")
#   - reply content (steered by ground_rules directive)
#
# Two modes:
#   "plain"     — meeting-friendly. Translates tool names and args into
#                 plain English ("Checking the code...", "Want me to grab
#                 the Sentry issue?"). Default for new bots.
#   "technical" — developer-flavored. Tool names verbatim, args shown
#                 (with bulk-content collapsed to size hints), full
#                 file:line / code-style replies.
#
# Replaces the deprecated agent.permission_verbosity (terse|verbose)
# field, which only covered the prompt surface. Old configs translate:
#   terse   → plain
#   verbose → technical
import logging as _voice_log
_agent_block = _config.get("agent") or {}
VOICE = (_agent_block.get("voice") or "").lower()
if not VOICE:
    legacy = (_agent_block.get("permission_verbosity") or "").lower()
    if legacy:
        VOICE = "technical" if legacy == "verbose" else "plain"
        _voice_log.getLogger("config").warning(
            f"DEPRECATED: agent.permission_verbosity={legacy!r} translated to "
            f"agent.voice={VOICE!r}. Move the value to agent.voice in the "
            f"bot's config.yaml — permission_verbosity will be removed in a "
            f"future release."
        )
    else:
        VOICE = "plain"
if VOICE not in ("plain", "technical"):
    _voice_log.getLogger("config").warning(
        f"agent.voice={VOICE!r} not recognized (expected 'plain' or "
        f"'technical') — defaulting to 'plain'"
    )
    VOICE = "plain"

# Back-compat shim — older code paths read PERMISSION_VERBOSITY directly.
# Keep the constant alive and keep it in sync with VOICE so callers don't
# silently break during the deprecation window.
PERMISSION_VERBOSITY = "verbose" if VOICE == "technical" else "terse"

# Progress narration: when the bot calls auto-approved tools (Read,
# Grep, etc.) the user otherwise sees nothing until the final reply.
# When enabled, the chat runner emits a one-line "📖 reading X" status
# per tool call, throttled so multi-tool turns don't flood chat.
#   enabled            — master switch (default on for claude_cli bots).
#   min_silence_seconds — only narrate after this many seconds of
#                          chat silence; faster turns stay quiet.
#   throttle_seconds    — minimum gap between narrator messages.
_narration = _agent_block.get("progress_narration") or {}
PROGRESS_NARRATION_ENABLED        = bool(_narration.get("enabled", True))
PROGRESS_NARRATION_MIN_SILENCE_S  = float(_narration.get("min_silence_seconds", 4))
PROGRESS_NARRATION_THROTTLE_S     = float(_narration.get("throttle_seconds", 5))

# ── INTERNAL TUNING ───────────────────────────────────────────────────────
# These used to live in each bot's config.yaml; they're tuned-once internals
# that shipped identical across bots. Edit here to change runtime behavior
# globally.
#
# Tool-call timeout precedence (highest wins):
#   1. `tool_timeout_seconds` on the mcp_servers[<name>] block in a bot's
#      config.yaml — explicit per-bot override the user edits.
#   2. DEFAULT_TOOL_TIMEOUTS[<server_name>] below — ship-level default
#      commensurate with that MCP's typical worst-case task.
#   3. TOOL_TIMEOUT_SECONDS below — global fallback for any server whose
#      name isn't in the map.
ALONE_EXIT_GRACE_SECONDS   = 60    # once we've seen a peer and they leave, exit after this many seconds
LOBBY_WAIT_SECONDS         = 600   # max wait in Meet waiting room for host to admit us
CAPTION_SILENCE_SECONDS    = 0.7   # dead-air gap before a buffered caption chunk commits to history
MAX_TOKENS                 = 2000  # runaway guard on LLM output; "be brief" system-prompt does the real shaping. Bumped from 1000 in session 159 — multi-paragraph skill outputs (codebase-walkthrough, migration-plan) need ~1200–1800 tokens to deliver entry-point through closing-question without truncation.
TOOL_RESULT_MAX_CHARS      = 50000 # truncate a single tool result above this length before feeding to the LLM
TOOL_TIMEOUT_SECONDS       = 60    # global per-tool-call ceiling; per-server default/override beats this
LLM_STUCK_THRESHOLD_SECONDS = 45   # streaming-LLM watchdog: if no token has arrived by this point, post a one-shot "Anthropic is taking longer than usual" notice in chat. Threshold is high enough that healthy calls (TTFT 1–3s, total 3–15s) never trigger; only fires on real server-side spikes (we observed a 49.8s stall once).
BROWSER_PROFILE_DIR        = str(Path.home() / ".brainchild" / "browser_profile")   # persistent Chrome profile (cookies, Google login)
AUTH_STATE_FILE            = str(Path.home() / ".brainchild" / "auth_state.json")    # Playwright storageState JSON for quick re-auth
GOOGLE_ACCOUNT_FILE        = str(Path.home() / ".brainchild" / "google_account.json") # cached {"email": "..."} for the wizard's "✓ signed in as X" detect screen
ENV_FILE                   = str(Path.home() / ".brainchild" / ".env")               # shared .env for API keys; wizard writes here, config + MCPs read here
DEBUG_DIR                  = str(Path.home() / ".brainchild" / "debug")              # screenshots + HTML dumps from save_debug() and adapter failure paths

# Ship-level default per-server timeouts. Intended to reflect each MCP's
# typical worst-case task — generous enough to cover real work, tight enough
# that a truly hung call fails in bounded time. A user can override per-bot
# by setting `tool_timeout_seconds` on the mcp_servers[<name>] block.
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


def relativize_home(p):
    """Return path with $HOME replaced by `~`, else unchanged.

    Used when rendering local paths into strings that will flow to the LLM
    or meeting chat (claude-code footers, log lines). Keeps the absolute path
    off the wire so it doesn't leak the user's directory layout.
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

# ── MCP servers ───────────────────────────────────────────────────────────
import logging as _logging
_mcp_log = _logging.getLogger("config.mcp")

# Env keys a server config must not override — they influence how binaries
# and shared libraries are located when the MCP subprocess launches, so a
# malicious or mistaken config line could redirect execution or preload.
# Exact matches (case-insensitive) and prefix matches for the dyld/ld family.
_UNSAFE_ENV_KEYS = {"PATH", "PYTHONPATH", "PYTHONHOME", "IFS"}
_UNSAFE_ENV_PREFIXES = ("LD_", "DYLD_")


def _is_unsafe_env_key(key: str) -> bool:
    upper = key.upper()
    if upper in _UNSAFE_ENV_KEYS:
        return True
    return any(upper.startswith(p) for p in _UNSAFE_ENV_PREFIXES)


def _resolve_env_vars(env_dict, server_name):
    """Replace ${VAR} references with os.environ values.

    Returns (resolved_dict, missing_vars) — missing_vars lists the ${VAR}
    names that resolved to empty/missing and is persisted on the server
    block so downstream MCP error classification can pre-tag a startup
    failure as "missing_creds" instead of a generic crash.

    Logs a warning for any ${VAR} that resolves to empty, tagged with
    the server name so user-configured MCP issues are easy to spot.
    Drops and warns on keys that could redirect binary or library lookup
    (PATH, PYTHONPATH, LD_*, DYLD_*, …) — those stay bound to the parent
    process environment and must not be overridable from config.
    """
    resolved = {}
    missing_vars = []
    for k, v in env_dict.items():
        if _is_unsafe_env_key(k):
            _mcp_log.warning(
                f"MCP USER CONFIG: server '{server_name}' env key '{k}' is "
                f"refused — cannot override binary/library lookup paths from config"
            )
            continue
        if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
            var_name = v[2:-1]
            value = os.environ.get(var_name, "")
            if not value:
                _mcp_log.warning(
                    f"MCP USER CONFIG: server '{server_name}' env var {var_name} "
                    f"is empty or missing from .env — tool calls may fail at auth time"
                )
                missing_vars.append(var_name)
            resolved[k] = value
        else:
            resolved[k] = v
    return resolved, missing_vars

MCP_SERVERS = {}
# Parallel set of server *names* that are configured but disabled. Kept so
# MCPClient can produce a granular "<server> is disabled" error when the LLM
# calls a tool whose namespaced prefix matches a disabled server, instead of
# the generic "Unknown tool" — see mcp_client.disabled_server_for_tool().
DISABLED_MCP_SERVERS = {}
for _name, _srv in _config.get("mcp_servers", {}).items():
    # Blocks with `enabled: false` are declared but dormant — kept in config so
    # the build wizard can toggle them on without re-authoring env/hints/tools.
    # Default is enabled when the field is absent (backward-compat).
    if not _srv.get("enabled", True):
        DISABLED_MCP_SERVERS[_name] = {}
        continue
    if LLM_PROVIDER == "claude_cli":
        # Track A: claude already knows the command/args/env for its MCP
        # servers (from ~/.claude.json). Our config block is a toggle-only
        # surface so the user can disable individual servers via the wizard
        # to save context. We track the enabled set so step 5d can pass
        # `disabledMcpjsonServers` to claude via --settings, but skip the
        # full operational parse (no env-var resolution, no read/confirm
        # tool sets — claude handles all that itself).
        MCP_SERVERS[_name] = {"_track_a_toggle_only": True}
        continue
    _resolved_env, _missing_vars = _resolve_env_vars(_srv.get("env", {}), _name)
    # Auth style: "env" (API key via .env — default) or "oauth" (mcp-remote
    # browser OAuth, token cached at ~/.mcp-auth/mcp-remote-<version>/<md5(url)>_tokens.json).
    # For "oauth" servers auth_url is required — it's the URL mcp-remote uses
    # to derive the cache key (for Linear that's /mcp, not the /sse arg passed
    # to the binary). MCPClient.connect_all fails fast with kind=oauth_needed
    # if the cache is absent, so OAuth can never hang meeting join.
    _auth = _srv.get("auth", "env")
    if _auth not in ("env", "oauth"):
        _mcp_log.warning(
            f"MCP USER CONFIG: server '{_name}' has unknown auth='{_auth}' — treating as 'env'"
        )
        _auth = "env"
    _auth_url = _srv.get("auth_url", "")
    if _auth == "oauth" and not _auth_url:
        _mcp_log.warning(
            f"MCP USER CONFIG: server '{_name}' has auth='oauth' but no auth_url — "
            f"cache-path check cannot run, server will be treated as needing auth until configured"
        )
    _block = {
        "command": _srv["command"],
        "args": _srv.get("args", []),
        "env": _resolved_env,
        # Unresolved ${VAR} references from .env — consumed by MCPClient's
        # startup classifier to surface "missing_creds" before the binary's
        # crash-on-boot message buries the real cause.
        "missing_vars": _missing_vars,
        "auth": _auth,
        "auth_url": _auth_url,
        # Remediation URL surfaced in the wizard status screen + runtime
        # pre-flight (15.7.4 / 15.7.4.5). Docs/settings page where the user
        # goes to acquire or manage the credential for this server; for
        # OAuth servers this is informational (the real fix is `brainchild
        # auth <name>`).
        "credentials_url": _srv.get("credentials_url", ""),
        "hints": _srv.get("hints", "").strip(),
        "confirm_tools": set(_srv.get("confirm_tools", [])),
        # Tools that auto-execute without user confirmation. Empty set = every
        # tool from this server requires confirmation. Bundle authors declare
        # the read tool names here; the pipeline carries no per-MCP defaults.
        "read_tools": set(_srv.get("read_tools", [])),
    }
    # Optional per-server hard timeout override (e.g. claude-code runs minutes).
    if "tool_timeout_seconds" in _srv:
        _block["tool_timeout_seconds"] = _srv["tool_timeout_seconds"]
    MCP_SERVERS[_name] = _block

# Legacy mcp_servers.<srv>.read_tools / confirm_tools translation (track-B).
#
# Pre-session-169, per-MCP permissions lived under each server block. Now
# the unified `permissions:` block accepts fnmatch globs (`mcp__sentry__get_*`)
# and is the single authority for both tracks. Any legacy entries are
# translated in-memory into namespaced names appended to the right list:
#
#   mcp_servers.linear.read_tools: [pull_request_read]
#     → PERMISSIONS_AUTO_APPROVE += ["mcp__linear__pull_request_read"]
#
# Bundled per-server lists are non-authoritative going forward; new bots
# should write everything in the top-level `permissions:` block. We log a
# one-line deprecation warning per load if any entries were translated, so
# users running with old configs see exactly one nudge per session.
import logging as _perm_log
_legacy_translated = 0
for _name, _block in MCP_SERVERS.items():
    for _tool in sorted(_block.get("read_tools") or []):
        _entry = f"mcp__{_name}__{_tool}"
        if _entry not in PERMISSIONS_AUTO_APPROVE:
            PERMISSIONS_AUTO_APPROVE.append(_entry)
            _legacy_translated += 1
    for _tool in sorted(_block.get("confirm_tools") or []):
        _entry = f"mcp__{_name}__{_tool}"
        if _entry not in PERMISSIONS_ALWAYS_ASK:
            PERMISSIONS_ALWAYS_ASK.append(_entry)
            _legacy_translated += 1
if _legacy_translated:
    _perm_log.getLogger("config").warning(
        f"DEPRECATED: translated {_legacy_translated} per-server "
        f"read_tools/confirm_tools entries into the unified permissions block. "
        f"Move them into permissions.auto_approve / always_ask in the bot's "
        f"config.yaml — the per-server keys will be dropped in a future release."
    )

# Secrets from .env
OPENAI_API_KEY    = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
