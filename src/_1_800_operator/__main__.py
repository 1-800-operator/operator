"""
Operator — AI Meeting Participant
Cross-platform entry point. Auto-detects OS and dispatches to the right adapter.

Usage:
    operator dial <name> <url> Dial named agent into a specific Meet
    operator dial <name>       Auto-open a new Meet, dial in that bot
    operator try <name>       Terminal test-drive (no Meet)
    operator build            Create a new agent (wizard)
    operator edit <target>    Edit an agent config (wizard, surgical) or .env (in $EDITOR)
    operator where <target>   Print the absolute path of a config file
    operator                  Print usage + agent list
"""
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

_AGENTS_DIR = Path.home() / ".operator" / "agents"
_BUNDLED_AGENTS_DIR = Path(__file__).resolve().parent / "agents"
_SKILLS_DIR = Path.home() / ".operator" / "skills"
_BUNDLED_SKILLS_DIR = Path(__file__).resolve().parent / "skills"


# MCP-server overlay fields the user owns via `operator edit claude`.
# Persisted to disk; preserved across boots and across cwd switches.
# Anything outside this set (command, args, env, auth, auth_url, description)
# is rediscovered fresh on every boot from cwd-aware `discover_all_mcps()`
# and never written to disk — Claude Code's project-scope `mcpServers`
# semantics make discovery cwd-sensitive, so persisting source-driven
# fields would either flap by cwd or hide user edits behind stale state.
_CLAUDE_MCP_OVERLAY_FIELDS = (
    "enabled", "hints", "read_tools", "confirm_tools", "tool_timeout_seconds",
)


def _sync_claude_imports() -> None:
    """Sync the `claude` agent's MCP overlay with the user's Claude Code config.

    Runs on every boot of the claude agent. Maintains a slim *overlay* in
    ~/.operator/agents/claude/config.yaml's `mcp_servers` block —
    persisting only fields the user owns (`enabled`, `hints`, `read_tools`,
    `confirm_tools`, `tool_timeout_seconds`). Source-driven fields
    (command/args/env/auth/auth_url/description) are NEVER stored on disk
    and are rediscovered fresh on every boot from cwd-aware
    `discover_all_mcps()`.

    Why overlay-only: Claude Code's project-scope `mcpServers` are
    cwd-sensitive by design. Persisting the full discovered block on
    disk meant switching cwds rewrote the cfg with whatever the new cwd
    saw — pruning project-scope MCPs from "wrong" cwds and clobbering
    the user's hand-authored hints. The overlay model says: cfg is the
    user's authored truth (toggles + hints), cwd determines what's
    *active* this run, never what's *persisted*.

    Behavior:
      - Discovered MCP not in overlay → adds `{enabled: True}` (default-on
        for first sight; wizard's MCP toggle step lets the user disable).
      - Discovered MCP already in overlay → no change. User's enable/hints
        survive untouched.
      - In overlay but not in current discovery → DORMANT for this run.
        Overlay entry persists. config.py's runtime view excludes it
        (banner + LLM tools). Reactivates next time it's rediscovered.
      - The only way to remove a dormant entry is `operator edit claude`
        (explicit user action) or a full `operator build` reset.

    The `~3s` cost of `claude mcp list` is paid every boot. Acceptable
    because discovery is the live source of truth — caching across boots
    would defeat the cwd-aware semantics.

    Comments in config.yaml are lost on rewrite (ruamel round-trip
    preserves them, but we still only write when the overlay actually
    changed to keep formatting churn off no-op boots).
    """
    cfg_path = _AGENTS_DIR / "claude" / "config.yaml"
    if not cfg_path.is_file():
        return

    from _1_800_operator.pipeline.setup import _load_yaml, _dump_yaml
    try:
        cfg = _load_yaml(cfg_path)
    except Exception:
        return

    from _1_800_operator.pipeline.claude_code_import import (
        _slugify_mcp_name,
        append_env_placeholders,
        discover_all_mcps,
        discover_mcp_health,
    )

    mcps, wrapped = discover_all_mcps()
    existing_overlay = cfg.get("mcp_servers") or {}
    if not isinstance(existing_overlay, dict):
        existing_overlay = {}

    def _compact(prev: dict | None) -> dict:
        """Carry forward only meaningful overlay fields. `enabled` always
        anchors the row (defaults True if absent); other fields drop when
        empty so the cfg stays terse.
        """
        row: dict = {}
        if isinstance(prev, dict):
            for k in _CLAUDE_MCP_OVERLAY_FIELDS:
                if k not in prev:
                    continue
                v = prev[k]
                if k == "enabled":
                    row["enabled"] = bool(v)
                elif v:  # drop empty list / "" / 0
                    row[k] = v
        row.setdefault("enabled", True)
        return row

    new_overlay: dict = {}
    added: list[str] = []
    env_refs: set[str] = set()
    discovered_names = {m.name for m in mcps}

    for m in mcps:
        prev = existing_overlay.get(m.name)
        new_overlay[m.name] = _compact(prev if isinstance(prev, dict) else None)
        if not isinstance(prev, dict):
            added.append(m.name)
        env_refs.update(m.env_vars_referenced)

    # Dormant rows: in overlay but not in current discovery. Keep the row
    # (overlay is the persistence layer; cwd determines what's active this
    # run, never what's persisted).
    dormant: list[str] = []
    for name, prev in existing_overlay.items():
        if name in discovered_names:
            continue
        new_overlay[name] = _compact(prev if isinstance(prev, dict) else None)
        dormant.append(name)

    env_file = Path.home() / ".operator" / ".env"
    placeheld: list[str] = []
    if env_refs:
        placeheld = append_env_placeholders(sorted(env_refs), env_file)

    cfg.pop("_claude_import_done", None)
    cfg["mcp_servers"] = new_overlay

    on_disk_cfg = _load_yaml(cfg_path)
    on_disk_servers = on_disk_cfg.get("mcp_servers") or {}
    if dict(new_overlay) != dict(on_disk_servers) or "_claude_import_done" in on_disk_cfg:
        try:
            _dump_yaml(cfg, cfg_path)
        except OSError as e:
            print(f"[claude] WARN: could not write {cfg_path}: {e}", file=sys.stderr)
            return

    parts = []
    if added:
        parts.append(f"+{len(added)} ({', '.join(added)})")
    if dormant:
        parts.append(f"~{len(dormant)} dormant ({', '.join(dormant)})")
    if parts:
        msg = f"[claude] MCP sync: {' '.join(parts)}"
        if wrapped and added:
            msg += f" — {wrapped} hosted wrapped via mcp-remote"
        print(msg, file=sys.stderr)
    if placeheld:
        print(
            f"[claude] added {len(placeheld)} env placeholder(s) to {env_file}: "
            f"{', '.join(placeheld)} — edit the file to fill in values.",
            file=sys.stderr,
        )

    # Pre-flight MCP health: surface anything `claude mcp list` already
    # marks as unhealthy (e.g. "Needs authentication", "Failed to connect")
    # to stderr before meeting join. Filter by overlay's `enabled` so
    # disabled servers don't generate noise.
    for name, _url, status, healthy in discover_mcp_health():
        if healthy:
            continue
        slug = _slugify_mcp_name(name)
        srv = new_overlay.get(slug)
        if isinstance(srv, dict) and not srv.get("enabled", True):
            continue
        print(
            f"[claude] ⚠ MCP needs attention: {name} — {status} "
            f"→ run `operator auth {slug}`",
            file=sys.stderr,
        )


