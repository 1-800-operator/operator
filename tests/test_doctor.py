"""
Tests for `operator doctor` checks.

Covers _check_cwd_trusted — the workspace-trust check, the inform-only
half of operator's trust-dialog handling (operator detects + warns, never
writes the trust state itself). Run:

    source venv/bin/activate
    python tests/test_doctor.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import contextlib
import io
import time

from _1_800_operator import config
from _1_800_operator.pipeline.doctor import (
    _check_cwd_trusted,
    _check_meeting_record_mcp,
    _render_last_failure,
)


# ── helpers for the meeting-record MCP check ────────────────────────────────

_OK_MCP_LINE = (
    "operator-meeting-record: /Users/jojo/.local/share/uv/tools/1-800-operator"
    "/bin/python -m _1_800_operator.mcp_servers.record_server - ✓ Connected\n"
)


def _settings(tmp: Path, allow_entries: list[str] | None) -> Path:
    """Write a minimal ~/.claude/settings.json; allow_entries=None omits the key."""
    p = tmp / "settings.json"
    if allow_entries is None:
        p.write_text("{}", encoding="utf-8")
    else:
        p.write_text(
            json.dumps({"permissions": {"allow": allow_entries}}),
            encoding="utf-8",
        )
    return p


def _runner(rc: int, out: str):
    return lambda: (rc, out)


def _registry(tmp, projects):
    """Write a minimal ~/.claude.json-shaped file; return its path."""
    p = Path(tmp) / ".claude.json"
    p.write_text(json.dumps({"projects": projects}), encoding="utf-8")
    return p


def test_trusted_cwd():
    """projects[<cwd>].hasTrustDialogAccepted is True → ok, no warning."""
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp) / "proj"
        cwd.mkdir()
        reg = _registry(tmp, {str(cwd): {"hasTrustDialogAccepted": True}})
        r = _check_cwd_trusted(registry_path=reg, cwd=cwd)
        assert r.ok, r
        assert "trusted" in r.detail, r.detail
    print("  trusted cwd → ok OK")


def test_untrusted_cwd_entry_exists():
    """Entry exists but hasTrustDialogAccepted is false → optional warning."""
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp) / "proj"
        cwd.mkdir()
        reg = _registry(tmp, {str(cwd): {"hasTrustDialogAccepted": False}})
        r = _check_cwd_trusted(registry_path=reg, cwd=cwd)
        assert not r.ok and r.optional, r
        assert "not trusted" in r.detail, r.detail
        assert "trust prompt" in r.fix, r.fix
    print("  untrusted cwd (entry exists) → optional warning OK")


def test_cwd_not_in_registry():
    """cwd absent from projects entirely → optional warning (never opened
    claude here, so it's untrusted)."""
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp) / "proj"
        cwd.mkdir()
        reg = _registry(tmp, {"/some/other/dir": {"hasTrustDialogAccepted": True}})
        r = _check_cwd_trusted(registry_path=reg, cwd=cwd)
        assert not r.ok and r.optional, r
    print("  cwd not in registry → optional warning OK")


def test_registry_missing_fails_open():
    """A missing ~/.claude.json skips the check (ok=True) — fail open
    rather than hard-block on our own check being unable to read."""
    with tempfile.TemporaryDirectory() as tmp:
        r = _check_cwd_trusted(registry_path=Path(tmp) / "nope.json", cwd=Path(tmp))
        assert r.ok and r.optional, r
        assert "skipped" in r.detail, r.detail
    print("  missing registry → fails open (skipped) OK")


def test_registry_garbage_fails_open():
    """Unparseable JSON in the registry also fails open."""
    with tempfile.TemporaryDirectory() as tmp:
        reg = Path(tmp) / ".claude.json"
        reg.write_text("{not json", encoding="utf-8")
        r = _check_cwd_trusted(registry_path=reg, cwd=Path(tmp))
        assert r.ok and r.optional, r
    print("  garbage registry → fails open OK")


def test_resolved_path_match():
    """cwd is matched both as-is and resolved — covers macOS /tmp vs
    /private/tmp and other symlinked paths. The registry here is keyed
    ONLY under the resolved path; on macOS temp dirs cwd != resolved, so
    this genuinely exercises the resolve() fallback."""
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp) / "proj"
        cwd.mkdir()
        resolved = str(cwd.resolve())
        reg = _registry(tmp, {resolved: {"hasTrustDialogAccepted": True}})
        r = _check_cwd_trusted(registry_path=reg, cwd=cwd)
        assert r.ok, f"resolved-path match failed (cwd={cwd}, resolved={resolved}): {r}"
    print("  resolved-path match OK")


