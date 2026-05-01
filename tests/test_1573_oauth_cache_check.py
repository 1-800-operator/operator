"""
Phase 15.7.3 — OAuth cache-path pre-check.

Covers:
  1. _mcp_remote_cache_dir returns None when ~/.mcp-auth is absent; picks
     the lexicographically-latest version dir when multiple exist.
  2. _oauth_cache_exists True iff <md5(auth_url)>_tokens.json lives in the
     latest cache dir; returns False on empty auth_url and False when
     ~/.mcp-auth doesn't exist at all.
  3. MCPClient.connect_all skips spawn for auth=oauth servers whose cache
     is absent, recording kind="oauth_needed" with a fix that names
     `operator auth <name>`, and still attempts spawn for servers
     whose cache is present.
  4. ChatRunner._post_mcp_failure_banner renders the oauth_needed kind
     with the `operator auth <name>` call-to-action inline.

Uses a tmpdir + monkey-patched Path.home so real ~/.mcp-auth is untouched.

Run:
    source venv/bin/activate
    python tests/test_1573_oauth_cache_check.py
"""
import hashlib
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
os.environ.setdefault("OPERATOR_BOT", "pm")

from _1_800_operator import config
from _1_800_operator.pipeline import mcp_client as mcp_mod
from _1_800_operator.pipeline.mcp_client import (
    MCPClient,
    _mcp_remote_cache_dir,
    _oauth_cache_exists,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _with_fake_home(fn):
    """Run fn(tmp_home: Path) with Path.home() pointed at a fresh temp dir."""
    def wrapper():
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch("_1_800_operator.pipeline.oauth_cache.Path.home", return_value=tmp_path):
                fn(tmp_path)
    wrapper.__name__ = fn.__name__
    return wrapper


# ---------------------------------------------------------------------------
# Test 1: _mcp_remote_cache_dir behavior
# ---------------------------------------------------------------------------

@_with_fake_home
def test_cache_dir_none_when_mcp_auth_missing(home):
    assert _mcp_remote_cache_dir() is None
    print("PASS  test_cache_dir_none_when_mcp_auth_missing")


@_with_fake_home
def test_cache_dir_none_when_no_version_dirs(home):
    (home / ".mcp-auth").mkdir()
    assert _mcp_remote_cache_dir() is None
    print("PASS  test_cache_dir_none_when_no_version_dirs")


@_with_fake_home
def test_cache_dir_picks_latest_version(home):
    base = home / ".mcp-auth"
    base.mkdir()
    (base / "mcp-remote-0.1.36").mkdir()
    (base / "mcp-remote-0.1.37").mkdir()
    (base / "mcp-remote-0.1.38").mkdir()
    selected = _mcp_remote_cache_dir()
    assert selected is not None
    assert selected.name == "mcp-remote-0.1.38", selected
    print("PASS  test_cache_dir_picks_latest_version")


# ---------------------------------------------------------------------------
# Test 2: _oauth_cache_exists
# ---------------------------------------------------------------------------

@_with_fake_home
def test_oauth_cache_exists_false_on_empty_auth_url(home):
    assert _oauth_cache_exists("") is False
    print("PASS  test_oauth_cache_exists_false_on_empty_auth_url")


@_with_fake_home
def test_oauth_cache_exists_false_when_mcp_auth_missing(home):
    assert _oauth_cache_exists("https://mcp.linear.app/mcp") is False
    print("PASS  test_oauth_cache_exists_false_when_mcp_auth_missing")


@_with_fake_home
def test_oauth_cache_exists_true_for_matching_token_file(home):
    base = home / ".mcp-auth" / "mcp-remote-0.1.38"
    base.mkdir(parents=True)
    url = "https://example.test/mcp"
    url_hash = hashlib.md5(url.encode()).hexdigest()
    (base / f"{url_hash}_tokens.json").write_text("{}")
    assert _oauth_cache_exists(url) is True
    # Different URL → different hash → False
    assert _oauth_cache_exists("https://other.test/mcp") is False
    print("PASS  test_oauth_cache_exists_true_for_matching_token_file")


# ---------------------------------------------------------------------------
# Test 3: MCPClient.connect_all OAuth pre-check
# ---------------------------------------------------------------------------

def test_connect_all_skips_spawn_when_oauth_cache_missing():
    """auth=oauth server with missing token cache → no spawn, kind=oauth_needed."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        fake_servers = {
            "linear": {
                "command": "npx",
                "args": ["-y", "mcp-remote", "https://mcp.linear.app/mcp"],
                "env": {},
                "missing_vars": [],
                "auth": "oauth",
                "auth_url": "https://mcp.linear.app/mcp",
                "hints": "",
                "confirm_tools": set(),
                "read_tools": set(),
            }
        }
        spawn_attempts = []

        with patch("_1_800_operator.pipeline.oauth_cache.Path.home", return_value=tmp_path), \
             patch.object(config, "MCP_SERVERS", fake_servers):
            client = MCPClient()
            client._start_loop = lambda: None  # skip event-loop startup
            client._connect_server = lambda n, s: spawn_attempts.append(n) or []
            discovered = client.connect_all()

        assert spawn_attempts == [], f"should not have tried to spawn; got {spawn_attempts}"
        assert "linear" in client.startup_failures
        info = client.startup_failures["linear"]
        assert info["kind"] == "oauth_needed", info
        assert "operator auth linear" in info["fix"]
        assert info["auth_url"] == "https://mcp.linear.app/mcp"
        assert discovered == []
    print("PASS  test_connect_all_skips_spawn_when_oauth_cache_missing")


def test_connect_all_spawns_when_oauth_cache_present():
    """auth=oauth server with token cache present → attempts spawn normally."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cache_dir = tmp_path / ".mcp-auth" / "mcp-remote-0.1.38"
        cache_dir.mkdir(parents=True)
        url = "https://mcp.linear.app/mcp"
        (cache_dir / f"{hashlib.md5(url.encode()).hexdigest()}_tokens.json").write_text("{}")

        fake_servers = {
            "linear": {
                "command": "npx",
                "args": ["-y", "mcp-remote", url],
                "env": {},
                "missing_vars": [],
                "auth": "oauth",
                "auth_url": url,
                "hints": "",
                "confirm_tools": set(),
                "read_tools": set(),
            }
        }
        spawn_attempts = []

        with patch("_1_800_operator.pipeline.oauth_cache.Path.home", return_value=tmp_path), \
             patch.object(config, "MCP_SERVERS", fake_servers):
            client = MCPClient()
            client._start_loop = lambda: None
            client._connect_server = lambda n, s: spawn_attempts.append(n) or []
            client.connect_all()

        assert spawn_attempts == ["linear"], spawn_attempts
        assert client.startup_failures == {}, client.startup_failures
    print("PASS  test_connect_all_spawns_when_oauth_cache_present")


def test_connect_all_env_auth_ignores_cache_path():
    """auth=env (default) server → cache path never checked, spawn attempted."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        fake_servers = {
            "github": {
                "command": "./github-mcp-server",
                "args": ["stdio"],
                "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_fake"},
                "missing_vars": [],
                "auth": "env",
                "auth_url": "",
                "hints": "",
                "confirm_tools": set(),
                "read_tools": set(),
            }
        }
        spawn_attempts = []

        with patch("_1_800_operator.pipeline.oauth_cache.Path.home", return_value=tmp_path), \
             patch.object(config, "MCP_SERVERS", fake_servers):
            client = MCPClient()
            client._start_loop = lambda: None
            client._connect_server = lambda n, s: spawn_attempts.append(n) or []
            client.connect_all()

        assert spawn_attempts == ["github"], spawn_attempts
    print("PASS  test_connect_all_env_auth_ignores_cache_path")


# ---------------------------------------------------------------------------
# Test 4: Banner renders oauth_needed kind with call-to-action
# ---------------------------------------------------------------------------

def test_banner_renders_oauth_needed_with_auth_command():
    """oauth_needed → fragment inlines `operator auth <name>` so user can act."""
    # Import late so the module's OPERATOR_BOT default applies.
    from _1_800_operator.pipeline.chat_runner import ChatRunner

    connector = MagicMock()
    llm = MagicMock()
    mcp = MagicMock()
    mcp.startup_failures = {
        "linear": {
            "kind": "oauth_needed",
            "fix": "run `operator auth linear` once to authorize — token is cached after",
            "auth_url": "https://mcp.linear.app/mcp",
            "raw": "oauth cache missing",
        }
    }
    runner = ChatRunner(connector, llm, mcp_client=mcp)
    sent = []
    runner._send = lambda t, kind="chat": sent.append(t)
    runner._post_mcp_failure_banner()
    assert len(sent) == 1, sent
    line = sent[0]
    assert "linear didn't load" in line
    assert "operator auth linear" in line
    assert line.endswith("Ask for details.")
    print("PASS  test_banner_renders_oauth_needed_with_auth_command")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_cache_dir_none_when_mcp_auth_missing,
        test_cache_dir_none_when_no_version_dirs,
        test_cache_dir_picks_latest_version,
        test_oauth_cache_exists_false_on_empty_auth_url,
        test_oauth_cache_exists_false_when_mcp_auth_missing,
        test_oauth_cache_exists_true_for_matching_token_file,
        test_connect_all_skips_spawn_when_oauth_cache_missing,
        test_connect_all_spawns_when_oauth_cache_present,
        test_connect_all_env_auth_ignores_cache_path,
        test_banner_renders_oauth_needed_with_auth_command,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