def _ensure_user_agents():
    """Sync-on-every-run: copy any bundled bot that is missing from the
    user's ~/.operator/agents/ dir. Existing user bots are never touched
    or overwritten — only missing ones are seeded.

    Runs from main() before any CLI dispatch. This keeps new bundled agents
    (e.g., `claude` added post-first-run) discoverable by existing users
    without forcing them to delete their agents dir. Tradeoff: if a user
    deliberately deletes a bundled bot, it reappears on next run — delete
    it again, or configure around it.
    """
    import shutil
    if not _BUNDLED_AGENTS_DIR.exists():
        return
    _AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    for bundled in _BUNDLED_AGENTS_DIR.iterdir():
        if not bundled.is_dir():
            continue
        dest = _AGENTS_DIR / bundled.name
        if not dest.exists():
            shutil.copytree(bundled, dest)


def _migrate_legacy_user_artifacts():
    """One-shot relocation of `browser_profile/`, `auth_state.json`, and `.env`
    from the dev-mode repo root (pre-Phase-14.5) into `~/.operator/`.

    Pre-fix, three user-scoped artifacts were pinned at the repo root: the
    Playwright persistent profile, the Google-session cookie export, and the
    shared `.env`. Each used a different mechanism to get there (`_BASE`
    walk-up for the first two, `_ROOT` walk-up in setup.py for `.env`); all
    three broke for installed/site-packages use and collided across dev
    checkouts. This shim picks up legacy copies on the user's machine so
    Google login and API keys survive the move.

    Idempotent: if the target already exists we leave the legacy copy in
    place rather than overwriting — the user can reconcile manually.
    Silent no-op on fresh installs or after the first successful run.
    """
    import shutil
    home_dir = Path.home() / ".operator"
    home_dir.mkdir(parents=True, exist_ok=True)

    # Dev-mode repo root — src/operator/__main__.py → up 3 levels.
    repo_root = Path(__file__).resolve().parent.parent.parent

    for name in ("browser_profile", "auth_state.json", ".env"):
        src = repo_root / name
        dst = home_dir / name
        if dst.exists() or not src.exists():
            continue
        try:
            shutil.move(str(src), str(dst))
            print(f"[operator] migrated {name} → {dst}", file=sys.stderr)
        except OSError as e:
            print(
                f"[operator] WARN: could not migrate {src} → {dst}: {e}",
                file=sys.stderr,
            )


def _ensure_user_skills():
    """Sync-on-every-run: copy any bundled skill that is missing from the
    user's ~/.operator/skills/ dir. Existing user skills are never touched
    or overwritten — only missing ones are seeded.

    Same shape as `_ensure_user_agents` — additive, non-destructive. A user
    who edits a bundled skill post-seed keeps their edits on subsequent
    runs. A user who deletes a bundled skill sees it reappear on next run
    (matches the agents-dir behavior).
    """
    import shutil
    if not _BUNDLED_SKILLS_DIR.exists():
        return
    _SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    for bundled in _BUNDLED_SKILLS_DIR.iterdir():
        if not bundled.is_dir():
            continue
        dest = _SKILLS_DIR / bundled.name
        if not dest.exists():
            shutil.copytree(bundled, dest)


# ── Prevent Ctrl+C from killing child processes ────────────────────
# Playwright's Node.js driver and Chrome are child processes in our
# terminal's foreground process group.  When the user presses Ctrl+C,
# the terminal sends SIGINT to the whole group — killing Chrome
# abruptly and leaving it in the meeting for ~60s until Meet's
# heartbeat times out.
#
# Fix: put every child in its own session (setsid) so SIGINT only
# reaches our Python process.  We then close Chrome cleanly via
# Playwright, and Meet sees an immediate disconnect.
_OriginalPopenInit = subprocess.Popen.__init__


def _detached_popen_init(self, *args, **kwargs):
    kwargs.setdefault("start_new_session", True)
    _OriginalPopenInit(self, *args, **kwargs)


subprocess.Popen.__init__ = _detached_popen_init


def _kill_orphaned_children():
    """Last-resort cleanup: kill any child processes that survived graceful shutdown."""
    import signal as _sig
    import subprocess as _sp
    import time as _time

    pid = os.getpid()
    try:
        result = _sp.run(
            ["pgrep", "-P", str(pid)],
            capture_output=True, text=True, timeout=3,
            start_new_session=False,
        )
    except Exception:
        return

    child_pids = [int(p) for p in result.stdout.strip().split("\n") if p.strip()]
    if not child_pids:
        return

    import logging
    log = logging.getLogger("operator")

    labeled = []
    for cpid in child_pids:
        try:
            r = _sp.run(
                ["ps", "-o", "command=", "-p", str(cpid)],
                capture_output=True, text=True, timeout=1,
                start_new_session=False,
            )
            cmd = r.stdout.strip().replace("\n", " ")
        except Exception:
            cmd = ""
        labeled.append(f"{cpid} ({cmd})" if cmd else str(cpid))
    log.warning(f"Safety net: killing {len(child_pids)} orphaned child process(es): [{', '.join(labeled)}]")

    for cpid in child_pids:
        try:
            os.kill(cpid, _sig.SIGTERM)
        except ProcessLookupError:
            pass

    _time.sleep(0.5)

    for cpid in child_pids:
        try:
            os.kill(cpid, 0)
            os.kill(cpid, _sig.SIGKILL)
            log.warning(f"Safety net: SIGKILL sent to pid {cpid}")
        except ProcessLookupError:
            pass


# Short, single-clause hint per MCP startup-failure kind. Keep these
# under ~30 chars so the roll-up line stays scannable when several
# servers fail at once.
_MCP_KIND_HINT = {
    "missing_creds":   "missing {vars}",
    "binary_missing":  "binary not found",
    "startup_timeout": "didn't respond",
    "handshake_crash": "crashed on startup",
    "oauth_needed":    "run `operator auth {name}`",
    "unknown":         "error",
}


