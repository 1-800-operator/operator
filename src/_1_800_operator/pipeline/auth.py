"""OAuth helper — foreground-run an mcp-remote server once to seed its
token cache.

Factored out of `__main__._run_auth` in Phase 15.7.4 so both the CLI
(`operator auth <mcp>`) and the wizard's inline "authorize now?" prompt
hit the same code path. No behavior change vs the original; just a
pure-function entry point.

Zero-hang guarantee at meeting join depends on this: MCPClient.connect_all
fails fast with kind="oauth_needed" when the cache is missing, and the
join-time banner tells the user to run this. The wizard can also run it
mid-setup for OAuth servers the user just toggled on.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
from pathlib import Path

import yaml

_AGENTS_DIR = Path.home() / ".operator" / "agents"


def find_oauth_mcp_config(mcp_name: str) -> dict | None:
    """Find `mcp_name`'s raw yaml config by scanning ~/.operator/agents/.

    Returns the dict for the first bot that declares `mcp_name` with
    auth:oauth, or None if no bot declares it that way. The wizard
    guarantees per-MCP fields stay in sync across bots, so picking the
    first match is safe.
    """
    if not _AGENTS_DIR.exists():
        return None
    for bot_dir in sorted(_AGENTS_DIR.iterdir()):
        cfg_path = bot_dir / "config.yaml"
        if not cfg_path.exists():
            continue
        try:
            with open(cfg_path) as f:
                bot_cfg = yaml.safe_load(f) or {}
        except Exception:
            continue
        servers = bot_cfg.get("mcp_servers") or {}
        srv = servers.get(mcp_name)
        if srv and srv.get("auth") == "oauth":
            return srv
    return None


def run_auth(mcp_name: str) -> int:
    """Spawn an OAuth MCP in the foreground once to seed its token cache.

    Reads the MCP's command/args/env/auth_url from the first bot config
    that declares it with auth:oauth. Inherits stdout/stderr so the user
    sees mcp-remote's "visit this URL" prompt. Polls for the cache file
    (~/.mcp-auth/mcp-remote-*/<md5(auth_url)>_tokens.json) and exits
    cleanly once it appears, terminating the subprocess.

    Returns a shell exit code (0 success, 2 config error, 130 user
    aborted, non-zero otherwise) so the CLI wrapper can forward it
    verbatim.
    """
    srv = find_oauth_mcp_config(mcp_name)
    if srv is None:
        print(f"No OAuth MCP named {mcp_name!r} in {_AGENTS_DIR} (scanned bots for auth:oauth entries).")
        return 2
    auth_url = srv.get("auth_url", "")
    if not auth_url:
        print(f"MCP {mcp_name!r} has auth:oauth but no auth_url — cannot watch for cache file.")
        return 2

    # Resolve ${VAR} references the same way config.py does for runtime env,
    # minus the unsafe-key filter (we're not launching a bot, just mcp-remote).
    raw_env = srv.get("env") or {}
    resolved_env: dict[str, str] = {}
    for k, v in raw_env.items():
        if isinstance(v, str):
            m = re.fullmatch(r"\$\{([^}]+)\}", v)
            resolved_env[k] = os.environ.get(m.group(1), "") if m else v
        else:
            resolved_env[k] = v

    cmd = srv["command"]
    args = srv.get("args") or []
    env = {**os.environ, **resolved_env}

    url_hash = hashlib.md5(auth_url.encode()).hexdigest()
    mcp_auth_base = Path.home() / ".mcp-auth"

    def _find_token_file() -> Path | None:
        if not mcp_auth_base.exists():
            return None
        for d in mcp_auth_base.glob("mcp-remote-*"):
            f = d / f"{url_hash}_tokens.json"
            if f.exists():
                return f
        return None

    existing = _find_token_file()
    if existing is not None:
        print(f"Token already cached at {existing}")
        print(f"Delete the file first if you want to re-authorize.")
        return 0

    print(f"Launching: {cmd} {' '.join(args)}")
    print(f"Watching:  ~/.mcp-auth/mcp-remote-*/{url_hash}_tokens.json")
    print(f"Complete OAuth in your browser when it opens; this exits once the token lands.\n")

    # stdin=PIPE keeps the subprocess's stdin open (mcp-remote exits on EOF)
    # without us having to write anything. stdout/stderr inherit so the user
    # sees the "authorize this client" URL.
    proc = subprocess.Popen(
        [cmd, *args],
        env=env,
        stdin=subprocess.PIPE,
    )
    try:
        while True:
            found = _find_token_file()
            if found is not None:
                print(f"\n✓ Token cached at {found} — {mcp_name} is now authorized.")
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return 0
            if proc.poll() is not None:
                print(f"\nmcp-remote exited with code {proc.returncode} before the token file appeared.")
                return proc.returncode or 1
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nAborted — OAuth not completed.")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        return 130
