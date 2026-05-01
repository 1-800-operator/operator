"""MCP readiness check — Phase 15.7.4.

One helper that combines env-var presence (15.7.1's `missing_vars`),
OAuth token-cache presence (15.7.3's `oauth_cache_exists`), and
claude-code-specific prereqs (git binary + claude CLI + `claude auth
status`) into a per-server status dict. The wizard's status screen and
the runtime pre-flight (15.7.4.5) both consume this.

Shape of the returned record per server:

    {
        "status":        "ok" | "missing_env" | "oauth_needed" | "prereq_missing",
        "fix":           str,                 # one-line user-facing hint
        "fix_url":       str | None,          # remediation URL, if applicable
        "missing_vars":  list[str],           # only for missing_env
        "auth_url":      str | None,          # only for oauth_needed
    }

The helper takes `mcp_servers: dict[str, dict]` — the raw-YAML shape
(wizard hands it `state.bot_cfg["mcp_servers"]`; runtime hands it
`config.MCP_SERVERS`). It reads `enabled`, `auth`, `auth_url`, `env`,
and `credentials_url` — all optional except `env` (missing = no env
vars needed). It does *not* mutate the input.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from dotenv import load_dotenv

from _1_800_operator.pipeline.oauth_cache import oauth_cache_exists

# Ensure ~/.operator/.env is in os.environ before we inspect it.
# Idempotent and override=False, so runtime pre-flight (where config.py
# already called this) is a no-op, and wizard calls (which import
# readiness before any config import) see the same secrets the runtime
# would. Path must match config.ENV_FILE — duplicated here to avoid
# importing config (which requires OPERATOR_BOT to be set).
load_dotenv(Path.home() / ".operator" / ".env", override=False)

_ENV_REF_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")

# How long to wait on `claude auth status --json` before giving up. The
# subcommand is local-only (no network) and normally returns in <300 ms;
# 5 s is generous slack for a cold `node` warm-up on slow disks.
_CLAUDE_AUTH_TIMEOUT = 5.0


def _missing_env_vars(env_dict: dict | None) -> list[str]:
    """Return env var names referenced as ${VAR} in `env_dict` that are
    absent or empty in os.environ.

    Mirrors config._resolve_env_vars's missingness logic so the wizard
    gets the same answer without having to run the full config loader.
    Both the raw-YAML shape (values like "${GITHUB_TOKEN}") and the
    already-resolved shape (plain strings, `missing_vars` populated)
    are handled — if the server block carries a pre-computed
    `missing_vars` list, we trust it.
    """
    if not env_dict:
        return []
    missing: list[str] = []
    seen: set[str] = set()
    for v in env_dict.values():
        if not isinstance(v, str):
            continue
        for var in _ENV_REF_RE.findall(v):
            if var in seen:
                continue
            seen.add(var)
            if not os.environ.get(var):
                missing.append(var)
    return missing


def _probe_claude_code(*, check_auth: bool = True) -> tuple[str, str]:
    """Check claude-code prereqs. Returns (status, detail).

    status:
      "ok"              — git + claude both on PATH, and (if check_auth)
                          `claude auth status --json` reports loggedIn=true.
      "prereq_missing"  — something's missing; detail names what.

    `check_auth=False` is the knob for callers that want to skip the
    ~300 ms subprocess hop during routine wizard use (we keep it on by
    default — stale login is the whole point of 15.7.4's gate).
    """
    if shutil.which("git") is None:
        return "prereq_missing", "git CLI not on PATH — install git first"
    if shutil.which("claude") is None:
        return "prereq_missing", "claude CLI not on PATH — install Claude Code first"
    if not check_auth:
        return "ok", ""
    try:
        r = subprocess.run(
            ["claude", "auth", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=_CLAUDE_AUTH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return "prereq_missing", (
            f"`claude auth status` did not respond within {_CLAUDE_AUTH_TIMEOUT:.0f}s "
            f"— try running it manually and sign in"
        )
    except OSError as e:
        return "prereq_missing", f"could not run `claude auth status`: {e}"
    if r.returncode != 0:
        return "prereq_missing", "`claude auth status` exited non-zero — run `claude auth login`"
    try:
        payload = json.loads(r.stdout)
    except json.JSONDecodeError:
        return "prereq_missing", "`claude auth status` returned unparseable output — run `claude auth login`"
    if not payload.get("loggedIn"):
        return "prereq_missing", "not logged in — run `claude auth login`"
    return "ok", ""


def report_mcp_readiness(
    mcp_servers: dict[str, dict],
    *,
    enabled_only: bool = True,
    check_claude_code_auth: bool = True,
) -> dict[str, dict]:
    """Return per-server readiness records.

    Only inspects servers with `enabled: True` unless enabled_only=False
    — dormant blocks in the full-union scaffold would otherwise spam the
    status screen with noise the user doesn't care about.

    Priority of checks per server:
      1. claude-code → binary + login probe (15.7.4's prereq gate).
      2. auth=="oauth" → OAuth token-cache path exists.
      3. default (auth=="env" or absent) → every ${VAR} in env is set.

    All three can degrade to "prereq_missing" for the claude-code case;
    the OAuth branch returns "oauth_needed"; the env branch returns
    "missing_env" with a `missing_vars` list callers can surface by name.
    """
    out: dict[str, dict] = {}
    for name, srv in mcp_servers.items():
        if enabled_only and not srv.get("enabled", True):
            continue

        credentials_url = srv.get("credentials_url") or None

        if name == "claude-code":
            status, detail = _probe_claude_code(check_auth=check_claude_code_auth)
            if status == "ok":
                out[name] = {"status": "ok", "fix": "", "fix_url": None}
            else:
                out[name] = {
                    "status": "prereq_missing",
                    "fix": detail,
                    "fix_url": credentials_url,
                }
            continue

        if srv.get("auth") == "oauth":
            auth_url = srv.get("auth_url") or ""
            if oauth_cache_exists(auth_url):
                out[name] = {"status": "ok", "fix": "", "fix_url": None}
            else:
                out[name] = {
                    "status": "oauth_needed",
                    "fix": f"run `operator auth {name}` once to authorize",
                    "fix_url": credentials_url,
                    "auth_url": auth_url,
                }
            continue

        # Default: env-authed server. Prefer the pre-resolved missing_vars
        # list (populated by config._resolve_env_vars) when available so
        # wizard and runtime agree on what's missing.
        pre = srv.get("missing_vars")
        if isinstance(pre, list):
            missing = list(pre)
        else:
            missing = _missing_env_vars(srv.get("env"))

        if missing:
            out[name] = {
                "status": "missing_env",
                "fix": f"set {', '.join(missing)} in .env",
                "fix_url": credentials_url,
                "missing_vars": missing,
            }
        else:
            out[name] = {"status": "ok", "fix": "", "fix_url": None}

    return out


# Status → unicode glyph used by the wizard status screen + runtime pre-flight.
# Keeping the mapping here (vs in setup.py) so both callers render identically.
STATUS_GLYPH = {
    "ok": "✓",
    "missing_env": "✗",
    "oauth_needed": "⚠",
    "prereq_missing": "✗",
}


# Runtime pre-flight: exit codes returned to the CLI. 0 = proceed to
# browser spin-up; non-zero = abort before join so the user can fix
# their .env or finish an authorization out-of-band.
PREFLIGHT_OK = 0
PREFLIGHT_USER_ABORT = 2


def preflight_mcp_readiness(
    mcp_servers: dict[str, dict],
    *,
    input_fn=input,
    output_fn=print,
    run_auth_fn=None,
) -> int:
    """Runtime MCP pre-flight — Phase 15.7.4.5.

    Runs inside `_run_bot()` after `OPERATOR_BOT` is set and config is
    loaded, but before the browser spins up. Catches the hand-edit-config
    case where a user enables an OAuth/env MCP outside the wizard — the
    15.7.3 banner handles this mid-meeting, but that's obtrusive.
    Wizard + runtime are belt-and-suspenders.

    All-ok state exits 0 silently (zero latency cost on the happy path —
    no printing, no prompts). Only speaks when something needs attention.

    Per-status behavior when there's a gap:
      oauth_needed  → "Authorize X now? [y/N]" default N. `y` runs
                      `run_auth(name)` inline (foreground mcp-remote
                      spawn + browser popup); on success re-checks the
                      same server and continues if now ok.
      missing_env   → "Continue without X? [Y/n]" default Y. `n` returns
                      PREFLIGHT_USER_ABORT so the shell wrapper exits
                      with a non-zero code — user edits .env and re-runs.
      prereq_missing → "Continue anyway? [Y/n]" default Y. Non-blocking;
                      bundled claude-code binary probe uses this so
                      meetings without claude-code calls don't pay for
                      a stale auth gate.

    input_fn / output_fn / run_auth_fn are injected for testability
    (defaults wire to stdin/stdout + the real OAuth seed flow). Callers
    flipping `--no-preflight` bypass this entirely in `_run_bot`.

    Intentionally uses check_claude_code_auth=False: the 5s `claude
    auth status` subprocess adds noticeable latency to every bot launch
    even on the happy path. The 15.7.2 mid-meeting banner catches the
    claude-code stale-login case with a clear "re-auth" message — the
    wizard's proactive probe (15.7.4) is where we pay for a thorough
    check, not on every runtime boot.
    """
    if run_auth_fn is None:
        from _1_800_operator.pipeline.auth import run_auth as run_auth_fn

    report = report_mcp_readiness(
        mcp_servers,
        enabled_only=True,
        check_claude_code_auth=False,
    )
    problems = {n: r for n, r in report.items() if r["status"] != "ok"}
    if not problems:
        return PREFLIGHT_OK

    output_fn("")
    output_fn("Pre-flight — some MCPs need attention before the meeting:")
    for name, rec in problems.items():
        glyph = STATUS_GLYPH[rec["status"]]
        line = f"  {glyph} {name} — {rec['fix']}"
        if rec.get("fix_url"):
            line += f" ({rec['fix_url']})"
        output_fn(line)
    output_fn("")

    for name, rec in problems.items():
        status = rec["status"]
        if status == "oauth_needed":
            # Default N (skip) — authorize-now is the non-default opt-in
            # because it takes over the terminal with mcp-remote's output
            # and opens a browser. User who prefers to auth later can hit
            # Enter and the bot still boots (linear will be runtime_disabled
            # but the rest of the meeting works).
            answer = _ask(
                input_fn,
                output_fn,
                f"{name}: authorize now? (browser popup; runs "
                f"`operator auth {name}`) [y/N] ",
                default="n",
            )
            if answer.lower() == "y":
                rc = run_auth_fn(name)
                if rc == 0:
                    output_fn(f"  ✓ {name} authorized — continuing.")
                else:
                    output_fn(
                        f"  ⚠ {name} not authorized (exit {rc}) — "
                        f"continuing without it. Run `operator auth {name}` later."
                    )
            # either way, fall through — runtime will skip the server via
            # the same 15.7.3 oauth_needed gate that catches it today.
            continue

        if status == "missing_env":
            # Default Y (continue) — missing creds usually means the user
            # intentionally hasn't set up that MCP yet. n exits so they
            # can edit .env and re-run without having to Ctrl+C mid-join.
            answer = _ask(
                input_fn,
                output_fn,
                f"{name}: continue without it? [Y/n] ",
                default="y",
            )
            if answer.lower() == "n":
                output_fn(
                    f"Aborting. Set {', '.join(rec.get('missing_vars', []))} in .env and re-run."
                )
                return PREFLIGHT_USER_ABORT
            continue

        if status == "prereq_missing":
            # Default Y — same logic as missing_env: user may not use this
            # MCP this meeting, and a stale claude-code auth shouldn't
            # block a pm or designer run.
            answer = _ask(
                input_fn,
                output_fn,
                f"{name}: continue anyway? [Y/n] ",
                default="y",
            )
            if answer.lower() == "n":
                output_fn(f"Aborting. Fix {name} prereqs and re-run.")
                return PREFLIGHT_USER_ABORT
            continue

    return PREFLIGHT_OK


def _ask(input_fn, output_fn, prompt: str, *, default: str) -> str:
    """Prompt with a single-char default. Empty input returns `default`.

    Thin wrapper so the rest of the module stays testable without
    pulling in rich.Prompt (which the wizard uses but the runtime
    pre-flight doesn't — we're in the plain CLI pre-browser phase and
    want no Rich console noise here).
    """
    try:
        raw = input_fn(prompt)
    except EOFError:
        # Non-interactive stdin (pipe, redirected input): fall back to
        # the default so CI/scripted launches don't hang waiting for a
        # keypress. Users who want full non-interactive are expected to
        # pass --no-preflight instead.
        return default
    raw = (raw or "").strip()
    return raw or default