def _emit_mcp_rollup(mcp, connector=None):
    """Print one ▸ line summarizing per-server MCP connect status.

    Silent when there are no MCP servers configured or the MCPClient
    didn't run (Track A: claude_cli owns its own MCPs). On mixed
    success, failed servers get a terse remediation hint pulled from
    `_MCP_KIND_HINT` so the user knows the exact next command without
    digging through /tmp/operator.log.

    If `connector` is provided AND any servers failed, also drop a
    single chat-side one-liner so meeting participants see the
    degraded state without having to inspect the host terminal.
    Successful-only loads stay terminal-only — chat doesn't need to
    say "everything's fine."
    """
    from _1_800_operator import config
    from _1_800_operator.pipeline import ui
    if not mcp or not config.MCP_SERVERS:
        return
    failures = getattr(mcp, "startup_failures", {}) or {}
    parts = []
    for name in config.MCP_SERVERS:
        info = failures.get(name)
        if not info:
            parts.append(f"{name} ✓")
            continue
        kind = info.get("kind", "unknown")
        template = _MCP_KIND_HINT.get(kind, "error")
        if kind == "missing_creds":
            vars_list = info.get("vars") or []
            vars_str = vars_list[0] if len(vars_list) == 1 else "credentials"
            hint = template.format(vars=vars_str)
        elif kind == "oauth_needed":
            hint = template.format(name=name)
        else:
            hint = template
        parts.append(f"{name} ✗ ({hint})")
    ui.say("MCP: " + " · ".join(parts))

    if connector is not None and failures:
        failed_names = [n for n in failures]
        suffix = "" if len(failed_names) == 1 else "s"
        chat_msg = (
            f"⚠ {len(failed_names)} MCP server{suffix} failed to start: "
            f"{', '.join(failed_names)}. Tools from {'it' if len(failed_names) == 1 else 'them'} "
            f"won't work this meeting — see host terminal or `tail /tmp/operator.log` for details."
        )
        try:
            connector.send_chat(chat_msg)
        except Exception as e:
            import logging as _logging
            _logging.getLogger("operator").warning(
                f"could not surface MCP failure to chat: {e}"
            )


def _print_startup_banner(skills):
    """Print the face + identity + loadout banner as the boot splash.

    Must fire BEFORE MCP / browser startup logs so it sits at the top of the
    terminal like a fighter-select splash, not buried mid-scroll. MCP server
    names come from config (known without connecting); per-server ✓/✗ status
    is left to the existing connect logs rather than duplicated here.

        ▄▄▄▄▄▄   <AgentName>
        █ ▲▲ █   <tagline>
        █ ══ █   linear · github · 4 skills · claude-sonnet-4-5
        ▀▀▀▀▀▀

    Also triggers the first-run portrait hook: any bot without a committed
    portrait.txt gets one minted from the deterministic glyph generator.
    """
    from _1_800_operator import config
    import sys as _sys
    import shutil as _shutil
    from _1_800_operator.pipeline import face
    from rich.cells import cell_len

    bot_name = os.environ.get("OPERATOR_BOT", "")
    portrait_path = _AGENTS_DIR / bot_name / "portrait.txt"

    # First-run hook — contributor-added bot with no portrait gets one minted.
    if bot_name and not portrait_path.exists():
        if face.write_if_missing(bot_name, portrait_path):
            import logging
            logging.getLogger("operator").info(
                f"minted fresh portrait: {portrait_path}"
            )

    face_text = face.load_or_render(bot_name, portrait_path=portrait_path)
    face_lines = face_text.split("\n")

    sep = " · "
    parts = list(config.MCP_SERVERS.keys())
    n_skills = len(skills) if skills else 0
    if n_skills:
        parts.append(f"{n_skills} skills")
    parts.append(config.LLM_MODEL or f"{config.LLM_PROVIDER} (subscription)")
    loadout = sep.join(parts)

    gap = "   "
    face_w = max((cell_len(fl) for fl in face_lines), default=0)
    term_w = _shutil.get_terminal_size((80, 20)).columns
    right_w = max(20, term_w - face_w - cell_len(gap))

    def _wrap(text: str) -> list[str]:
        if not text:
            return [""]
        if cell_len(text) <= right_w:
            return [text]
        out: list[str] = []
        cur = ""
        for word in text.split(" "):
            candidate = f"{cur} {word}" if cur else word
            if cell_len(candidate) <= right_w:
                cur = candidate
                continue
            if cur:
                out.append(cur)
                cur = ""
            while cell_len(word) > right_w:
                out.append(word[:right_w])
                word = word[right_w:]
            cur = word
        if cur:
            out.append(cur)
        return out

    right: list[str] = []
    for entry in (config.AGENT_NAME, config.AGENT_TAGLINE, loadout):
        right.extend(_wrap(entry))

    pad_face = " " * face_w
    n_rows = max(len(face_lines), len(right))
    print("", file=_sys.stderr)
    for i in range(n_rows):
        fl = face_lines[i] if i < len(face_lines) else pad_face
        rt = right[i] if i < len(right) else ""
        print(f"{fl}{gap}{rt}".rstrip(), file=_sys.stderr)
    print("", file=_sys.stderr)


def _available_bots():
    if not _AGENTS_DIR.exists():
        return []
    return sorted(
        p.name for p in _AGENTS_DIR.iterdir()
        if p.is_dir() and (p / "config.yaml").exists()
    )


def _bot_tagline(name):
    # Prefer the explicit agent.tagline in config.yaml; fall back to the first
    # non-header line of README.md for older bots that pre-date the field.
    cfg = _AGENTS_DIR / name / "config.yaml"
    if cfg.exists():
        try:
            import yaml
            data = yaml.safe_load(cfg.read_text()) or {}
            tag = ((data.get("agent") or {}).get("tagline") or "").strip()
            if tag:
                return tag
        except Exception:
            pass
    readme = _AGENTS_DIR / name / "README.md"
    if not readme.exists():
        return ""
    lines = readme.read_text().splitlines()
    seen_h1 = False
    for line in lines:
        stripped = line.strip()
        if not seen_h1:
            if stripped.startswith("# "):
                seen_h1 = True
            continue
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def _print_usage():
    print("Usage:")
    print("  operator dial <name> [url]      Dial an agent into a Meet (auto-opens one if no url)")
    print("  operator deploy <name> <url>    Send an agent into an existing meeting (Phase 14.19)")
    print("  operator slip <name> [url]      Attach an agent to your own Chrome session (Phase 14.19)")
    print("  operator login <name>           Sign into Google for dial/deploy (Phase 14.19.4)")
    print("  operator try <name>             Terminal test-drive (no Meet)")
    print("  operator build                  Create a new agent (wizard)")
    print("  operator auth <mcp>             Authorize an OAuth MCP (Linear, etc.)")
    print("  operator edit <target>          Open an agent config (or .env) in $EDITOR")
    print("  operator where <target>         Print the absolute path of a config file")
    print()
    print("Flags:")
    print("  --force                         Retry join even if a session is flagged stuck")
    print("  --no-preflight                  Skip the MCP readiness check (for CI/scripted launches)")
    print("  --yolo                          Skip per-tool permission prompts (dial/deploy/slip)")
    print()
    bots = _available_bots()
    if bots:
        print("Available bots:")
        for b in bots:
            tag = _bot_tagline(b)
            print(f"  {b:<12} {tag}")


def _resolve_config_target(target):
    """Map a user-supplied target to an on-disk path under ~/.operator/.

    Accepts a bot name (`claude`, `pm`, …) or the special token `.env` / `env`.
    Returns (path, error_message); exactly one is non-None.
    """
    home = Path.home() / ".operator"
    if target in (".env", "env"):
        return home / ".env", None
    if target in _available_bots():
        return _AGENTS_DIR / target / "config.yaml", None
    return None, f"Unknown target: {target!r}. Expected a bot name or `.env`."


