"""
Phase 15.7.4 — MCP readiness helper + wizard status screen.

Covers:
  1. _missing_env_vars mirrors config._resolve_env_vars' missingness logic
     (pure env-refs, no side effects) — varieties: no refs, plain string,
     multi-ref, duplicate refs, non-string value.
  2. report_mcp_readiness per-status output:
     - ok for env server with every ${VAR} set,
     - missing_env for env server with at least one unset ${VAR},
     - ok for oauth server with cache present,
     - oauth_needed for oauth server with cache absent,
     - ok for claude-code with binaries + loggedIn,
     - prereq_missing for claude-code with git missing / claude missing /
       loggedIn=false / exit-non-zero / non-json / timeout.
  3. enabled_only honored (default True drops disabled blocks; False keeps).
  4. Pre-resolved missing_vars (runtime shape, list present on the block)
     short-circuits the env-ref scan — runtime and wizard agree.
  5. credentials_url passes through as fix_url for all non-ok statuses.
  6. check_claude_code_auth=False skips the subprocess hop.

Uses monkey-patched Path.home (oauth_cache), shutil.which (readiness),
and subprocess.run (readiness) so no binary or filesystem state leaks.

Run:
    source venv/bin/activate
    python tests/test_1574_readiness.py
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _1_800_operator.pipeline.readiness import (
    STATUS_GLYPH,
    _missing_env_vars,
    _probe_claude_code,
    report_mcp_readiness,
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


def _seed_oauth_cache(home: Path, url: str) -> None:
    """Drop a token file for `url` into ~/.mcp-auth/mcp-remote-X.Y.Z/."""
    base = home / ".mcp-auth" / "mcp-remote-0.1.38"
    base.mkdir(parents=True, exist_ok=True)
    url_hash = hashlib.md5(url.encode()).hexdigest()
    (base / f"{url_hash}_tokens.json").write_text("{}")


def _run_ok(stdout: str, returncode: int = 0) -> MagicMock:
    """Return a subprocess.run-like CompletedProcess stub."""
    r = MagicMock()
    r.stdout = stdout
    r.stderr = ""
    r.returncode = returncode
    return r


# ---------------------------------------------------------------------------
# Test 1: _missing_env_vars
# ---------------------------------------------------------------------------

def test_missing_env_vars_none_when_no_refs():
    assert _missing_env_vars({"FOO": "literal"}) == []
    assert _missing_env_vars({}) == []
    assert _missing_env_vars(None) == []
    print("PASS  test_missing_env_vars_none_when_no_refs")


def test_missing_env_vars_finds_unset_refs():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("TEST_READY_A", None)
        os.environ.pop("TEST_READY_B", None)
        os.environ["TEST_READY_A"] = "set"
        missing = _missing_env_vars({"A": "${TEST_READY_A}", "B": "${TEST_READY_B}"})
        assert missing == ["TEST_READY_B"], missing
    print("PASS  test_missing_env_vars_finds_unset_refs")


def test_missing_env_vars_deduplicates():
    os.environ.pop("TEST_READY_DUP", None)
    # Same var referenced twice across two keys should appear once.
    missing = _missing_env_vars({"A": "${TEST_READY_DUP}", "B": "${TEST_READY_DUP}"})
    assert missing == ["TEST_READY_DUP"], missing
    print("PASS  test_missing_env_vars_deduplicates")


def test_missing_env_vars_ignores_non_string_values():
    # Only string values with ${VAR} refs are scanned — dicts/lists/ints skipped.
    os.environ.pop("TEST_READY_C", None)
    missing = _missing_env_vars({"A": 42, "B": ["x"], "C": "${TEST_READY_C}"})
    assert missing == ["TEST_READY_C"], missing
    print("PASS  test_missing_env_vars_ignores_non_string_values")


# ---------------------------------------------------------------------------
# Test 2: report_mcp_readiness — env servers
# ---------------------------------------------------------------------------

def test_env_server_ok_when_all_vars_set():
    os.environ["TEST_GH_TOKEN"] = "abc123"
    servers = {
        "github": {
            "enabled": True,
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${TEST_GH_TOKEN}"},
            "credentials_url": "https://github.com/settings/tokens",
        }
    }
    report = report_mcp_readiness(servers)
    assert report["github"]["status"] == "ok", report
    assert report["github"]["fix_url"] is None, report
    print("PASS  test_env_server_ok_when_all_vars_set")


def test_env_server_missing_env_surfaces_vars_and_url():
    os.environ.pop("TEST_FIGMA_MISSING", None)
    servers = {
        "figma": {
            "enabled": True,
            "env": {"FIGMA_API_KEY": "${TEST_FIGMA_MISSING}"},
            "credentials_url": "https://www.figma.com/developers/api#access-tokens",
        }
    }
    report = report_mcp_readiness(servers)
    rec = report["figma"]
    assert rec["status"] == "missing_env", rec
    assert rec["missing_vars"] == ["TEST_FIGMA_MISSING"], rec
    assert "TEST_FIGMA_MISSING" in rec["fix"], rec
    assert rec["fix_url"] == "https://www.figma.com/developers/api#access-tokens", rec
    print("PASS  test_env_server_missing_env_surfaces_vars_and_url")


def test_env_server_honors_pre_resolved_missing_vars():
    """Runtime shape (MCP_SERVERS) pre-computes missing_vars; respect it."""
    # env block is already resolved (plain strings, no ${VAR}), but
    # missing_vars is set — helper should trust the list.
    servers = {
        "github": {
            "enabled": True,
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": ""},
            "missing_vars": ["GITHUB_TOKEN"],
            "credentials_url": "https://github.com/settings/tokens",
        }
    }
    report = report_mcp_readiness(servers)
    assert report["github"]["status"] == "missing_env", report
    assert report["github"]["missing_vars"] == ["GITHUB_TOKEN"], report
    print("PASS  test_env_server_honors_pre_resolved_missing_vars")


# ---------------------------------------------------------------------------
# Test 3: report_mcp_readiness — OAuth servers
# ---------------------------------------------------------------------------

@_with_fake_home
def test_oauth_server_ok_when_cache_present(home):
    url = "https://mcp.linear.app/mcp"
    _seed_oauth_cache(home, url)
    servers = {
        "linear": {
            "enabled": True,
            "auth": "oauth",
            "auth_url": url,
            "env": {},
            "credentials_url": "https://linear.app/settings/account/security",
        }
    }
    report = report_mcp_readiness(servers)
    assert report["linear"]["status"] == "ok", report
    print("PASS  test_oauth_server_ok_when_cache_present")


@_with_fake_home
def test_oauth_server_needed_when_cache_absent(home):
    servers = {
        "linear": {
            "enabled": True,
            "auth": "oauth",
            "auth_url": "https://mcp.linear.app/mcp",
            "env": {},
            "credentials_url": "https://linear.app/settings/account/security",
        }
    }
    report = report_mcp_readiness(servers)
    rec = report["linear"]
    assert rec["status"] == "oauth_needed", rec
    assert "operator auth linear" in rec["fix"], rec
    assert rec["auth_url"] == "https://mcp.linear.app/mcp", rec
    assert rec["fix_url"] == "https://linear.app/settings/account/security", rec
    print("PASS  test_oauth_server_needed_when_cache_absent")


# ---------------------------------------------------------------------------
# Test 4: report_mcp_readiness — claude-code prereq gate
# ---------------------------------------------------------------------------

def test_claude_code_ok_when_binaries_and_logged_in():
    servers = {
        "claude-code": {
            "enabled": True,
            "env": {},
            "credentials_url": "https://docs.claude.com/en/docs/claude-code/overview",
        }
    }
    with patch("_1_800_operator.pipeline.readiness.shutil.which", return_value="/usr/bin/fake"), \
         patch("_1_800_operator.pipeline.readiness.subprocess.run",
               return_value=_run_ok(json.dumps({"loggedIn": True}))):
        report = report_mcp_readiness(servers)
    assert report["claude-code"]["status"] == "ok", report
    print("PASS  test_claude_code_ok_when_binaries_and_logged_in")


def test_claude_code_prereq_missing_when_git_absent():
    servers = {"claude-code": {"enabled": True, "env": {},
                               "credentials_url": "https://docs.claude.com/x"}}
    with patch("_1_800_operator.pipeline.readiness.shutil.which",
               side_effect=lambda name: None if name == "git" else "/usr/bin/claude"):
        report = report_mcp_readiness(servers)
    rec = report["claude-code"]
    assert rec["status"] == "prereq_missing", rec
    assert "git" in rec["fix"].lower(), rec
    assert rec["fix_url"] == "https://docs.claude.com/x", rec
    print("PASS  test_claude_code_prereq_missing_when_git_absent")


def test_claude_code_prereq_missing_when_claude_absent():
    servers = {"claude-code": {"enabled": True, "env": {},
                               "credentials_url": "https://docs.claude.com/x"}}
    with patch("_1_800_operator.pipeline.readiness.shutil.which",
               side_effect=lambda name: "/usr/bin/git" if name == "git" else None):
        report = report_mcp_readiness(servers)
    rec = report["claude-code"]
    assert rec["status"] == "prereq_missing", rec
    assert "claude" in rec["fix"].lower(), rec
    print("PASS  test_claude_code_prereq_missing_when_claude_absent")


def test_claude_code_prereq_missing_when_not_logged_in():
    servers = {"claude-code": {"enabled": True, "env": {},
                               "credentials_url": "https://docs.claude.com/x"}}
    with patch("_1_800_operator.pipeline.readiness.shutil.which", return_value="/usr/bin/fake"), \
         patch("_1_800_operator.pipeline.readiness.subprocess.run",
               return_value=_run_ok(json.dumps({"loggedIn": False}))):
        report = report_mcp_readiness(servers)
    rec = report["claude-code"]
    assert rec["status"] == "prereq_missing", rec
    assert "claude auth login" in rec["fix"], rec
    print("PASS  test_claude_code_prereq_missing_when_not_logged_in")


def test_claude_code_prereq_missing_when_auth_status_nonzero():
    servers = {"claude-code": {"enabled": True, "env": {},
                               "credentials_url": "https://docs.claude.com/x"}}
    with patch("_1_800_operator.pipeline.readiness.shutil.which", return_value="/usr/bin/fake"), \
         patch("_1_800_operator.pipeline.readiness.subprocess.run",
               return_value=_run_ok("", returncode=2)):
        report = report_mcp_readiness(servers)
    rec = report["claude-code"]
    assert rec["status"] == "prereq_missing", rec
    assert "claude auth login" in rec["fix"], rec
    print("PASS  test_claude_code_prereq_missing_when_auth_status_nonzero")


def test_claude_code_prereq_missing_on_malformed_json():
    servers = {"claude-code": {"enabled": True, "env": {},
                               "credentials_url": "https://docs.claude.com/x"}}
    with patch("_1_800_operator.pipeline.readiness.shutil.which", return_value="/usr/bin/fake"), \
         patch("_1_800_operator.pipeline.readiness.subprocess.run",
               return_value=_run_ok("not-json")):
        report = report_mcp_readiness(servers)
    rec = report["claude-code"]
    assert rec["status"] == "prereq_missing", rec
    assert "claude auth login" in rec["fix"], rec
    print("PASS  test_claude_code_prereq_missing_on_malformed_json")


def test_claude_code_prereq_missing_on_timeout():
    servers = {"claude-code": {"enabled": True, "env": {},
                               "credentials_url": "https://docs.claude.com/x"}}
    def _raise_timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=5)
    with patch("_1_800_operator.pipeline.readiness.shutil.which", return_value="/usr/bin/fake"), \
         patch("_1_800_operator.pipeline.readiness.subprocess.run", side_effect=_raise_timeout):
        report = report_mcp_readiness(servers)
    rec = report["claude-code"]
    assert rec["status"] == "prereq_missing", rec
    assert "did not respond" in rec["fix"], rec
    print("PASS  test_claude_code_prereq_missing_on_timeout")


def test_claude_code_skips_auth_probe_when_disabled():
    """check_claude_code_auth=False returns ok on binary presence alone."""
    servers = {"claude-code": {"enabled": True, "env": {},
                               "credentials_url": "https://docs.claude.com/x"}}
    with patch("_1_800_operator.pipeline.readiness.shutil.which", return_value="/usr/bin/fake"), \
         patch("_1_800_operator.pipeline.readiness.subprocess.run") as run_mock:
        report = report_mcp_readiness(servers, check_claude_code_auth=False)
        assert not run_mock.called, "auth probe should not spawn subprocess when disabled"
    assert report["claude-code"]["status"] == "ok", report
    print("PASS  test_claude_code_skips_auth_probe_when_disabled")


# ---------------------------------------------------------------------------
# Test 5: enabled_only flag
# ---------------------------------------------------------------------------

def test_enabled_only_default_drops_disabled_blocks():
    os.environ["TEST_ENABLED_KEY"] = "x"
    servers = {
        "github": {"enabled": True, "env": {"K": "${TEST_ENABLED_KEY}"}},
        "notion": {"enabled": False, "env": {"K": "${TEST_ENABLED_KEY}"}},
    }
    report = report_mcp_readiness(servers)
    assert set(report.keys()) == {"github"}, report
    print("PASS  test_enabled_only_default_drops_disabled_blocks")


def test_enabled_only_false_includes_disabled_blocks():
    os.environ["TEST_ENABLED_KEY"] = "x"
    servers = {
        "github": {"enabled": True, "env": {"K": "${TEST_ENABLED_KEY}"}},
        "notion": {"enabled": False, "env": {"K": "${TEST_ENABLED_KEY}"}},
    }
    report = report_mcp_readiness(servers, enabled_only=False)
    assert set(report.keys()) == {"github", "notion"}, report
    print("PASS  test_enabled_only_false_includes_disabled_blocks")


# ---------------------------------------------------------------------------
# Test 6: glyph table stays in sync with statuses
# ---------------------------------------------------------------------------

def test_status_glyph_covers_every_status():
    emitted = {"ok", "missing_env", "oauth_needed", "prereq_missing"}
    assert emitted.issubset(STATUS_GLYPH.keys()), STATUS_GLYPH
    print("PASS  test_status_glyph_covers_every_status")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_missing_env_vars_none_when_no_refs,
        test_missing_env_vars_finds_unset_refs,
        test_missing_env_vars_deduplicates,
        test_missing_env_vars_ignores_non_string_values,
        test_env_server_ok_when_all_vars_set,
        test_env_server_missing_env_surfaces_vars_and_url,
        test_env_server_honors_pre_resolved_missing_vars,
        test_oauth_server_ok_when_cache_present,
        test_oauth_server_needed_when_cache_absent,
        test_claude_code_ok_when_binaries_and_logged_in,
        test_claude_code_prereq_missing_when_git_absent,
        test_claude_code_prereq_missing_when_claude_absent,
        test_claude_code_prereq_missing_when_not_logged_in,
        test_claude_code_prereq_missing_when_auth_status_nonzero,
        test_claude_code_prereq_missing_on_malformed_json,
        test_claude_code_prereq_missing_on_timeout,
        test_claude_code_skips_auth_probe_when_disabled,
        test_enabled_only_default_drops_disabled_blocks,
        test_enabled_only_false_includes_disabled_blocks,
        test_status_glyph_covers_every_status,
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
