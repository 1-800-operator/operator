"""
Best-effort version-stale check for the operator plugin.

Compares the locally-cached marketplace metadata to the published
marketplace.json on GitHub. If a newer plugin version exists, returns
a one-line hint string that ChatRunner logs after join (log-only — a
plugin-version notice is noise for meeting participants). Silent
failure on any error (offline, parse mismatch, missing files) — this
is a courtesy, not a load-bearing check.

The hint points the user at `/operator:update`, the plugin-side skill
that runs the two `claude plugin` commands needed to refresh the local
marketplace cache + install the new plugin version.
"""
import json
import logging
import os
import urllib.request

log = logging.getLogger(__name__)

_LOCAL_MARKETPLACE = os.path.expanduser(
    "~/.claude/plugins/marketplaces/1-800-operator/.claude-plugin/marketplace.json"
)
_REMOTE_MARKETPLACE_URL = (
    "https://raw.githubusercontent.com/1-800-operator/operator/main/"
    ".claude-plugin/marketplace.json"
)
_FETCH_TIMEOUT_SECONDS = 5


def _parse_version(s: str) -> tuple[int, ...]:
    """Parse a semver-ish "X.Y.Z" into a tuple for ordered comparison.

    Non-integer components yield (0,) so a malformed version doesn't
    spuriously appear newer than a well-formed one.
    """
    if not s:
        return (0,)
    try:
        return tuple(int(p) for p in s.strip().split("."))
    except ValueError:
        return (0,)


def _plugin_version_from(data: dict) -> str | None:
    """Pull the `operator` plugin's version out of a marketplace.json dict."""
    for plugin in data.get("plugins", []) or []:
        if plugin.get("name") == "operator":
            return plugin.get("version") or None
    return None


def check_for_newer_plugin() -> str | None:
    """Return a chat-line hint if a newer operator plugin version is
    available, else None.

    Compares the local marketplace cache (refreshed by `claude plugin
    marketplace update`) against the live marketplace.json on
    github.com/1-800-operator/operator. The local cache is the proxy
    for "what version this user's Claude Code knows about" — if
    remote > local, the user should run /operator:update.

    Silent on any failure: missing local cache (plugin never
    installed), missing network, malformed JSON. Returns None and
    swallows the error.
    """
    if not os.path.exists(_LOCAL_MARKETPLACE):
        return None
    try:
        with open(_LOCAL_MARKETPLACE) as f:
            local = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.debug(f"update_check: local marketplace unreadable: {e}")
        return None
    local_version = _plugin_version_from(local)
    if not local_version:
        return None

    try:
        req = urllib.request.Request(
            _REMOTE_MARKETPLACE_URL,
            headers={"Cache-Control": "no-cache"},
        )
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_SECONDS) as resp:
            remote = json.load(resp)
    except Exception as e:
        log.debug(f"update_check: remote fetch failed: {e}")
        return None
    remote_version = _plugin_version_from(remote)
    if not remote_version:
        return None

    if _parse_version(remote_version) > _parse_version(local_version):
        return (
            f"A newer operator version ({remote_version}) is available — "
            f"type /operator:update in Claude Code to upgrade."
        )
    return None