def _run_edit(argv):
    """`operator edit <name>` is the surgical-modify path. For bot
    names, run the same wizard as `operator build` minus the
    "reset to bundled?" gate — every step pre-loaded with current state,
    user accepts/changes each one, atomic write at the end. For `.env`
    (which has no toggleable shape), keep the $EDITOR flow.
    """
    if not argv:
        print("Usage: operator edit <bot-name | .env>\n")
        _print_usage()
        return 2
    target = argv[0]
    if target in (".env", "env"):
        return _run_edit_env_file()
    if target not in _available_bots():
        print(
            f"Unknown target: {target!r}. Expected a bot name or `.env`.\n"
            f"Available bots: {', '.join(sorted(_available_bots())) or '(none)'}"
        )
        _print_usage()
        return 2
    from _1_800_operator.pipeline.install_preflight import run_install_preflight
    run_install_preflight()
    from _1_800_operator.pipeline.setup import run as _wizard_run
    return _wizard_run([], target_agent=target, reset_allowed=False)


def _run_edit_env_file():
    """`operator edit .env` keeps the $EDITOR flow — env-var key/value
    pairs are the wrong shape for a TUI."""
    import shlex
    path = Path.home() / ".operator" / ".env"
    if not path.exists():
        print(f"Config file does not exist: {path}")
        return 1
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    cmd = shlex.split(editor) + [str(path)]
    subprocess.call(cmd)
    # Editors (vim especially) sometimes exit nonzero for benign reasons —
    # swapfile noise, terminal weirdness — even when the user saved cleanly.
    # Don't propagate that to the shell prompt; the only thing that matters
    # is whether the file still exists. Print a positive signal so the user
    # has no ambiguity about whether their edit landed.
    if path.exists():
        print(f"Saved {path}")
        return 0
    print(f"File no longer exists: {path}")
    return 1


def _run_where(argv):
    if not argv:
        print("Usage: operator where <bot-name | .env>\n")
        _print_usage()
        return 2
    path, err = _resolve_config_target(argv[0])
    if err:
        print(err)
        _print_usage()
        return 2
    print(path)
    return 0


def _run_setup():
    from _1_800_operator.pipeline.install_preflight import run_install_preflight
    run_install_preflight()
    from _1_800_operator.pipeline.setup import run as _wizard_run
    return _wizard_run([])


def _run_login(name):
    """Single-purpose Google sign-in for dial/deploy (Phase 14.19.4).

    Wraps `_launch_signin_flow` from the wizard's step-2 code without any
    of the wizard's prompt scaffolding. Idempotent — running twice
    refreshes the session via Google's logout flow so the user lands on
    the account picker instead of being silently re-recognized.

    Slip mode launches its own dedicated Chrome under
    `~/.operator/slip_profile/`, which is independent of `auth_state.json`
    and lives outside this command's reach. login is for the headless
    profile that dial/deploy share.
    """
    if name not in _available_bots():
        print(f"Unknown bot: {name!r}\n")
        _print_usage()
        return 2

    from _1_800_operator.pipeline.google_signin import (
        _AUTH_STATE_FILE,
        _BROWSER_PROFILE_DIR,
        _GOOGLE_ACCOUNT_FILE,
        _launch_signin_flow,
        detect_google_session,
    )

    detected = detect_google_session(_AUTH_STATE_FILE, _GOOGLE_ACCOUNT_FILE)
    sign_out_first = detected.detected
    if detected.detected and detected.email:
        print(f"Currently signed in as {detected.email}. Refreshing session…")
    elif detected.detected:
        print("Existing Google session detected. Refreshing…")
    else:
        print("No Google session yet. Opening sign-in window…")

    try:
        email = _launch_signin_flow(
            _BROWSER_PROFILE_DIR,
            _AUTH_STATE_FILE,
            _GOOGLE_ACCOUNT_FILE,
            sign_out_first=sign_out_first,
        )
    except KeyboardInterrupt:
        print("\nAborted.")
        return 130
    except Exception as e:
        print(f"Sign-in failed: {e}")
        return 1

    if email:
        print(f"✓ signed in as {email}")
    else:
        print("✓ Google session saved")
    return 0


# _run_auth and _find_oauth_mcp_config moved to operator.pipeline.auth in
# Phase 15.7.4 so the wizard's inline "authorize now?" prompt and this CLI
# dispatch hit the same code path. Re-exported here for backward compat.
from _1_800_operator.pipeline.auth import (
    find_oauth_mcp_config as _find_oauth_mcp_config,
    run_auth as _run_auth,
)


def main():
    # Strip group/world bits from anything we create under ~/.operator/.
    # Files are born 0o600 and dirs 0o700 with this mask, closing the
    # mkdir → chmod race for callers that don't pass mode= explicitly.
    # Only touches files this process creates; existing files keep their
    # current perms.
    os.umask(0o077)
    _migrate_legacy_user_artifacts()
    _ensure_user_agents()
    _ensure_user_skills()
    argv = sys.argv[1:]

    if not argv or argv[0] in ("-h", "--help"):
        _print_usage()
        return 0

    first = argv[0]

    if first in ("build", "setup"):
        # `setup` kept as undocumented alias for muscle memory after the
        # build rename — not advertised in --help; safe to drop later.
        if len(argv) > 1:
            print(f"Unexpected argument after 'build': {argv[1]!r}\n")
            _print_usage()
            return 2
        return _run_setup()
    if first == "try":
        if len(argv) < 2:
            print("Usage: operator try <name>\n")
            _print_usage()
            return 2
        return _run_try(argv[1])
    if first == "auth":
        if len(argv) != 2:
            print("Usage: operator auth <mcp>\n")
            _print_usage()
            return 2
        return _run_auth(argv[1])
    if first == "login":
        if len(argv) != 2:
            print("Usage: operator login <name>\n")
            _print_usage()
            return 2
        return _run_login(argv[1])
    if first == "edit":
        return _run_edit(argv[1:])
    if first == "where":
        return _run_where(argv[1:])
    # `run` kept as a hidden alias for muscle memory + external links after
    # the dial rename — not advertised in --help; safe to drop later.
    if first in ("dial", "run"):
        if len(argv) < 2:
            print("Usage: operator dial <name> [url]\n")
            _print_usage()
            return 2
        name = argv[1]
        if name not in _available_bots():
            print(f"Unknown bot: {name!r}\n")
            _print_usage()
            return 2
        rest, yolo = _consume_yolo(argv[2:])
        if yolo:
            os.environ["OPERATOR_YOLO"] = "1"
        return _run_bot(name, rest)

    # Phase 14.19.2 — `deploy <name> <url>`. Sends agent as a separate
    # participant into an existing meeting. URL required (no meet.new
    # auto-open). Routes through the same `_run_bot` path as dial; the
    # only difference at this level is URL-required.
    if first == "deploy":
        if len(argv) < 3:
            print("Usage: operator deploy <name> <url>\n")
            _print_usage()
            return 2
        name = argv[1]
        url = argv[2]
        if name not in _available_bots():
            print(f"Unknown bot: {name!r}\n")
            _print_usage()
            return 2
        rest, yolo = _consume_yolo(argv[3:])
        if yolo:
            os.environ["OPERATOR_YOLO"] = "1"
        return _run_bot(name, [url] + rest)

    # Phase 14.19.2/3 — `slip <name> <url>`. CDP-attach to user's existing
    # Chrome session; agent responds *as the user* with a marker prefix.
    # claude-only in v0.0.1; URL required (no meet.new auto-open in slip
    # because the meeting is whatever the user has open).
    if first == "slip":
        if len(argv) < 2:
            print("Usage: operator slip claude <https://meet.google.com/xxx-xxxx-xxx>\n")
            _print_usage()
            return 2
        name = argv[1]
        if name not in _available_bots():
            print(f"Unknown bot: {name!r}\n")
            _print_usage()
            return 2
        rest, yolo = _consume_yolo(argv[2:])
        if yolo:
            os.environ["OPERATOR_YOLO"] = "1"
        return _run_slip(name, rest)

    if first.startswith("-"):
        print(f"Unknown option: {first}\n")
        _print_usage()
        return 2

    # Phase 15.8: bare `operator <name>` is no longer accepted. A known bot
    # name gets a pointed hint pointing at the new form; anything else falls
    # through to the generic unknown-subcommand message.
    if first in _available_bots():
        print(
            f"Dial agents via `operator dial {first}`. "
            f"Bare `operator {first}` is no longer supported.\n"
        )
        return 2
    print(f"Unknown bot or subcommand: {first!r}\n")
    _print_usage()
    return 2


