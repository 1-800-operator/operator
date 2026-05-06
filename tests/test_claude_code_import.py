"""
Phase 14.19.7-F — claude_code_import (slimmed).

The wizard-era discovery helpers (extract_imported_mcps,
discover_hosted_mcps_via_cli, discover_mcp_health, discover_all_mcps,
read_user_claude_md, append_env_placeholders, normalize_path_for_storage,
_classify_transport, _slugify_mcp_name, _wrap_http_as_stdio) were dropped
in 14.19.7-F alongside the rest of the wizard cleanup. The remaining
helpers cover what claude_cli still calls at boot:

  - read_user_mcp_config — pure read of `~/.claude.json` (or the
    `~/.claude/settings.json` fallback). Used by claude_cli to map
    operator's `disabledMcpjsonServers` overlay back to JSON keys.

`claude_code_installed_and_logged_in` is a thin wrapper over
readiness._probe_claude_code; the underlying probe is exercised in
tests/test_1574_readiness.py, so we don't double-test it here.

Run:
    source venv/bin/activate
    python tests/test_claude_code_import.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _1_800_operator.pipeline.claude_code_import import read_user_mcp_config


def _with_fake_home(fn):
    """Run fn(tmp_home: Path) with ~/.claude.json + ~/.claude/ sandboxed
    under a fresh temp dir.
    """
    def wrapper():
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with patch(
                "_1_800_operator.pipeline.claude_code_import.Path.home",
                return_value=home,
            ):
                import _1_800_operator.pipeline.claude_code_import as mod
                with patch.object(mod, "_USER_CONFIG_CANDIDATES", [
                    home / ".claude.json",
                    home / ".claude" / "settings.json",
                ]):
                    fn(home)
    wrapper.__name__ = fn.__name__
    return wrapper


@_with_fake_home
def test_read_user_mcp_config_missing_returns_empty(home):
    assert read_user_mcp_config() == {}
    print("PASS  test_read_user_mcp_config_missing_returns_empty")


@_with_fake_home
def test_read_user_mcp_config_malformed_returns_empty(home):
    (home / ".claude.json").write_text("{ not valid json")
    assert read_user_mcp_config() == {}
    print("PASS  test_read_user_mcp_config_malformed_returns_empty")


@_with_fake_home
def test_read_user_mcp_config_reads_top_level_json(home):
    payload = {"mcpServers": {"a": {"command": "x"}}}
    (home / ".claude.json").write_text(json.dumps(payload))
    got = read_user_mcp_config()
    assert got == payload, got
    print("PASS  test_read_user_mcp_config_reads_top_level_json")


@_with_fake_home
def test_read_user_mcp_config_fallback_to_settings_json(home):
    (home / ".claude").mkdir()
    payload = {"mcpServers": {"b": {"command": "y"}}}
    (home / ".claude" / "settings.json").write_text(json.dumps(payload))
    got = read_user_mcp_config()
    assert got == payload, got
    print("PASS  test_read_user_mcp_config_fallback_to_settings_json")


if __name__ == "__main__":
    tests = [
        test_read_user_mcp_config_missing_returns_empty,
        test_read_user_mcp_config_malformed_returns_empty,
        test_read_user_mcp_config_reads_top_level_json,
        test_read_user_mcp_config_fallback_to_settings_json,
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
