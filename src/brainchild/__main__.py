"""
Brainchild — AI Meeting Participant
Cross-platform entry point. Auto-detects OS and dispatches to the right adapter.

Usage:
    brainchild run <name> <url> Run named agent in a specific Meet
    brainchild run <name>       Auto-open a new Meet, join as that bot
    brainchild try <name>       Terminal test-drive (no Meet)
    brainchild build            Create a new agent (wizard)
    brainchild edit <target>    Open an agent config (or .env) in $EDITOR
    brainchild where <target>   Print the absolute path of a config file
    brainchild                  Print usage + agent list
"""
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

_AGENTS_DIR = Path.home() / ".brainchild" / "agents"
_BUNDLED_AGENTS_DIR = Path(__file__).resolve().parent / "agents"
_SKILLS_DIR = Path.home() / ".brainchild" / "skills"
_BUNDLED_SKILLS_DIR = Path(__file__).resolve().parent / "skills"


# MCP-server fields whose source of truth is the user's Claude Code config —
# always overwritten from `~/.claude.json` / `claude mcp list` on every boot
# so updates flow through (e.g., user re-points a stdio server at a new
# binary path; we pick that up automatically).
_CLAUDE_MCP_SOURCE_DRIVEN_FIELDS = (
    "command", "args", "env", "auth", "auth_url", "description",
)
# Fields the user owns via `brainchild edit claude` — preserved across
# re-imports so meeting-scope tweaks aren't clobbered every run.
_CLAUDE_MCP_USER_PRESERVED_FIELDS = (
    "enabled", "hints", "read_tools", "confirm_tools",
)


def _sync_claude_imports() -> None:
    """Sync the `claude` agent's MCP servers with the user's Claude Code config.

    Runs on every boot of the claude agent. Re-reads ~/.claude.json plus
    `claude mcp list`, then for each discovered MCP merges with the
    existing entry in ~/.brainchild/agents/claude/config.yaml:

      - command, args, env, auth, auth_url, description → overwritten
        from source so changes in Claude Code flow through.
      - enabled, hints, read_tools, confirm_tools → preserved from the
        user's existing entry so meeting-scope edits stick.

    Servers present in the user config but missing from the discovered
    set are dropped — if the user removed an MCP from Claude Code, they
    want it gone from operator too.

    The `~3s` cost of `claude mcp list` is paid every boot. If that
    becomes a real latency concern, add a hash-check fast-path keyed on
    a digest of ~/.claude.json#mcpServers — but defer until measured.

    Comments in config.yaml are lost on rewrite (PyYAML round-trip);
    we only write back if the merged config differs from what's on disk,
    so a no-op sync doesn't trash formatting unnecessarily.
    """
    cfg_path = _AGENTS_DIR / "claude" / "config.yaml"
    if not cfg_path.is_file():
        return

    # Load via ruamel round-trip so comments and block-scalar styles in
    # the user's config survive the mutation+dump cycle. PyYAML safe_load
    # would return a plain dict and the next dump would strip everything
    # cosmetic.
    from brainchild.pipeline.setup import _load_yaml, _dump_yaml
    try:
        cfg = _load_yaml(cfg_path)
    except Exception:
        return

    from brainchild.pipeline.claude_code_import import (
        append_env_placeholders,
        discover_all_mcps,
    )

    mcps, wrapped = discover_all_mcps()
    existing = cfg.get("mcp_servers") or {}
    if not isinstance(existing, dict):
        existing = {}

    new_servers: dict = {}
    added: list[str] = []
    updated: list[str] = []
    removed: list[str] = []
    env_refs: set[str] = set()

    discovered_names = {m.name for m in mcps}
    for m in mcps:
        prev = existing.get(m.name)
        if isinstance(prev, dict):
            merged = dict(prev)
            for f in _CLAUDE_MCP_SOURCE_DRIVEN_FIELDS:
                if f in m.block:
                    merged[f] = m.block[f]
            for f in _CLAUDE_MCP_USER_PRESERVED_FIELDS:
                if f in prev:
                    merged[f] = prev[f]
                elif f in m.block:
                    merged[f] = m.block[f]
            new_servers[m.name] = merged
            if any(merged.get(f) != prev.get(f) for f in _CLAUDE_MCP_SOURCE_DRIVEN_FIELDS):
                updated.append(m.name)
        else:
            new_servers[m.name] = m.block
            added.append(m.name)
        env_refs.update(m.env_vars_referenced)

    for name in existing:
        if name not in discovered_names:
            removed.append(name)

    env_file = Path.home() / ".brainchild" / ".env"
    placeheld: list[str] = []
    if env_refs:
        placeheld = append_env_placeholders(sorted(env_refs), env_file)

    # Drop the legacy first-run-done flag if it's still hanging around —
    # the gate it controlled is gone now.
    cfg.pop("_claude_import_done", None)
    cfg["mcp_servers"] = new_servers

    # Only rewrite the file when the merge actually changed something on
    # disk. Keeps formatting churn off no-op boots.
    on_disk_cfg = _load_yaml(cfg_path)
    on_disk_servers = on_disk_cfg.get("mcp_servers") or {}
    if dict(new_servers) != dict(on_disk_servers) or "_claude_import_done" in on_disk_cfg:
        try:
            _dump_yaml(cfg, cfg_path)
        except OSError as e:
            print(f"[claude] WARN: could not write {cfg_path}: {e}", file=sys.stderr)
            return

    parts = []
    if added:
        parts.append(f"+{len(added)} ({', '.join(added)})")
    if updated:
        parts.append(f"~{len(updated)} ({', '.join(updated)})")
    if removed:
        parts.append(f"-{len(removed)} ({', '.join(removed)})")
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