def _run_try(name):
    """Terminal test-drive — boot the full pipeline (LLM + MCP + skills) against
    a stdin/stdout connector instead of a Meet. Mirrors _run_macos up to the
    browser join, but synchronous MCP startup (no browser to overlap with) and
    a plain 'chat ready' banner on stderr.
    """
    if name not in _available_bots():
        print(f"Unknown bot: {name!r}\n")
        _print_usage()
        return 2

    # Must land before any `from _1_800_operator import config`.
    os.environ["OPERATOR_BOT"] = name

    import logging
    import signal
    import time as _time

    logging.basicConfig(
        filename="/tmp/operator.log",
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    # Keep stderr clean — terminal UX is the chat itself. Logs stay in /tmp/operator.log.
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)

    log = logging.getLogger("operator")

    from _1_800_operator import config
    from _1_800_operator.connectors.terminal import TerminalConnector
    from _1_800_operator.pipeline import ui
    from _1_800_operator.pipeline.chat_runner import ChatRunner
    from _1_800_operator.pipeline.llm import LLMClient
    from _1_800_operator.pipeline.meeting_record import MeetingRecord
    from _1_800_operator.pipeline.providers import build_provider
    from _1_800_operator.pipeline.skills import load_skills

    skills = load_skills(
        config.SKILLS_ENABLED,
        external_paths=config.SKILLS_EXTERNAL_PATHS,
        shared_library_dir=config.SKILLS_SHARED_LIBRARY,
    )
    _print_startup_banner(skills)

    llm = LLMClient(build_provider())
    llm.inject_skills(skills, config.SKILLS_PROGRESSIVE_DISCLOSURE)

    mcp = None
    # Track A (claude_cli): the user's claude subprocess owns its own MCP servers
    # via ~/.claude.json — operator's own MCPClient must not try to connect to
    # them (the config blocks are toggle-only stubs without `command`/`args`).
    if config.MCP_SERVERS and config.LLM_PROVIDER != "claude_cli":
        from _1_800_operator.pipeline.mcp_client import MCPClient
        mcp = MCPClient()
        try:
            mcp.connect_all()
            llm.inject_mcp_hints(config.MCP_SERVERS)
            loaded = [n for n in config.MCP_SERVERS if n not in mcp.startup_failures]
            llm.inject_mcp_status(loaded, mcp.startup_failures)
            gh_login = mcp.resolve_github_user()
            if gh_login:
                llm.inject_github_user(gh_login)
        except Exception as e:
            log.error(f"MCP client startup failed: {e}")
            ui.err("MCP startup failed")
            mcp = None

    connector = TerminalConnector(bot_name=config.AGENT_NAME)
    slug = f"terminal-{int(_time.time())}"
    record = MeetingRecord(slug=slug, meta={"mode": "terminal", "bot": name})
    llm.set_record(record)

    print("\nchat ready — type to message, /quit or Ctrl+D to exit\n", file=sys.stderr)

    runner = ChatRunner(
        connector,
        llm,
        mcp_client=mcp,
        meeting_record=record,
        skills=skills,
        skills_progressive=config.SKILLS_PROGRESSIVE_DISCLOSURE,
    )

    _shutdown_called = False

    def _shutdown(signum=None, frame=None):
        nonlocal _shutdown_called
        if _shutdown_called:
            return
        _shutdown_called = True
        if signum:
            log.info(f"Received signal {signum} — shutting down")
        runner.stop()
        if mcp:
            mcp.shutdown()
        connector.leave()
        _kill_orphaned_children()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        runner.run(meeting_url=None)
    except KeyboardInterrupt:
        log.info("Interrupted — exiting terminal test-drive")
    finally:
        _shutdown()
        ui.ok("Goodbye.")
    return 0


def _consume_yolo(args):
    """Strip `--yolo` from argv list; return (filtered_args, yolo_bool).

    Centralized so dial/deploy/slip get identical handling. The flag
    appends `--dangerously-skip-permissions` to the spawned `claude` CLI
    via the OPERATOR_YOLO env var read in providers/claude_cli.py:_spawn.
    """
    yolo = "--yolo" in args
    return [a for a in args if a != "--yolo"], yolo


