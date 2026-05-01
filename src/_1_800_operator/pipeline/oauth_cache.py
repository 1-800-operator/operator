"""mcp-remote OAuth token-cache helpers — shared by 15.7.3 startup gate
(mcp_client) and 15.7.4 readiness reports (readiness, wizard).

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
    """Return the lexicographically-latest ~/.mcp-auth/mcp-remote-<version>/ dir.

    mcp-remote bumps the version suffix on each release; picking the
    largest match means a user who has upgraded locally doesn't start
    hitting a stale lower-version cache. Returns None when ~/.mcp-auth
    doesn't exist or holds no mcp-remote-* subdir.
    """
    base = Path.home() / ".mcp-auth"
    if not base.exists():
        return None
    candidates = sorted(d for d in base.glob("mcp-remote-*") if d.is_dir())
    return candidates[-1] if candidates else None


def oauth_cache_exists(auth_url: str) -> bool:
    """True iff mcp-remote has a token cache file for auth_url.

    Existence ≠ validity — a revoked/expired token still has a file on
    disk. Runtime sniff (`_looks_like_auth_error` in mcp_client) catches
    the revoked case; this check is only about preventing mcp-remote
    from hanging at meeting join waiting for a browser OAuth popup.
    """
    if not auth_url:
        return False
    cache_dir = mcp_remote_cache_dir()
    if cache_dir is None:
        return False
    url_hash = hashlib.md5(auth_url.encode()).hexdigest()
    return (cache_dir / f"{url_hash}_tokens.json").exists()