def _render_with_failure_file(tmp_path: Path, payload: dict | str) -> str:
    """Point config.LAST_FAILURE_PATH at a temp file holding `payload`,
    capture _render_last_failure()'s stdout, restore the path."""
    p = tmp_path / "last_failure.json"
    if isinstance(payload, str):
        p.write_text(payload, encoding="utf-8")
    else:
        p.write_text(json.dumps(payload), encoding="utf-8")
    saved = config.LAST_FAILURE_PATH
    config.LAST_FAILURE_PATH = str(p)
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _render_last_failure()
        return buf.getvalue()
    finally:
        config.LAST_FAILURE_PATH = saved


def test_render_last_failure_absent_is_silent():
    """No file → nothing printed (a clean run has nothing to say)."""
    with tempfile.TemporaryDirectory() as tmp:
        saved = config.LAST_FAILURE_PATH
        config.LAST_FAILURE_PATH = str(Path(tmp) / "missing.json")
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                _render_last_failure()
            assert buf.getvalue() == "", f"expected silence, got: {buf.getvalue()!r}"
        finally:
            config.LAST_FAILURE_PATH = saved
    print("  no-file → silent OK")


def test_render_last_failure_dumps_structured_record():
    """A populated file is dumped verbatim — scalars on their lines, the
    PTY tail and log tail printed below their labels. No classification
    or rewriting — the model reading doctor's output is the translator."""
    with tempfile.TemporaryDirectory() as tmp:
        payload = {
            "ts": time.time() - 90,
            "meeting_url": "https://meet.google.com/sim-abcd-efg",
            "meeting_slug": "sim-abcd-efg",
            "exception_class": "ClaudeCLIProtocolError",
            "message": "briefing (turn 0) produced no reply before the 180s boot ceiling — inner-claude is wedged.",
            "phase": "boot",
            "pty_tail": "Starting Claude Code...\nLoading session 24f38462\n",
            "operator_log_tail": "2026-05-14 21:55:01 INFO ClaudeCLI spawning interactive claude",
        }
        out = _render_with_failure_file(Path(tmp), payload)
        assert "Last meeting failure" in out, out
        # Header / scalars surface verbatim from the record.
        assert "ClaudeCLIProtocolError" in out
        assert "phase            boot" in out
        assert "https://meet.google.com/sim-abcd-efg" in out
        assert "1m ago" in out, "age should render in short form"
        # Multi-line + long fields land below their label.
        assert "message:\n" in out
        assert "boot ceiling" in out
        assert "pty_tail:\n" in out
        assert "Loading session 24f38462" in out
        assert "operator_log_tail:\n" in out
        assert "ClaudeCLI spawning interactive claude" in out
        # No classifier output sneaks in — doctor never translates.
        for forbidden in (
            "usually means", "try re-running", "looks like",
            "likely cause", "your claude is",
        ):
            assert forbidden.lower() not in out.lower(), (
                f"doctor must not classify ({forbidden!r} found)"
            )
    print("  populated file → structured dump, no classification OK")


def test_render_last_failure_garbage_is_silent():
    """Unparseable JSON in the file → render silently skips, doesn't raise."""
    with tempfile.TemporaryDirectory() as tmp:
        out = _render_with_failure_file(Path(tmp), "{ not json")
        assert out == "", f"garbage → silent, got: {out!r}"
    print("  garbage file → silent OK")


def test_meeting_record_mcp_happy_path():
    """Registered + connected + allowlisted → ok."""
    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings(Path(tmp), ["mcp__operator-meeting-record__*"])
        r = _check_meeting_record_mcp(
            settings_path=settings,
            mcp_list_runner=_runner(0, _OK_MCP_LINE),
        )
        assert r.ok, r
        assert "allowlisted" in r.detail, r.detail
    print("  registered + allowlisted → ok OK")


