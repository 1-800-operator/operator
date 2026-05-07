"""mcp-remote OAuth token-cache helpers — used by readiness reports
(readiness, wizard status screen).

Pure: no dependency on operator.config, so importable from the wizard
(which runs before OPERATOR_BOT is set) and from the runtime (which
has a bot selected). The hashing mirrors mcp-remote's `getServerUrlHash`
on the happy path (md5(serverUrl) — no authorize_resource/headers),
which is the shape every OAuth MCP we ship uses.
"""
from __future__ import annotations

import hashlib
from pathlib import Path


def mcp_remote_cache_dir() -> Path | None:
    """Return the most-recently-modified ~/.mcp-auth/mcp-remote-<version>/ dir.

    Mtime, not lexicographic sort — as strings, `mcp-remote-0.1.10` <
    `mcp-remote-0.1.9` (a digit-boundary inversion), so a user with both
    an older and a newer cache could silently route to the older one.
    The currently-installed mcp-remote writes to its own version dir at
    runtime, so its mtime is freshest. Returns None when ~/.mcp-auth
    doesn't exist or holds no mcp-remote-* subdir.
    """
    base = Path.home() / ".mcp-auth"
    if not base.exists():
        return None
    candidates = [d for d in base.glob("mcp-remote-*") if d.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda d: d.stat().st_mtime)


def oauth_cache_exists(auth_url: str) -> bool:
    """True iff mcp-remote has a token cache file for auth_url.

    Existence ≠ validity — a revoked/expired token still has a file on
    disk. The runtime catches the revoked case at meeting join; this
    check is only about preventing mcp-remote from hanging waiting for
    a browser OAuth popup.
    """
    if not auth_url:
        return False
    cache_dir = mcp_remote_cache_dir()
    if cache_dir is None:
        return False
    url_hash = hashlib.md5(auth_url.encode()).hexdigest()
    return (cache_dir / f"{url_hash}_tokens.json").exists()