def _run_slip(name, rest):
    """slip mode — launch a dedicated Chrome window for the meeting and
    CDP-attach claude to it.

    Slip Chrome lives at ~/.operator/slip_profile/ — operator-owned,
    separate from the user's main browser. First run: user signs into
    Google in slip Chrome once, cookies persist for future sessions.
    User's main Chrome is never touched.

    Pipeline mirrors _run_macos's construction but swaps connectors,
    skips the meet.new auto-open (slip always takes a URL), and drops
    the user-browser auto-open (slip Chrome IS where the meeting opens).
    Track A only — claude owns its MCPs; no MCPClient setup.

    OPERATOR_BOT=claude is set early (temporary; 14.19.7 deletes the
    config layer entirely).
    """
    if name != "claude":
        print(
            f"slip mode is claude-only in v0.0.1; got {name!r}. "
            f"Use `operator dial {name}` or `operator deploy {name} <url>` instead.",
            file=sys.stderr,
        )
        return 2

    url = None
    for arg in rest:
        if arg.startswith("-"):
            print(f"Unknown flag: {arg}", file=sys.stderr)
            return 2
        elif url is None:
            url = arg
        else:
            print(f"Unexpected argument: {arg}", file=sys.stderr)
            return 2

    if not url:
        print(
            "slip requires a Meet URL: operator slip claude <https://meet.google.com/xxx-xxxx-xxx>",
            file=sys.stderr,
        )
        return 2

    if sys.platform != "darwin":
        print(
            "slip mode is currently macOS-only. Use `operator dial claude` or "
            "`operator deploy claude <url>` on Linux.",
            file=sys.stderr,
        )
        return 2

    # Set OPERATOR_BOT before any config import — config.py exits 2 on import
    # without it. Pipeline modules read it transitively. Phase 14.19.7
    # collapses this when the config layer dies.
    os.environ["OPERATOR_BOT"] = name

    # claude binary preflight — same gate _run_bot uses for the claude agent.
    # Fail loud and early; no browser dance, no config load if claude isn't
    # installed or logged in.
    from _1_800_operator.pipeline.claude_code_import import (
        claude_code_installed_and_logged_in,
    )
    ok, reason = claude_code_installed_and_logged_in()
    if not ok:
        print(
            f"\nslip claude requires the Claude Code CLI.\n"
            f"  {reason}\n"
            f"\nInstall Claude Code (https://claude.ai/code) and run "
            f"`claude login`, then re-run.\n",
            file=sys.stderr,
        )
        return 2

    # Sync claude MCPs (parity with _run_bot — picks up servers added/removed
    # in ~/.claude.json since last run).
    import time as _time_init
    _t_sync = _time_init.monotonic()
    _sync_claude_imports()

    import logging
    import signal
    import time as _time

    logging.basicConfig(
        filename="/tmp/operator.log",
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)
    log = logging.getLogger("operator")
    log.info(f"TIMING claude_sync={_time.monotonic() - _t_sync:.2f}s")

    from _1_800_operator import config
    from _1_800_operator.bridges import claude as claude_bridge
    from _1_800_operator.connectors.attach_adapter import AttachAdapter, SlipAttachError
    from _1_800_operator.pipeline import ui
    from _1_800_operator.pipeline.chat_runner import ChatRunner
    from _1_800_operator.pipeline.llm import LLMClient
    from _1_800_operator.pipeline.meeting_record import MeetingRecord, slug_from_url
    from _1_800_operator.pipeline.providers import build_provider
    from _1_800_operator.pipeline.skills import load_skills

    # Track A guard — slip is claude-only and claude is the only track-A
    # provider. If something exotic is wired in config, fail before the
    # user's Chrome gets quit.
    if config.LLM_PROVIDER != "claude_cli":
        print(
            f"slip mode requires the claude_cli LLM provider; "
            f"config has {config.LLM_PROVIDER!r}. Re-import claude or check "
            f"~/.operator/agents/claude/config.yaml.",
            file=sys.stderr,
        )
        return 2

    t_start = _time.monotonic()

    skills = load_skills(
        config.SKILLS_ENABLED,
        external_paths=config.SKILLS_EXTERNAL_PATHS,
        shared_library_dir=config.SKILLS_SHARED_LIBRARY,
    )
    _print_startup_banner(skills)

    # Build meeting record up-front — URL is known, no meet.new resolution
    # gymnastics needed. The transcript MCP server (spawned by claude via
    # --mcp-config) reads from this path.
    slug = slug_from_url(url)
    meeting_record = MeetingRecord(slug=slug, meta={"meet_url": url, "mode": "slip"})

    llm = LLMClient(build_provider())
    llm.inject_skills(skills, config.SKILLS_PROGRESSIVE_DISCLOSURE)
    llm.set_record(meeting_record)

    # Active-meeting marker (parity with _run_macos — useful for any
    # static-config MCPs that need the active meeting JSONL path).
    try:
        marker = Path.home() / ".operator" / ".current_meeting"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(meeting_record.path), encoding="utf-8")
    except OSError as e:
        log.warning(f"could not write current-meeting marker: {e}")

    connector = AttachAdapter(reply_prefix=claude_bridge.REPLY_PREFIX_SLIP)

    ui.say("Launching slip Chrome…")
    try:
        connector.join(url)
    except SlipAttachError as e:
        ui.err(str(e))
        return 2

    log.info(f"TIMING setup={_time.monotonic() - t_start:.1f}s")
    runner = ChatRunner(
        connector,
        llm,
        mcp_client=None,  # track A — claude owns its MCPs
        meeting_record=meeting_record,
        skills=skills,
        skills_progressive=config.SKILLS_PROGRESSIVE_DISCLOSURE,
        # slip is "speak when spoken to": no intro, no Hold-for-Claude
        # filler, no 1-on-1 trigger bypass. claude only responds when
        # explicitly @claude'd. dial/deploy leave this default.
        quiet_mode=True,
    )

    _shutdown_called = False

    def _shutdown(signum=None, frame=None):
        nonlocal _shutdown_called
        if _shutdown_called:
            return
        _shutdown_called = True
        if signum:
            log.info(f"Received signal {signum} — shutting down")
        runner.stop()
        try:
            marker = Path.home() / ".operator" / ".current_meeting"
            if marker.exists():
                marker.unlink()
        except OSError:
            pass
        connector.leave()
        _kill_orphaned_children()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        log.info(f"Starting Operator slip mode — attached to {url}")
        runner.run(url)
    except KeyboardInterrupt:
        log.info("Interrupted — detaching")
    finally:
        _shutdown()
        ui.ok("Detached — slip Chrome stays open so the meeting can continue. Goodbye.")
    return 0


