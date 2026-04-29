"""
Test the always-re-import + per-entry merge for the claude agent's MCP block.

Validates the field-ownership rules:
  - command, args, env, auth, auth_url, description → source-driven
    (overwritten from ~/.claude.json on every sync)
  - enabled, hints, read_tools, confirm_tools → user-preserved
    (kept across syncs so meeting-scope tweaks survive)
  - servers missing in the discovered set are dropped
  - new servers are added

Usage:
    python tests/test_claude_mcp_sync.py
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BRAINCHILD_BOT", "claude")

import yaml

from brainchild import __main__ as bm
from brainchild.pipeline.claude_code_import import ImportedMCP


def _write_cfg(tmpdir: Path, payload: dict) -> Path:
    agents = tmpdir / "agents" / "claude"
    agents.mkdir(parents=True)
    cfg_path = agents / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(payload, sort_keys=False))
    return cfg_path


def _read_cfg(cfg_path: Path) -> dict:
    return yaml.safe_load(cfg_path.read_text()) or {}


def _run_sync_with(tmpdir: Path, discovered: list[ImportedMCP]):
    """Patch _AGENTS_DIR + discover_all_mcps, run sync."""
    with patch.object(bm, "_AGENTS_DIR", tmpdir / "agents"), \
         patch("brainchild.pipeline.claude_code_import.discover_all_mcps",
               return_value=(discovered, 0)), \
         patch("brainchild.pipeline.claude_code_import.append_env_placeholders",
               return_value=[]):
        bm._sync_claude_imports()


def test_first_sync_adds_servers():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        cfg = _write_cfg(td, {"mcp_servers": {}})
        mcp = ImportedMCP(
            name="sentry",
            block={
                "enabled": True, "description": "from src",
                "command": "npx", "args": ["-y", "sentry-mcp"],
                "env": {}, "auth": "env",
                "read_tools": [], "confirm_tools": [], "hints": "",
            },
            transport="stdio", env_vars_referenced=[],
        )
        _run_sync_with(td, [mcp])
        servers = _read_cfg(cfg)["mcp_servers"]
        assert "sentry" in servers, servers
        assert servers["sentry"]["command"] == "npx"
        print("✓ first sync adds new server")


def test_user_preserved_fields_survive():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        cfg = _write_cfg(td, {"mcp_servers": {
            "sentry": {
                "enabled": False,  # user disabled
                "command": "old-binary",
                "args": [],
                "hints": "use ONLY for SEV-1 incidents",
                "read_tools": ["get_issue"],
                "confirm_tools": ["resolve_issue"],
            }
        }})
        mcp = ImportedMCP(
            name="sentry",
            block={
                "enabled": True, "description": "fresh",
                "command": "npx", "args": ["-y", "sentry-mcp@latest"],
                "env": {}, "auth": "env",
                "read_tools": [], "confirm_tools": [], "hints": "",
            },
            transport="stdio", env_vars_referenced=[],
        )
        _run_sync_with(td, [mcp])
        s = _read_cfg(cfg)["mcp_servers"]["sentry"]
        # Source-driven fields overwritten
        assert s["command"] == "npx", s
        assert s["args"] == ["-y", "sentry-mcp@latest"], s
        # User-preserved fields kept
        assert s["enabled"] is False, "user's enabled=false was clobbered"
        assert s["hints"] == "use ONLY for SEV-1 incidents", s["hints"]
        assert s["read_tools"] == ["get_issue"], s
        assert s["confirm_tools"] == ["resolve_issue"], s
        print("✓ user-preserved fields survive sync")


def test_server_removed_when_missing_from_source():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        cfg = _write_cfg(td, {"mcp_servers": {
            "sentry": {"enabled": True, "command": "npx", "args": []},
            "notion": {"enabled": True, "command": "npx", "args": []},
        }})
        # Only sentry survives in source; notion was removed from ~/.claude.json
        mcp = ImportedMCP(
            name="sentry",
            block={"enabled": True, "command": "npx", "args": [],
                   "env": {}, "auth": "env", "description": "",
                   "read_tools": [], "confirm_tools": [], "hints": ""},
            transport="stdio", env_vars_referenced=[],
        )
        _run_sync_with(td, [mcp])
        servers = _read_cfg(cfg)["mcp_servers"]
        assert "sentry" in servers
        assert "notion" not in servers, "notion should have been dropped"
        print("✓ servers missing from source are dropped")


def test_legacy_done_flag_removed():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        cfg = _write_cfg(td, {
            "_claude_import_done": True,
            "mcp_servers": {},
        })
        _run_sync_with(td, [])
        loaded = _read_cfg(cfg)
        assert "_claude_import_done" not in loaded, "legacy flag should be stripped"
        print("✓ legacy _claude_import_done flag stripped on sync")


def test_no_op_does_not_rewrite():
    """When the merged result equals the on-disk state, the file should not be touched."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        block = {
            "enabled": True, "description": "fresh",
            "command": "npx", "args": ["-y", "x"],
            "env": {}, "auth": "env",
            "read_tools": [], "confirm_tools": [], "hints": "",
        }
        cfg = _write_cfg(td, {"mcp_servers": {"sentry": block}})
        before_mtime = cfg.stat().st_mtime_ns
        # Brief sleep so mtime resolution can register a change if we did write.
        import time
        time.sleep(0.01)
        mcp = ImportedMCP(name="sentry", block=block, transport="stdio", env_vars_referenced=[])
        _run_sync_with(td, [mcp])
        after_mtime = cfg.stat().st_mtime_ns
        assert before_mtime == after_mtime, "no-op sync should not rewrite the file"
        print("✓ no-op sync does not rewrite (formatting preserved)")


if __name__ == "__main__":
    test_first_sync_adds_servers()
    test_user_preserved_fields_survive()
    test_server_removed_when_missing_from_source()
    test_legacy_done_flag_removed()
    test_no_op_does_not_rewrite()
    print("\nAll claude MCP sync tests passed.")