def _ensure_user_agents():
    """Sync-on-every-run: copy any bundled bot that is missing from the
    user's ~/.brainchild/agents/ dir. Existing user bots are never touched
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
    from the dev-mode repo root (pre-Phase-14.5) into `~/.brainchild/`.

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
    home_dir = Path.home() / ".brainchild"
    home_dir.mkdir(parents=True, exist_ok=True)

    # Dev-mode repo root — src/brainchild/__main__.py → up 3 levels.
    repo_root = Path(__file__).resolve().parent.parent.parent

    for name in ("browser_profile", "auth_state.json", ".env"):
        src = repo_root / name
        dst = home_dir / name
        if dst.exists() or not src.exists():
            continue
        try:
            shutil.move(str(src), str(dst))
            print(f"[brainchild] migrated {name} → {dst}", file=sys.stderr)
        except OSError as e:
            print(
                f"[brainchild] WARN: could not migrate {src} → {dst}: {e}",
                file=sys.stderr,
            )


def _ensure_user_skills():
    """Sync-on-every-run: copy any bundled skill that is missing from the
    user's ~/.brainchild/skills/ dir. Existing user skills are never touched
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
    log = logging.getLogger("brainchild")

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
    "oauth_needed":    "run `brainchild auth {name}`",
    "unknown":         "error",
}