def _run_bot(name, rest):
    url = None
    force = False
    no_preflight = False
    for arg in rest:
        if arg == "--force":
            force = True
        elif arg == "--no-preflight":
            # 15.7.4.5 escape hatch — skips the readiness check for
            # CI/scripted launches that can't answer interactive prompts.
            no_preflight = True
        elif arg.startswith("-"):
            print(f"Unknown flag: {arg}")
            return 2
        elif url is None:
            url = arg
        else:
            print(f"Unexpected argument: {arg}")
            return 2

    import time as _time_init
    import logging as _logging_init

    # MUST be set before anything in this function imports `config` —
    # transitively or directly. Pipeline modules (setup, claude_code_import,
    # readiness, …) read OPERATOR_BOT at config-import time; if any of them
    # ever grow a top-level `from _1_800_operator import config` and the env
    # var isn't set yet, config.py exits 2 with "OPERATOR_BOT env var is not
    # set". Setting it as the very first line of `_run_bot` makes the
    # contract enforced by code position, not by comment discipline.
    os.environ["OPERATOR_BOT"] = name

    # Claude agent — Phase 15.9 hard-fail gate. The `claude` bundled agent's
    # entire identity is "inherit your Claude Code setup." If Claude Code
    # isn't installed or the user isn't logged in, there's nothing for the
    # agent to be. Fail loudly before any config loads or browser spins up.
    # Other agents bypass this check entirely.
    if name == "claude":
        from _1_800_operator.pipeline.claude_code_import import (
            claude_code_installed_and_logged_in,
        )
        ok, reason = claude_code_installed_and_logged_in()
        if not ok:
            print(
                f"\nThe `claude` agent requires the Claude Code CLI.\n"
                f"  {reason}\n"
                f"\nInstall Claude Code (https://claude.ai/code) and run "
                f"`claude login`, then re-run `operator dial claude`.\n",
                file=sys.stderr,
            )
            return 2
        # Sync Claude Code MCP servers into the agent config on every boot —
        # picks up servers added/removed/edited in `~/.claude.json` since
        # the last run. Shares one cached `claude mcp list` shell-out with
        # downstream config.py runtime view (per-process cache in
        # `claude_code_import._claude_mcp_list_cached`).
        _t_sync = _time_init.monotonic()
        _sync_claude_imports()
        _logging_init.getLogger("operator").info(
            f"TIMING claude_sync={_time_init.monotonic() - _t_sync:.2f}s"
        )

    # Codex agent — same hard-fail posture as claude. The codex bundled
    # agent's identity is "OpenAI Codex CLI as the meeting brain." If
    # codex isn't installed or the user isn't logged in via ChatGPT
    # subscription, there's nothing for the agent to be. The check also
    # rejects API-key auth (subscription-only by design — defense layer 2
    # of the billing guard; layer 1 is OPENAI_API_KEY="" in the agent's
    # mcp_servers.codex.env block).
    if name == "codex":
        from _1_800_operator.pipeline.codex_import import (
            codex_installed_and_logged_in,
        )
        ok, reason = codex_installed_and_logged_in()
        if not ok:
            print(
                f"\nThe `codex` agent requires the OpenAI Codex CLI.\n"
                f"  {reason}\n"
                f"\nInstall Codex (`npm install -g @openai/codex`) and run "
                f"`codex login`, then re-run `operator dial codex`.\n",
                file=sys.stderr,
            )
            return 2

    # 15.7.4.5 runtime pre-flight — catches hand-edit-config cases the
    # wizard status screen doesn't. All-ok state is silent (zero visible
    # cost on the happy path). Non-zero exit means the user opted out
    # mid-prompt; skip browser spin-up entirely.
    if not no_preflight:
        _t_cfg = _time_init.monotonic()
        from _1_800_operator import config
        _logging_init.getLogger("operator").info(
            f"TIMING config_import={_time_init.monotonic() - _t_cfg:.2f}s"
        )
        from _1_800_operator.pipeline.readiness import (
            PREFLIGHT_OK,
            preflight_mcp_readiness,
        )
        # Track A skips: claude owns its MCPs, our toggle-only stubs would fail
        # the readiness check on the missing `command` key.
        if config.LLM_PROVIDER != "claude_cli":
            rc = preflight_mcp_readiness(config.MCP_SERVERS)
            if rc != PREFLIGHT_OK:
                return rc

    if sys.platform == "darwin":
        return _run_macos(url, force=force) or 0
    return _run_linux(url, force=force) or 0


def _run_macos(meeting_url=None, force=False):
    """Run on macOS — direct URL or meet.new auto-launch."""
    from _1_800_operator.pipeline.chrome_preflight import (
        require_chrome_or_exit,
        require_signed_in_or_exit,
    )
    require_chrome_or_exit()
    require_signed_in_or_exit()

    import logging
    import signal
    import threading as _threading
    import time as _time

    logging.basicConfig(
        filename="/tmp/operator.log",
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    # Stderr stays reserved for the user-facing narrative (pipeline.ui).
    # Detailed diagnostics live in /tmp/operator.log only.
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)

    log = logging.getLogger("operator")

    from _1_800_operator import config
    from _1_800_operator.connectors.macos_adapter import MacOSAdapter
    from _1_800_operator.pipeline import ui
    from _1_800_operator.pipeline.chat_runner import ChatRunner
    from _1_800_operator.pipeline.llm import LLMClient
    from _1_800_operator.pipeline.providers import build_provider

    t_start = _time.monotonic()

    # Skills load up-front so inject_skills lands before MCP hints/status in
    # the system prompt, and so the banner can show skill count before MCP
    # connects. Banner prints immediately after, as the boot splash.
    from _1_800_operator.pipeline.skills import load_skills
    skills = load_skills(
        config.SKILLS_ENABLED,
        external_paths=config.SKILLS_EXTERNAL_PATHS,
        shared_library_dir=config.SKILLS_SHARED_LIBRARY,
    )
    _print_startup_banner(skills)
    ui.say("Launching Chrome…")

    connector = MacOSAdapter(force=force)
    llm = LLMClient(build_provider())
    llm.inject_skills(skills, config.SKILLS_PROGRESSIVE_DISCLOSURE)

    # Captions → MeetingRecord wiring. The JS bridge (window.__onCaption) is
    # exposed by MacOSAdapter at browser startup whenever config.CAPTIONS_ENABLED
    # is true, so set_caption_callback is safe to call before OR after
    # connector.join(). meet.new mode late-binds after the URL resolves.
    def _wire_meeting_record(url):
        if not config.CAPTIONS_ENABLED:
            return None, None
        from _1_800_operator.pipeline.meeting_record import MeetingRecord, slug_from_url
        from _1_800_operator.pipeline.transcript import TranscriptFinalizer
        slug = slug_from_url(url)
        record = MeetingRecord(slug=slug, meta={"meet_url": url})
        llm.set_record(record)
        # Write the active meeting path to a marker file so MCP servers
        # registered via fully-static config (e.g. codex) can locate the
        # current meeting without per-spawn env-var interpolation. Cleanup
        # happens in _shutdown.
        try:
            marker = Path.home() / ".operator" / ".current_meeting"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(str(record.path), encoding="utf-8")
        except OSError as e:
            log.warning(f"could not write current-meeting marker: {e}")
        finalizer = TranscriptFinalizer(record, silence_seconds=config.CAPTION_SILENCE_SECONDS)
        connector.set_caption_callback(finalizer.on_caption_update)
        log.info("captions enabled — transcript will be appended to meeting record")
        return record, finalizer

    meeting_record = None
    transcript_finalizer = None
    if meeting_url:
        meeting_record, transcript_finalizer = _wire_meeting_record(meeting_url)

    # Start MCP connection in background while browser joins
    _mcp_result = {"client": None}
    def _connect_mcp():
        t_mcp = _time.monotonic()
        from _1_800_operator.pipeline.mcp_client import MCPClient
        client = MCPClient()
        try:
            tool_names = client.connect_all()
            log.info(f"TIMING mcp_connect={_time.monotonic() - t_mcp:.1f}s ({len(tool_names)} tools)")
            _mcp_result["client"] = client
        except Exception as e:
            log.error(f"MCP client startup failed: {e}")

    # Track A: claude owns its own MCPs — our blocks are toggle-only stubs.
    if config.MCP_SERVERS and config.LLM_PROVIDER != "claude_cli":
        mcp_thread = _threading.Thread(target=_connect_mcp, daemon=True)
        mcp_thread.start()
    else:
        mcp_thread = None

    connector.join(meeting_url)

    # meet.new mode: wait for the browser to redirect and publish the real URL.
    if meeting_url is None:
        meeting_url = connector.wait_for_resolved_url(timeout=45)
        if not meeting_url:
            log.error("meet.new did not produce a meeting URL — exiting")
            ui.err("meet.new did not produce a meeting URL")
            connector.leave()
            _kill_orphaned_children()
            return 1
        log.info(f"meet.new resolved to {meeting_url}")
        ui.ok(f"Fresh meeting: {meeting_url}")
        # The bot joins in a headless Chrome — pop the Meet open in the
        # user's default browser so they can see and chat with the bot.
        try:
            webbrowser.open(meeting_url)
        except Exception as e:
            log.warning(f"could not auto-open meeting URL in browser: {e}")
        meeting_record, transcript_finalizer = _wire_meeting_record(meeting_url)

    mcp = None
    if mcp_thread:
        mcp_thread.join()
        mcp = _mcp_result["client"]
        if mcp:
            llm.inject_mcp_hints(config.MCP_SERVERS)
            loaded = [n for n in config.MCP_SERVERS if n not in mcp.startup_failures]
            llm.inject_mcp_status(loaded, mcp.startup_failures)
            gh_login = mcp.resolve_github_user()
            if gh_login:
                llm.inject_github_user(gh_login)
        _emit_mcp_rollup(mcp, connector=connector)

    log.info(f"TIMING setup={_time.monotonic() - t_start:.1f}s")
    runner = ChatRunner(
        connector,
        llm,
        mcp_client=mcp,
        meeting_record=meeting_record,
        skills=skills,
        skills_progressive=config.SKILLS_PROGRESSIVE_DISCLOSURE,
    )

    _shutdown_called = False

    def _shutdown(signum=None, frame=None):
        nonlocal _shutdown_called
        if _shutdown_called:
            return
        _shutdown_called = True
        reason_file = os.path.join(config.BROWSER_PROFILE_DIR, ".operator.kill_reason")
        try:
            with open(reason_file) as _f:
                reason = _f.read().strip()
            os.remove(reason_file)
            ui.err(reason, hint_log=False)
            log.info(reason)
        except FileNotFoundError:
            if signum:
                log.info(f"Received signal {signum} — shutting down")
        runner.stop()
        if transcript_finalizer:
            transcript_finalizer.stop()
        try:
            marker = Path.home() / ".operator" / ".current_meeting"
            if marker.exists():
                marker.unlink()
        except OSError:
            pass
        if mcp:
            mcp.shutdown()
        connector.leave()
        _kill_orphaned_children()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        log.info(f"Starting Operator — joining {meeting_url}")
        runner.run(meeting_url)
        if not runner._stop_event.is_set():
            ui.say(f"Restart with: operator dial {os.environ.get('OPERATOR_BOT', '<name>')} {meeting_url}")
    except KeyboardInterrupt:
        log.info("Interrupted — leaving meeting")
    finally:
        _shutdown()
        ui.ok("Left meeting — goodbye.")
    return 0


