"""
Phase 15.9 — claude-code auto-import helpers.

Covers:
  1. _classify_transport — stdio (command), http/sse (type), url-only → http,
     empty dict → stdio fallback.
  2. _slugify_mcp_name — display-name → yaml-key normalization, edge cases.
  3. read_user_mcp_config — missing, malformed, valid JSON.
  4. extract_imported_mcps — stdio passthrough, http/sse wrapped via
     mcp-remote, env-var refs captured, malformed entries skipped,
     wrapped count correct.
  5. discover_hosted_mcps_via_cli — mocked subprocess.run: happy path
     (4 hosted MCPs parsed), non-zero return, FileNotFoundError, timeout,
     malformed lines skipped.
  6. discover_all_mcps — merges both sources, dedup by slug, wrapped
     count sums correctly.
  7. read_user_claude_md — missing, present.
  8. append_env_placeholders — new file creation, idempotent (var set as
     plain), idempotent (var already placeheld), newline handling, empty
     var_names, multiple runs each add their own header section.

Run:
    source venv/bin/activate
    python tests/test_claude_code_import.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _1_800_operator.pipeline.claude_code_import import (
    ImportedMCP,
    _classify_transport,
    _slugify_mcp_name,
    _wrap_http_as_stdio,
    append_env_placeholders,
    discover_all_mcps,
    discover_hosted_mcps_via_cli,
    discover_mcp_health,
    extract_imported_mcps,
    read_user_claude_md,
    read_user_mcp_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _with_fake_home(fn):
    """Run fn(tmp_home: Path) with ~/.claude.json, ~/.claude/, ~/.operator/
    all sandboxed under a fresh temp dir.
    """
    def wrapper():
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with patch(
                "_1_800_operator.pipeline.claude_code_import.Path.home",
                return_value=home,
            ):
                # Re-patch module-level constants derived from Path.home() at
                # import time — they won't pick up the patched Path.home.
                import _1_800_operator.pipeline.claude_code_import as mod
                # Bust the per-process `claude mcp list` cache so each test
                # sees a fresh mock invocation instead of whatever a prior
                # test (or the real CLI) populated.
                mod._CLAUDE_MCP_LIST_CACHE = None
                with (
                    patch.object(mod, "_USER_CONFIG_CANDIDATES", [
                        home / ".claude.json",
                        home / ".claude" / "settings.json",
                    ]),
                    patch.object(mod, "_USER_CLAUDE_MD", home / ".claude" / "CLAUDE.md"),
                ):
                    fn(home)
    wrapper.__name__ = fn.__name__
    return wrapper


def _run_ok(stdout: str, returncode: int = 0) -> MagicMock:
    r = MagicMock()
    r.stdout = stdout
    r.stderr = ""
    r.returncode = returncode
    return r


# ---------------------------------------------------------------------------
# _classify_transport
# ---------------------------------------------------------------------------

def test_classify_transport_stdio_when_command_present():
    assert _classify_transport({"command": "npx", "args": ["foo"]}) == "stdio"
    print("PASS  test_classify_transport_stdio_when_command_present")


def test_classify_transport_http_when_type_http():
    assert _classify_transport({"type": "http", "url": "https://x"}) == "http"
    print("PASS  test_classify_transport_http_when_type_http")


def test_classify_transport_sse_when_type_sse():
    assert _classify_transport({"type": "sse", "url": "https://x"}) == "sse"
    print("PASS  test_classify_transport_sse_when_type_sse")


def test_classify_transport_http_fallback_from_url():
    # URL present without explicit type → http
    assert _classify_transport({"url": "https://x"}) == "http"
    print("PASS  test_classify_transport_http_fallback_from_url")


def test_classify_transport_stdio_when_empty():
    assert _classify_transport({}) == "stdio"
    print("PASS  test_classify_transport_stdio_when_empty")


# ---------------------------------------------------------------------------
# _slugify_mcp_name
# ---------------------------------------------------------------------------

def test_slugify_normalizes_display_name():
    assert _slugify_mcp_name("claude.ai Linear") == "claude-ai-linear"
    assert _slugify_mcp_name("claude.ai Google Drive") == "claude-ai-google-drive"
    print("PASS  test_slugify_normalizes_display_name")


def test_slugify_handles_edge_cases():
    assert _slugify_mcp_name("") == "imported"
    assert _slugify_mcp_name("---") == "imported"
    assert _slugify_mcp_name("  UPPER  case  ") == "upper-case"
    print("PASS  test_slugify_handles_edge_cases")


# ---------------------------------------------------------------------------
# read_user_mcp_config
# ---------------------------------------------------------------------------

@_with_fake_home
def test_read_user_mcp_config_missing_returns_empty(home):
    # No ~/.claude.json, no ~/.claude/settings.json
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


# ---------------------------------------------------------------------------
# extract_imported_mcps
# ---------------------------------------------------------------------------

def test_extract_imported_mcps_empty_returns_nothing():
    mcps, wrapped = extract_imported_mcps({})
    assert mcps == []
    assert wrapped == 0
    print("PASS  test_extract_imported_mcps_empty_returns_nothing")


def test_extract_imported_mcps_stdio_passthrough():
    cfg = {"mcpServers": {
        "fs": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"], "env": {"K": "v"}},
    }}
    mcps, wrapped = extract_imported_mcps(cfg)
    assert wrapped == 0
    assert len(mcps) == 1
    assert mcps[0].name == "fs"
    assert mcps[0].transport == "stdio"
    assert mcps[0].block["command"] == "npx"
    assert mcps[0].block["args"] == ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    assert mcps[0].block["env"] == {"K": "v"}
    print("PASS  test_extract_imported_mcps_stdio_passthrough")


def test_extract_imported_mcps_http_wrapped_via_mcp_remote():
    cfg = {"mcpServers": {
        "notion": {"type": "http", "url": "https://mcp.notion.com/v1"},
    }}
    mcps, wrapped = extract_imported_mcps(cfg)
    assert wrapped == 1
    assert mcps[0].name == "notion"
    assert mcps[0].transport == "http"
    assert mcps[0].block["command"] == "npx"
    assert mcps[0].block["args"][0] == "-y"
    assert mcps[0].block["args"][1].startswith("mcp-remote@")
    assert mcps[0].block["args"][2] == "https://mcp.notion.com/v1"
    assert mcps[0].block["auth"] == "oauth"
    assert mcps[0].block["auth_url"] == "https://mcp.notion.com/v1"
    print("PASS  test_extract_imported_mcps_http_wrapped_via_mcp_remote")


def test_extract_imported_mcps_sse_wrapped_via_mcp_remote():
    cfg = {"mcpServers": {
        "svc": {"type": "sse", "url": "https://x.example/sse"},
    }}
    mcps, wrapped = extract_imported_mcps(cfg)
    assert wrapped == 1
    assert mcps[0].transport == "sse"
    assert mcps[0].block["auth"] == "oauth"
    print("PASS  test_extract_imported_mcps_sse_wrapped_via_mcp_remote")


def test_extract_imported_mcps_env_vars_captured():
    cfg = {"mcpServers": {
        "gh": {"command": "x", "env": {"T": "${GITHUB_TOKEN}", "H": "static", "N": "${NOTION_API_KEY}${FOO}"}},
    }}
    mcps, _ = extract_imported_mcps(cfg)
    assert set(mcps[0].env_vars_referenced) == {"GITHUB_TOKEN", "NOTION_API_KEY", "FOO"}
    print("PASS  test_extract_imported_mcps_env_vars_captured")


def test_extract_imported_mcps_skips_malformed_entries():
    cfg = {"mcpServers": {
        "string-not-dict": "oops",
        "empty-dict": {},             # no command, no url → skipped
        "ok": {"command": "x"},
    }}
    mcps, _ = extract_imported_mcps(cfg)
    names = {m.name for m in mcps}
    assert names == {"ok"}, names
    print("PASS  test_extract_imported_mcps_skips_malformed_entries")


def test_extract_imported_mcps_includes_project_scope_for_cwd():
    # `claude mcp add` defaults to project scope, writing entries under
    # ~/.claude.json#projects.<cwd>.mcpServers — not the top level. Importer
    # must walk both. Pin cwd so the test is independent of where it runs.
    cfg = {
        "mcpServers": {"user-scope-thing": {"command": "uss"}},
        "projects": {
            "/some/proj": {"mcpServers": {"proj-scope-thing": {"command": "pst"}}},
        },
    }
    mcps, _ = extract_imported_mcps(cfg, cwd=Path("/some/proj"))
    names = {m.name for m in mcps}
    assert names == {"user-scope-thing", "proj-scope-thing"}, names
    print("PASS  test_extract_imported_mcps_includes_project_scope_for_cwd")


def test_extract_imported_mcps_project_scope_wins_on_collision():
    cfg = {
        "mcpServers": {"shared": {"command": "user-version"}},
        "projects": {
            "/some/proj": {"mcpServers": {"shared": {"command": "proj-version"}}},
        },
    }
    mcps, _ = extract_imported_mcps(cfg, cwd=Path("/some/proj"))
    assert len(mcps) == 1
    assert mcps[0].block["command"] == "proj-version", mcps[0].block["command"]
    print("PASS  test_extract_imported_mcps_project_scope_wins_on_collision")


# ---------------------------------------------------------------------------
# discover_hosted_mcps_via_cli
# ---------------------------------------------------------------------------

def test_discover_hosted_mcps_parses_claude_mcp_list():
    stdout = (
        "Checking MCP server health…\n"
        "\n"
        "claude.ai Google Calendar: https://calendarmcp.googleapis.com/mcp/v1 - ! Needs authentication\n"
        "claude.ai Gmail: https://gmailmcp.googleapis.com/mcp/v1 - ! Needs authentication\n"
        "claude.ai Linear: https://mcp.linear.app/sse - ✓ Connected\n"
    )
    with patch("_1_800_operator.pipeline.claude_code_import.subprocess.run",
               return_value=_run_ok(stdout)):
        mcps = discover_hosted_mcps_via_cli()
    names = [m.name for m in mcps]
    assert names == ["claude-ai-google-calendar", "claude-ai-gmail", "claude-ai-linear"], names
    # SSE detection for /sse URLs
    linear = [m for m in mcps if m.name == "claude-ai-linear"][0]
    assert linear.transport == "sse", linear.transport
    # All get wrapped as mcp-remote stdio
    assert linear.block["command"] == "npx"
    assert linear.block["auth"] == "oauth"
    print("PASS  test_discover_hosted_mcps_parses_claude_mcp_list")


def test_discover_hosted_mcps_returncode_nonzero_returns_empty():
    with patch("_1_800_operator.pipeline.claude_code_import.subprocess.run",
               return_value=_run_ok("", returncode=1)):
        assert discover_hosted_mcps_via_cli() == []
    print("PASS  test_discover_hosted_mcps_returncode_nonzero_returns_empty")


def test_discover_hosted_mcps_file_not_found_returns_empty():
    with patch("_1_800_operator.pipeline.claude_code_import.subprocess.run",
               side_effect=FileNotFoundError):
        assert discover_hosted_mcps_via_cli() == []
    print("PASS  test_discover_hosted_mcps_file_not_found_returns_empty")


def test_discover_hosted_mcps_timeout_returns_empty():
    with patch("_1_800_operator.pipeline.claude_code_import.subprocess.run",
               side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=10)):
        assert discover_hosted_mcps_via_cli() == []
    print("PASS  test_discover_hosted_mcps_timeout_returns_empty")


def test_claude_mcp_list_cache_caches_failures():
    """A timed-out / missing CLI must be cached as a sentinel so repeat
    callers within the same boot don't each pay another 10s timeout.

    Boot makes three callers (discover_hosted, discover_health, runtime
    view in config.py); without sentinel caching, a broken `claude` CLI
    costs 30s instead of 10s.
    """
    import _1_800_operator.pipeline.claude_code_import as mod
    mod._CLAUDE_MCP_LIST_CACHE = None
    call_count = {"n": 0}

    def fake_run(*args, **kwargs):
        call_count["n"] += 1
        raise subprocess.TimeoutExpired(cmd="claude", timeout=10)

    with patch("_1_800_operator.pipeline.claude_code_import.subprocess.run",
               side_effect=fake_run):
        # Three boot-time callers all degrade to empty results...
        assert mod.discover_hosted_mcps_via_cli() == []
        assert mod.discover_mcp_health() == []
        assert mod.discover_hosted_mcps_via_cli() == []

    # ...but only ONE shell-out happens.
    assert call_count["n"] == 1, \
        f"Expected exactly 1 subprocess.run after caching, got {call_count['n']}"
    # And the cached sentinel reports failure shape callers already handle.
    cached = mod._CLAUDE_MCP_LIST_CACHE
    assert cached is not None
    assert cached.returncode != 0
    mod._CLAUDE_MCP_LIST_CACHE = None  # cleanup so other tests stay fresh
    print("PASS  test_claude_mcp_list_cache_caches_failures")


def test_discover_hosted_mcps_parses_http_annotation():
    # claude-code annotates HTTP-not-SSE remote MCPs with `(HTTP)` between
    # the URL and the ` - status` segment. Earlier regex didn't tolerate
    # that and silently dropped Sentry-style entries.
    stdout = (
        "sentry: https://mcp.sentry.dev/mcp (HTTP) - ✓ Connected\n"
        "claude.ai Linear: https://mcp.linear.app/sse - ✓ Connected\n"
    )
    with patch("_1_800_operator.pipeline.claude_code_import.subprocess.run",
               return_value=_run_ok(stdout)):
        mcps = discover_hosted_mcps_via_cli()
    names = [m.name for m in mcps]
    assert names == ["sentry", "claude-ai-linear"], names
    sentry = [m for m in mcps if m.name == "sentry"][0]
    assert sentry.transport == "http", sentry.transport
    print("PASS  test_discover_hosted_mcps_parses_http_annotation")


def test_discover_hosted_mcps_skips_malformed_lines():
    stdout = (
        "garbage\n"
        "ok-entry: https://x/sse - Connected\n"
        "another garbage line without the expected shape\n"
    )
    with patch("_1_800_operator.pipeline.claude_code_import.subprocess.run",
               return_value=_run_ok(stdout)):
        mcps = discover_hosted_mcps_via_cli()
    assert [m.name for m in mcps] == ["ok-entry"]
    print("PASS  test_discover_hosted_mcps_skips_malformed_lines")


# ---------------------------------------------------------------------------
# discover_mcp_health
# ---------------------------------------------------------------------------

def test_discover_mcp_health_classifies_status():
    stdout = (
        "claude.ai Linear: https://mcp.linear.app/sse - ✓ Connected\n"
        "claude.ai Gmail: https://gmailmcp.googleapis.com/mcp/v1 - ! Needs authentication\n"
        "sentry: https://mcp.sentry.dev/mcp (HTTP) - ✗ Failed to connect\n"
    )
    with patch("_1_800_operator.pipeline.claude_code_import.subprocess.run",
               return_value=_run_ok(stdout)):
        records = discover_mcp_health()
    assert len(records) == 3
    by_name = {name: (status, healthy) for name, _u, status, healthy in records}
    assert by_name["claude.ai Linear"][1] is True
    assert by_name["claude.ai Gmail"][1] is False
    assert by_name["claude.ai Gmail"][0] == "! Needs authentication"
    assert by_name["sentry"][1] is False
    assert by_name["sentry"][0] == "✗ Failed to connect"
    print("PASS  test_discover_mcp_health_classifies_status")


def test_discover_mcp_health_returns_empty_on_cli_failure():
    with patch("_1_800_operator.pipeline.claude_code_import.subprocess.run",
               side_effect=FileNotFoundError):
        assert discover_mcp_health() == []
    with patch("_1_800_operator.pipeline.claude_code_import.subprocess.run",
               return_value=_run_ok("", returncode=1)):
        assert discover_mcp_health() == []
    print("PASS  test_discover_mcp_health_returns_empty_on_cli_failure")


# ---------------------------------------------------------------------------
# discover_all_mcps
# ---------------------------------------------------------------------------

@_with_fake_home
def test_discover_all_merges_both_sources(home):
    # Seed both: json-level stdio + CLI-level hosted
    json_cfg = {"mcpServers": {"local-thing": {"command": "npx", "args": ["x"]}}}
    (home / ".claude.json").write_text(json.dumps(json_cfg))

    cli_stdout = "claude.ai Linear: https://mcp.linear.app/sse - ✓ Connected\n"
    with patch("_1_800_operator.pipeline.claude_code_import.subprocess.run",
               return_value=_run_ok(cli_stdout)):
        mcps, wrapped = discover_all_mcps()

    names = {m.name for m in mcps}
    assert names == {"local-thing", "claude-ai-linear"}, names
    # 1 from CLI wrap, 0 from json (stdio)
    assert wrapped == 1, wrapped
    print("PASS  test_discover_all_merges_both_sources")


@_with_fake_home
def test_discover_all_picks_up_project_mcp_json(home):
    """`claude mcp add -s project ...` writes to <cwd>/.mcp.json. Operator
    should discover those too — otherwise project-shared MCPs would be
    invisible to operator's banner / overlay / disable-toggle even though
    the inner CLI loads them."""
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {}}))
    with tempfile.TemporaryDirectory() as cwd_tmp:
        cwd = Path(cwd_tmp)
        (cwd / ".mcp.json").write_text(json.dumps({
            "mcpServers": {
                "shared-stdio": {"type": "stdio", "command": "echo", "args": ["hi"]},
                "shared-http": {"type": "http", "url": "https://example.com/mcp"},
            }
        }))
        # No CLI hosted MCPs
        with (
            patch("_1_800_operator.pipeline.claude_code_import.Path.cwd", return_value=cwd),
            patch("_1_800_operator.pipeline.claude_code_import.subprocess.run",
                  return_value=_run_ok("")),
        ):
            mcps, wrapped = discover_all_mcps()

    names = {m.name for m in mcps}
    assert names == {"shared-stdio", "shared-http"}, names
    # http source got wrapped via mcp-remote
    assert wrapped == 1, wrapped
    print("PASS  test_discover_all_picks_up_project_mcp_json")


@_with_fake_home
def test_discover_all_dedups_user_scope_over_project_mcp_json(home):
    """If the same name appears in user-scope and .mcp.json, user-scope
    wins (matches CLI's local > project > user precedence on a per-name
    basis, with first-seen winning in our merge order)."""
    (home / ".claude.json").write_text(json.dumps({
        "mcpServers": {"shared": {"command": "user-cmd", "args": []}}
    }))
    with tempfile.TemporaryDirectory() as cwd_tmp:
        cwd = Path(cwd_tmp)
        (cwd / ".mcp.json").write_text(json.dumps({
            "mcpServers": {"shared": {"command": "project-cmd", "args": []}}
        }))
        with (
            patch("_1_800_operator.pipeline.claude_code_import.Path.cwd", return_value=cwd),
            patch("_1_800_operator.pipeline.claude_code_import.subprocess.run",
                  return_value=_run_ok("")),
        ):
            mcps, _ = discover_all_mcps()
    assert len(mcps) == 1, [m.name for m in mcps]
    assert mcps[0].name == "shared"
    assert mcps[0].block["command"] == "user-cmd", mcps[0].block["command"]
    print("PASS  test_discover_all_dedups_user_scope_over_project_mcp_json")


# ---------------------------------------------------------------------------
# read_user_claude_md
# ---------------------------------------------------------------------------

@_with_fake_home
def test_read_user_claude_md_missing_returns_none(home):
    # Pin cwd to a clean dir so the project-scope walk doesn't pick up
    # whatever CLAUDE.md happens to live in the test runner's cwd.
    assert read_user_claude_md(cwd=home) is None
    print("PASS  test_read_user_claude_md_missing_returns_none")


@_with_fake_home
def test_read_user_claude_md_present_returns_contents(home):
    (home / ".claude").mkdir()
    (home / ".claude" / "CLAUDE.md").write_text("# hi\n")
    # Pin cwd to a clean dir so we test the user-scope-only path.
    clean_cwd = home / "empty_proj"
    clean_cwd.mkdir()
    assert read_user_claude_md(cwd=clean_cwd) == "# hi\n"
    print("PASS  test_read_user_claude_md_present_returns_contents")


@_with_fake_home
def test_read_user_claude_md_walks_project_scope_too(home):
    # User scope absent. Project root + project .claude/ both present.
    # Multi-source result should include both, each with a section header.
    proj = home / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / "CLAUDE.md").write_text("PROJECT ROOT RULES\n")
    (proj / ".claude" / "CLAUDE.md").write_text("PROJECT CLAUDE DIR RULES\n")
    out = read_user_claude_md(cwd=proj)
    assert out is not None
    assert "PROJECT ROOT RULES" in out
    assert "PROJECT CLAUDE DIR RULES" in out
    assert "# CLAUDE.md — ./CLAUDE.md" in out
    assert "# CLAUDE.md — ./.claude/CLAUDE.md" in out
    print("PASS  test_read_user_claude_md_walks_project_scope_too")


@_with_fake_home
def test_normalize_path_for_storage_prefers_home_then_cwd_then_absolute(home):
    from _1_800_operator.pipeline.claude_code_import import normalize_path_for_storage
    cwd = home / "proj"
    cwd.mkdir()
    # Under HOME → ~/...
    assert normalize_path_for_storage(home / ".claude" / "x", cwd=cwd) == "~/.claude/x"
    # Outside home, under cwd → ./...
    import tempfile as _tf
    extern = Path(_tf.mkdtemp(prefix="extern_"))
    try:
        cwd2 = extern / "p"
        cwd2.mkdir()
        (cwd2 / "CLAUDE.md").touch()
        assert normalize_path_for_storage(cwd2 / "CLAUDE.md", cwd=cwd2) == "./CLAUDE.md"
        # Outside both → absolute
        abs_path = extern / "totally_unrelated.md"
        abs_path.touch()
        assert normalize_path_for_storage(abs_path, cwd=cwd) == str(abs_path.resolve())
    finally:
        import shutil as _sh
        _sh.rmtree(extern, ignore_errors=True)
    print("PASS  test_normalize_path_for_storage_prefers_home_then_cwd_then_absolute")


@_with_fake_home
def test_discover_claude_md_sources_returns_labels_in_walk_order(home):
    # All three sources present. Caller (e.g. wizard) needs the labels
    # to render accurate provenance — without this the prompt hardcodes
    # ~/.claude/CLAUDE.md regardless of which scopes actually exist.
    from _1_800_operator.pipeline.claude_code_import import discover_claude_md_sources
    (home / ".claude").mkdir(exist_ok=True)
    (home / ".claude" / "CLAUDE.md").write_text("USER\n")
    proj = home / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / "CLAUDE.md").write_text("PROJ ROOT\n")
    (proj / ".claude" / "CLAUDE.md").write_text("PROJ CLAUDE\n")
    sources = discover_claude_md_sources(cwd=proj)
    labels = [label for label, _ in sources]
    assert labels == ["~/.claude/CLAUDE.md", "./CLAUDE.md", "./.claude/CLAUDE.md"]
    contents = [content for _, content in sources]
    assert contents == ["USER\n", "PROJ ROOT\n", "PROJ CLAUDE\n"]
    print("PASS  test_discover_claude_md_sources_returns_labels_in_walk_order")


@_with_fake_home
def test_discover_claude_md_sources_empty_when_nothing_present(home):
    from _1_800_operator.pipeline.claude_code_import import discover_claude_md_sources
    proj = home / "empty"
    proj.mkdir()
    assert discover_claude_md_sources(cwd=proj) == []
    print("PASS  test_discover_claude_md_sources_empty_when_nothing_present")


@_with_fake_home
def test_read_user_claude_md_single_project_source_no_header(home):
    # Only project-scope present (no user CLAUDE.md). Single-source rule:
    # return bare content, no section header.
    proj = home / "proj"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("ONLY ONE SOURCE\n")
    assert read_user_claude_md(cwd=proj) == "ONLY ONE SOURCE\n"
    print("PASS  test_read_user_claude_md_single_project_source_no_header")


# ---------------------------------------------------------------------------
# append_env_placeholders
# ---------------------------------------------------------------------------

def test_append_env_placeholders_creates_file_if_missing():
    with tempfile.TemporaryDirectory() as tmp:
        env_file = Path(tmp) / "nested" / ".env"
        added = append_env_placeholders(["FOO", "BAR"], env_file)
        assert added == ["BAR", "FOO"]
        content = env_file.read_text()
        assert "# BAR=" in content and "# FOO=" in content
        assert "# Added by operator" in content
    print("PASS  test_append_env_placeholders_creates_file_if_missing")


def test_append_env_placeholders_idempotent_when_var_set():
    with tempfile.TemporaryDirectory() as tmp:
        env_file = Path(tmp) / ".env"
        env_file.write_text("FOO=already_set\n")
        added = append_env_placeholders(["FOO", "NEW"], env_file)
        assert added == ["NEW"]
        content = env_file.read_text()
        assert content.count("# NEW=") == 1
        # Existing plain-set FOO untouched
        assert "FOO=already_set" in content
    print("PASS  test_append_env_placeholders_idempotent_when_var_set")


def test_append_env_placeholders_idempotent_when_placeheld():
    with tempfile.TemporaryDirectory() as tmp:
        env_file = Path(tmp) / ".env"
        env_file.write_text("# FOO=\n")
        added = append_env_placeholders(["FOO"], env_file)
        assert added == []
        # File untouched
        assert env_file.read_text() == "# FOO=\n"
    print("PASS  test_append_env_placeholders_idempotent_when_placeheld")


def test_append_env_placeholders_empty_input_is_noop():
    with tempfile.TemporaryDirectory() as tmp:
        env_file = Path(tmp) / ".env"
        env_file.write_text("X=y\n")
        added = append_env_placeholders([], env_file)
        assert added == []
        assert env_file.read_text() == "X=y\n"
    print("PASS  test_append_env_placeholders_empty_input_is_noop")


def test_append_env_placeholders_fixes_missing_trailing_newline():
    with tempfile.TemporaryDirectory() as tmp:
        env_file = Path(tmp) / ".env"
        env_file.write_text("OLD=x")  # no trailing \n
        added = append_env_placeholders(["NEW"], env_file)
        assert added == ["NEW"]
        content = env_file.read_text()
        # Should not have glued the header onto OLD=x; leading \n added.
        assert content.startswith("OLD=x\n"), repr(content[:40])
        assert "# NEW=" in content
    print("PASS  test_append_env_placeholders_fixes_missing_trailing_newline")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_classify_transport_stdio_when_command_present,
        test_classify_transport_http_when_type_http,
        test_classify_transport_sse_when_type_sse,
        test_classify_transport_http_fallback_from_url,
        test_classify_transport_stdio_when_empty,
        test_slugify_normalizes_display_name,
        test_slugify_handles_edge_cases,
        test_read_user_mcp_config_missing_returns_empty,
        test_read_user_mcp_config_malformed_returns_empty,
        test_read_user_mcp_config_reads_top_level_json,
        test_read_user_mcp_config_fallback_to_settings_json,
        test_extract_imported_mcps_empty_returns_nothing,
        test_extract_imported_mcps_stdio_passthrough,
        test_extract_imported_mcps_http_wrapped_via_mcp_remote,
        test_extract_imported_mcps_sse_wrapped_via_mcp_remote,
        test_extract_imported_mcps_env_vars_captured,
        test_extract_imported_mcps_skips_malformed_entries,
        test_extract_imported_mcps_includes_project_scope_for_cwd,
        test_extract_imported_mcps_project_scope_wins_on_collision,
        test_discover_hosted_mcps_parses_claude_mcp_list,
        test_discover_hosted_mcps_parses_http_annotation,
        test_discover_hosted_mcps_returncode_nonzero_returns_empty,
        test_discover_hosted_mcps_file_not_found_returns_empty,
        test_discover_hosted_mcps_timeout_returns_empty,
        test_claude_mcp_list_cache_caches_failures,
        test_discover_hosted_mcps_skips_malformed_lines,
        test_discover_mcp_health_classifies_status,
        test_discover_mcp_health_returns_empty_on_cli_failure,
        test_discover_all_merges_both_sources,
        test_discover_all_picks_up_project_mcp_json,
        test_discover_all_dedups_user_scope_over_project_mcp_json,
        test_read_user_claude_md_missing_returns_none,
        test_read_user_claude_md_present_returns_contents,
        test_read_user_claude_md_walks_project_scope_too,
        test_normalize_path_for_storage_prefers_home_then_cwd_then_absolute,
        test_discover_claude_md_sources_returns_labels_in_walk_order,
        test_discover_claude_md_sources_empty_when_nothing_present,
        test_read_user_claude_md_single_project_source_no_header,
        test_append_env_placeholders_creates_file_if_missing,
        test_append_env_placeholders_idempotent_when_var_set,
        test_append_env_placeholders_idempotent_when_placeheld,
        test_append_env_placeholders_empty_input_is_noop,
        test_append_env_placeholders_fixes_missing_trailing_newline,
    ]
    failed = 0
    # Bust the per-process `claude mcp list` cache before every test so
    # tests that mock subprocess.run see a fresh shell-out instead of
    # whatever a prior test (or the real CLI on this machine) cached.
    import _1_800_operator.pipeline.claude_code_import as _cci_mod
    for t in tests:
        _cci_mod._CLAUDE_MCP_LIST_CACHE = None
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