def _emit_mcp_rollup(mcp, connector=None):
    """Print one ▸ line summarizing per-server MCP connect status.

    Silent when there are no MCP servers configured or the MCPClient
    didn't run (Track A: claude_cli owns its own MCPs). On mixed
    success, failed servers get a terse remediation hint pulled from
    `_MCP_KIND_HINT` so the user knows the exact next command without
    digging through /tmp/brainchild.log.

    If `connector` is provided AND any servers failed, also drop a
    single chat-side one-liner so meeting participants see the
    degraded state without having to inspect the host terminal.
    Successful-only loads stay terminal-only — chat doesn't need to
    say "everything's fine."
    """
    from brainchild import config
    from brainchild.pipeline import ui
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
            f"won't work this meeting — see host terminal or `tail /tmp/brainchild.log` for details."
        )
        try:
            connector.send_chat(chat_msg)
        except Exception as e:
            import logging as _logging
            _logging.getLogger("brainchild").warning(
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
    from brainchild import config
    import sys as _sys
    import shutil as _shutil
    from brainchild.pipeline import face
    from rich.cells import cell_len

    bot_name = os.environ.get("BRAINCHILD_BOT", "")
    portrait_path = _AGENTS_DIR / bot_name / "portrait.txt"

    # First-run hook — contributor-added bot with no portrait gets one minted.
    if bot_name and not portrait_path.exists():
        if face.write_if_missing(bot_name, portrait_path):
            import logging
            logging.getLogger("brainchild").info(
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
    print("  brainchild run <name> [url] Run an agent in a Meet (auto-opens one if no url)")
    print("  brainchild try <name>       Terminal test-drive (no Meet)")
    print("  brainchild build            Create a new agent (wizard)")
    print("  brainchild auth <mcp>       Authorize an OAuth MCP (Linear, etc.)")
    print("  brainchild edit <target>    Open an agent config (or .env) in $EDITOR")
    print("  brainchild where <target>   Print the absolute path of a config file")
    print()
    print("Flags:")
    print("  --force                   Retry join even if a session is flagged stuck")
    print("  --no-preflight            Skip the MCP readiness check (for CI/scripted launches)")
    print()
    bots = _available_bots()
    if bots:
        print("Available bots:")
        for b in bots:
            tag = _bot_tagline(b)
            print(f"  {b:<12} {tag}")


def _resolve_config_target(target):
    """Map a user-supplied target to an on-disk path under ~/.brainchild/.

    Accepts a bot name (`claude`, `pm`, …) or the special token `.env` / `env`.
    Returns (path, error_message); exactly one is non-None.
    """
    home = Path.home() / ".brainchild"
    if target in (".env", "env"):
        return home / ".env", None
    if target in _available_bots():
        return _AGENTS_DIR / target / "config.yaml", None
    return None, f"Unknown target: {target!r}. Expected a bot name or `.env`."


def _run_edit(argv):
    if not argv:
        print("Usage: brainchild edit <bot-name | .env>\n")
        _print_usage()
        return 2
    path, err = _resolve_config_target(argv[0])
    if err:
        print(err)
        _print_usage()
        return 2
    if not path.exists():
        print(f"Config file does not exist: {path}")
        return 1
    import shlex
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    cmd = shlex.split(editor) + [str(path)]
    subprocess.call(cmd)
    # Editors (vim especially) sometimes exit nonzero for benign reasons —
    # swapfile noise, terminal weirdness — even when the user saved cleanly.
    # Don't propagate that to the shell prompt; the only thing that matters
    # is whether the config still exists. Print a positive signal so the
    # user has no ambiguity about whether their edit landed.
    if path.exists():
        print(f"Saved {path}")
        return 0
    print(f"File no longer exists: {path}")
    return 1


def _run_where(argv):
    if not argv:
        print("Usage: brainchild where <bot-name | .env>\n")
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
    from brainchild.pipeline.chrome_preflight import require_chrome_or_exit
    require_chrome_or_exit()
    from brainchild.pipeline.setup import run as _wizard_run
    return _wizard_run([])


# _run_auth and _find_oauth_mcp_config moved to brainchild.pipeline.auth in
# Phase 15.7.4 so the wizard's inline "authorize now?" prompt and this CLI
# dispatch hit the same code path. Re-exported here for backward compat.
from brainchild.pipeline.auth import (
    find_oauth_mcp_config as _find_oauth_mcp_config,
    run_auth as _run_auth,
)


def main():
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
            print("Usage: brainchild try <name>\n")
            _print_usage()
            return 2
        return _run_try(argv[1])
    if first == "auth":
        if len(argv) != 2:
            print("Usage: brainchild auth <mcp>\n")
            _print_usage()
            return 2
        return _run_auth(argv[1])
    if first == "edit":
        return _run_edit(argv[1:])
    if first == "where":
        return _run_where(argv[1:])
    if first == "run":
        if len(argv) < 2:
            print("Usage: brainchild run <name> [url]\n")
            _print_usage()
            return 2
        name = argv[1]
        if name not in _available_bots():
            print(f"Unknown bot: {name!r}\n")
            _print_usage()
            return 2
        return _run_bot(name, argv[2:])

    if first.startswith("-"):
        print(f"Unknown option: {first}\n")
        _print_usage()
        return 2

    # Phase 15.8: bare `brainchild <name>` is no longer accepted. A known bot
    # name gets a pointed hint pointing at the new form; anything else falls
    # through to the generic unknown-subcommand message.
    if first in _available_bots():
        print(
            f"Run agents via `brainchild run {first}`. "
            f"Bare `brainchild {first}` is no longer supported.\n"
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

    # Must land before any `from brainchild import config`.
    os.environ["BRAINCHILD_BOT"] = name

    import logging
    import signal
    import time as _time

    logging.basicConfig(
        filename="/tmp/brainchild.log",
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    # Keep stderr clean — terminal UX is the chat itself. Logs stay in /tmp/brainchild.log.
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)

    log = logging.getLogger("brainchild")

    from brainchild import config
    from brainchild.connectors.terminal import TerminalConnector
    from brainchild.pipeline import ui
    from brainchild.pipeline.chat_runner import ChatRunner
    from brainchild.pipeline.llm import LLMClient
    from brainchild.pipeline.meeting_record import MeetingRecord
    from brainchild.pipeline.providers import build_provider
    from brainchild.pipeline.skills import load_skills

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
    # via ~/.claude.json — brainchild's own MCPClient must not try to connect to
    # them (the config blocks are toggle-only stubs without `command`/`args`).
    if config.MCP_SERVERS and config.LLM_PROVIDER != "claude_cli":
        from brainchild.pipeline.mcp_client import MCPClient
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

    # Claude agent — Phase 15.9 hard-fail gate. The `claude` bundled agent's
    # entire identity is "inherit your Claude Code setup." If Claude Code
    # isn't installed or the user isn't logged in, there's nothing for the
    # agent to be. Fail loudly before any config loads or browser spins up.
    # Other agents bypass this check entirely.
    if name == "claude":
        from brainchild.pipeline.claude_code_import import (
            claude_code_installed_and_logged_in,
        )
        ok, reason = claude_code_installed_and_logged_in()
        if not ok:
            print(
                f"\nThe `claude` agent requires the Claude Code CLI.\n"
                f"  {reason}\n"
                f"\nInstall Claude Code (https://claude.ai/code) and run "
                f"`claude login`, then re-run `brainchild run claude`.\n",
                file=sys.stderr,
            )
            return 2
        # Sync Claude Code MCP servers into the agent config on every boot —
        # picks up servers added/removed/edited in `~/.claude.json` since
        # the last run. Pays the ~3s `claude mcp list` shell-out cost
        # every boot in exchange for staying in sync.
        _sync_claude_imports()

    # MUST be set before any `from brainchild import config` fires in the pipeline modules.
    os.environ["BRAINCHILD_BOT"] = name

    # 15.7.4.5 runtime pre-flight — catches hand-edit-config cases the
    # wizard status screen doesn't. All-ok state is silent (zero visible
    # cost on the happy path). Non-zero exit means the user opted out
    # mid-prompt; skip browser spin-up entirely.
    if not no_preflight:
        from brainchild import config
        from brainchild.pipeline.readiness import (
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
        _run_macos(url, force=force)
    else:
        _run_linux(url, force=force)
    return 0


def _run_macos(meeting_url=None, force=False):
    """Run on macOS — direct URL or meet.new auto-launch."""
    from brainchild.pipeline.chrome_preflight import require_chrome_or_exit
    require_chrome_or_exit()

    import logging
    import signal
    import threading as _threading
    import time as _time

    logging.basicConfig(
        filename="/tmp/brainchild.log",
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    # Stderr stays reserved for the user-facing narrative (pipeline.ui).
    # Detailed diagnostics live in /tmp/brainchild.log only.
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)

    log = logging.getLogger("brainchild")

    from brainchild import config
    from brainchild.connectors.macos_adapter import MacOSAdapter
    from brainchild.pipeline import ui
    from brainchild.pipeline.chat_runner import ChatRunner
    from brainchild.pipeline.llm import LLMClient
    from brainchild.pipeline.providers import build_provider

    t_start = _time.monotonic()

    # Skills load up-front so inject_skills lands before MCP hints/status in
    # the system prompt, and so the banner can show skill count before MCP
    # connects. Banner prints immediately after, as the boot splash.
    from brainchild.pipeline.skills import load_skills
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
        from brainchild.pipeline.meeting_record import MeetingRecord, slug_from_url
        from brainchild.pipeline.transcript import TranscriptFinalizer
        slug = slug_from_url(url)
        record = MeetingRecord(slug=slug, meta={"meet_url": url})
        llm.set_record(record)
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
        from brainchild.pipeline.mcp_client import MCPClient
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
            return
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
        reason_file = os.path.join(config.BROWSER_PROFILE_DIR, ".brainchild.kill_reason")
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
        if mcp:
            mcp.shutdown()
        connector.leave()
        _kill_orphaned_children()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        log.info(f"Starting Brainchild — joining {meeting_url}")
        runner.run(meeting_url)
        if not runner._stop_event.is_set():
            ui.say(f"Restart with: brainchild run {os.environ.get('BRAINCHILD_BOT', '<name>')} {meeting_url}")
    except KeyboardInterrupt:
        log.info("Interrupted — leaving meeting")
    finally:
        _shutdown()
        ui.ok("Left meeting — goodbye.")


def _run_linux(meeting_url, force=False):
    """Run on Linux — requires a meeting URL and a live DISPLAY."""
    import logging
    import signal

    logging.basicConfig(
        filename="/tmp/brainchild.log",
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)
    log = logging.getLogger("brainchild")

    if not meeting_url:
        meeting_url = os.environ.get("MEETING_URL")
    if not meeting_url:
        bot = os.environ.get("BRAINCHILD_BOT", "<name>")
        print("A meeting URL is required on Linux:", file=sys.stderr)
        print(f"   brainchild run {bot} <meet-url>", file=sys.stderr)
        print(f"   MEETING_URL=<url> brainchild run {bot}", file=sys.stderr)
        sys.exit(1)

    display = os.environ.get("DISPLAY")
    if not display:
        log.error("DISPLAY is not set")
        print("DISPLAY is not set — start Xvfb first:", file=sys.stderr)
        print("   Xvfb :99 -screen 0 1920x1080x24 &", file=sys.stderr)
        print("   export DISPLAY=:99", file=sys.stderr)
        sys.exit(1)
    log.info(f"DISPLAY={display}")

    from brainchild.connectors.linux_adapter import LinuxAdapter
    from brainchild.pipeline import ui
    from brainchild.pipeline.chat_runner import ChatRunner
    from brainchild.pipeline.llm import LLMClient
    from brainchild.pipeline.providers import build_provider
    from brainchild import config

    from brainchild.pipeline.skills import load_skills
    skills = load_skills(
        config.SKILLS_ENABLED,
        external_paths=config.SKILLS_EXTERNAL_PATHS,
        shared_library_dir=config.SKILLS_SHARED_LIBRARY,
    )
    _print_startup_banner(skills)
    ui.say("Launching Chromium…")

    log.info(f"Starting Brainchild (Linux) — joining {meeting_url}")
    connector = LinuxAdapter()
    llm = LLMClient(build_provider())
    llm.inject_skills(skills, config.SKILLS_PROGRESSIVE_DISCLOSURE)

    mcp = None
    # Track A: claude owns its own MCPs — our blocks are toggle-only stubs.
    if config.MCP_SERVERS and config.LLM_PROVIDER != "claude_cli":
        from brainchild.pipeline.mcp_client import MCPClient
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
            ui.say(f"Restart with: brainchild run {os.environ.get('BRAINCHILD_BOT', '<name>')} {meeting_url}")
    except KeyboardInterrupt:
        log.info("Interrupted — leaving meeting")
    finally:
        _shutdown()
        ui.ok("Left meeting — goodbye.")


if __name__ == "__main__":
    sys.exit(main() or 0)