def _run_linux(meeting_url, force=False):
    """Run on Linux — requires a meeting URL and a live DISPLAY."""
    import logging
    import signal

    logging.basicConfig(
        filename="/tmp/operator.log",
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)
    log = logging.getLogger("operator")

    if not meeting_url:
        meeting_url = os.environ.get("MEETING_URL")
    if not meeting_url:
        bot = os.environ.get("OPERATOR_BOT", "<name>")
        print("A meeting URL is required on Linux:", file=sys.stderr)
        print(f"   operator dial {bot} <meet-url>", file=sys.stderr)
        print(f"   MEETING_URL=<url> operator dial {bot}", file=sys.stderr)
        sys.exit(1)

    display = os.environ.get("DISPLAY")
    if not display:
        log.error("DISPLAY is not set")
        print("DISPLAY is not set — start Xvfb first:", file=sys.stderr)
        print("   Xvfb :99 -screen 0 1920x1080x24 &", file=sys.stderr)
        print("   export DISPLAY=:99", file=sys.stderr)
        sys.exit(1)
    log.info(f"DISPLAY={display}")

    from _1_800_operator.connectors.linux_adapter import LinuxAdapter
    from _1_800_operator.pipeline import ui
    from _1_800_operator.pipeline.chat_runner import ChatRunner
    from _1_800_operator.pipeline.llm import LLMClient
    from _1_800_operator.pipeline.providers import build_provider
    from _1_800_operator import config

    from _1_800_operator.pipeline.skills import load_skills
    skills = load_skills(
        config.SKILLS_ENABLED,
        external_paths=config.SKILLS_EXTERNAL_PATHS,
        shared_library_dir=config.SKILLS_SHARED_LIBRARY,
    )
    _print_startup_banner(skills)
    ui.say("Launching Chromium…")

    log.info(f"Starting Operator (Linux) — joining {meeting_url}")
    connector = LinuxAdapter()
    llm = LLMClient(build_provider())
    llm.inject_skills(skills, config.SKILLS_PROGRESSIVE_DISCLOSURE)

    mcp = None
    # Track A: claude owns its own MCPs — our blocks are toggle-only stubs.
    if config.MCP_SERVERS and config.LLM_PROVIDER != "claude_cli":
        from _1_800_operator.pipeline.mcp_client import MCPClient
        mcp = MCPClient()
        try:
            tool_names = mcp.connect_all()
            log.info(f"MCP tools discovered: {tool_names}")
            llm.inject_mcp_hints(config.MCP_SERVERS)
            loaded = [n for n in config.MCP_SERVERS if n not in mcp.startup_failures]
            llm.inject_mcp_status(loaded, mcp.startup_failures)
            gh_login = mcp.resolve_github_user()
            if gh_login:
                llm.inject_github_user(gh_login)
        except Exception as e:
            log.error(f"MCP client startup failed: {e}")
            ui.err("MCP startup failed")
            mcp = None
        _emit_mcp_rollup(mcp, connector=connector)

    runner = ChatRunner(
        connector,
        llm,
        mcp_client=mcp,
        skills=skills,
        skills_progressive=config.SKILLS_PROGRESSIVE_DISCLOSURE,
    )

    _shutdown_called = False

    def _shutdown(signum=None, frame=None):
        nonlocal _shutdown_called
        if _shutdown_called:
            return
        _shutdown_called = True
        if signum:
            log.info(f"Received signal {signum} — shutting down")
        runner.stop()
        if mcp:
            mcp.shutdown()
        connector.leave()
        _kill_orphaned_children()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        runner.run(meeting_url)
        if not runner._stop_event.is_set():
            ui.say(f"Restart with: operator dial {os.environ.get('OPERATOR_BOT', '<name>')} {meeting_url}")
    except KeyboardInterrupt:
        log.info("Interrupted — leaving meeting")
    finally:
        _shutdown()
        ui.ok("Left meeting — goodbye.")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