def test_meeting_record_mcp_not_registered():
    """`claude mcp list` doesn't mention our server → fail with dual-target fix."""
    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings(Path(tmp), ["mcp__operator-meeting-record__*"])
        other_servers_output = (
            "github: /usr/local/bin/github-mcp-server stdio - ✓ Connected\n"
            "claude.ai Gmail: https://gmailmcp.googleapis.com/mcp/v1 - ✓ Connected\n"
        )
        r = _check_meeting_record_mcp(
            settings_path=settings,
            mcp_list_runner=_runner(0, other_servers_output),
        )
        assert not r.ok, r
        assert "not registered" in r.detail, r.detail
        # Dual-target fix: outer-claude path AND copy-pasteable installer URL.
        assert "ask Claude to fix this" in r.fix, r.fix
        assert "1-800-operator.com/install" in r.fix, r.fix
    print("  not in mcp list → fail OK")


def test_meeting_record_mcp_registered_but_disconnected():
    """Registered but health-check failed (e.g. ✗ Failed to connect) → fail with the CLI's status verbatim."""
    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings(Path(tmp), ["mcp__operator-meeting-record__*"])
        broken_line = (
            "operator-meeting-record: /old/path/python -m _1_800_operator.mcp_servers.record_server "
            "- ✗ Failed to connect\n"
        )
        r = _check_meeting_record_mcp(
            settings_path=settings,
            mcp_list_runner=_runner(0, broken_line),
        )
        assert not r.ok, r
        assert "not connected" in r.detail, r.detail
        assert "Failed to connect" in r.detail, r.detail
    print("  registered but failed health → fail OK")


def test_meeting_record_mcp_allowlist_missing():
    """Registered + healthy but allowlist entry absent → fail (desktop-app silent-fail risk)."""
    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings(Path(tmp), ["Bash(operator:*)"])  # other entries present, ours absent
        r = _check_meeting_record_mcp(
            settings_path=settings,
            mcp_list_runner=_runner(0, _OK_MCP_LINE),
        )
        assert not r.ok, r
        assert "missing from" in r.detail, r.detail
        assert "1-800-operator.com/install" in r.fix, r.fix
    print("  allowlist missing → fail OK")


def test_meeting_record_mcp_settings_file_missing():
    """settings.json absent → fail (allowlist verification can't pass)."""
    with tempfile.TemporaryDirectory() as tmp:
        absent = Path(tmp) / "nope.json"
        r = _check_meeting_record_mcp(
            settings_path=absent,
            mcp_list_runner=_runner(0, _OK_MCP_LINE),
        )
        assert not r.ok, r
        assert "missing" in r.detail, r.detail
    print("  settings.json absent → fail OK")


def test_meeting_record_mcp_settings_garbage_fails_open():
    """Unparseable settings.json → optional warning, doesn't block ok-exit."""
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "settings.json"
        p.write_text("{not json", encoding="utf-8")
        r = _check_meeting_record_mcp(
            settings_path=p,
            mcp_list_runner=_runner(0, _OK_MCP_LINE),
        )
        assert r.ok and r.optional, r
        assert "couldn't parse" in r.detail, r.detail
    print("  garbage settings.json → optional warning OK")


def test_meeting_record_mcp_list_command_fails():
    """`claude mcp list` non-zero or timed out → fail with the rc surfaced."""
    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings(Path(tmp), ["mcp__operator-meeting-record__*"])
        r = _check_meeting_record_mcp(
            settings_path=settings,
            mcp_list_runner=_runner(124, "timed out"),
        )
        assert not r.ok, r
        assert "rc=124" in r.detail, r.detail
    print("  mcp list rc!=0 → fail OK")


def main():
    tests = [
        test_trusted_cwd,
        test_untrusted_cwd_entry_exists,
        test_cwd_not_in_registry,
        test_registry_missing_fails_open,
        test_registry_garbage_fails_open,
        test_resolved_path_match,
        test_render_last_failure_absent_is_silent,
        test_render_last_failure_dumps_structured_record,
        test_render_last_failure_garbage_is_silent,
        test_meeting_record_mcp_happy_path,
        test_meeting_record_mcp_not_registered,
        test_meeting_record_mcp_registered_but_disconnected,
        test_meeting_record_mcp_allowlist_missing,
        test_meeting_record_mcp_settings_file_missing,
        test_meeting_record_mcp_settings_garbage_fails_open,
        test_meeting_record_mcp_list_command_fails,
    ]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} doctor tests passed.")


if __name__ == "__main__":
    main()
