"""
Tests for the glob-pattern permission matcher in PermissionChatHandler.
Run: python tests/test_permission_matcher.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
os.environ.setdefault("OPERATOR_BOT", "claude")

from unittest.mock import MagicMock

from _1_800_operator.pipeline.permission_chat_handler import (
    _matches_any,
    PermissionChatHandler,
)


def test_matches_any_exact():
    assert _matches_any("Read", ["Read"]) is True
    assert _matches_any("Read", ["read"]) is False  # case-sensitive
    assert _matches_any("Read", ["Bash", "Write"]) is False
    assert _matches_any("Read", []) is False
    assert _matches_any("Read", None) is False


def test_matches_any_wildcard_suffix():
    pats = ["mcp__sentry__get_*"]
    assert _matches_any("mcp__sentry__get_sentry_resource", pats) is True
    assert _matches_any("mcp__sentry__get_event_attachment",  pats) is True
    assert _matches_any("mcp__sentry__search_issues",          pats) is False
    assert _matches_any("mcp__linear__get_issue",              pats) is False


def test_matches_any_wildcard_prefix_full_server():
    """`mcp__sentry__*` covers every tool from the Sentry server."""
    pats = ["mcp__sentry__*"]
    assert _matches_any("mcp__sentry__get_resource", pats) is True
    assert _matches_any("mcp__sentry__whoami",        pats) is True
    assert _matches_any("mcp__sentry__save_anything", pats) is True
    assert _matches_any("mcp__linear__get_issue",     pats) is False


def test_matches_any_mixed_list():
    """Bare names + globs in the same list both match."""
    pats = ["Read", "Grep", "mcp__sentry__get_*"]
    assert _matches_any("Read",                              pats) is True
    assert _matches_any("Grep",                              pats) is True
    assert _matches_any("mcp__sentry__get_sentry_resource",  pats) is True
    assert _matches_any("Bash",                              pats) is False


def test_matches_any_question_mark_and_class():
    assert _matches_any("Read", ["Rea?"]) is True
    assert _matches_any("Read", ["[RG]ead"]) is True
    assert _matches_any("Read", ["[XYZ]ead"]) is False


def test_matches_any_empty_pattern_skipped():
    assert _matches_any("Read", ["", "Read"]) is True
    assert _matches_any("Read", [None, "Read"]) is True
    assert _matches_any("Read", ["", None]) is False


def _handler(auto_approve, always_ask):
    """Build a handler with mocked connector + runner so we can test __call__.

    Clears config.DISABLED_MCP_SERVERS for the test — under pm/engineer/designer
    the bundled config disables several MCPs by default, and the handler's
    disabled-server guard runs *before* the auto_approve / always_ask match,
    short-circuiting any test whose tool name happens to match a disabled
    server (e.g. mcp__sentry__*).
    """
    from _1_800_operator import config
    config.DISABLED_MCP_SERVERS = {}
    runner = MagicMock()
    runner._send = MagicMock()
    runner._seen_ids = set()
    runner._own_messages = set()
    return PermissionChatHandler(
        connector=MagicMock(),
        runner=runner,
        auto_approve=auto_approve,
        always_ask=always_ask,
    )


def test_call_auto_approve_silent():
    h = _handler(auto_approve=["Read", "Grep"], always_ask=["Bash"])
    res = h("Read", {"file_path": "/tmp/x"})
    assert res["permissionDecision"] == "allow"
    assert "auto-approved" in res["permissionDecisionReason"]


def test_call_glob_auto_approve():
    h = _handler(
        auto_approve=["Read", "mcp__sentry__get_*"],
        always_ask=["Bash"],
    )
    res = h("mcp__sentry__get_sentry_resource", {"url": "https://..."})
    assert res["permissionDecision"] == "allow"


def test_call_always_ask_wins_over_auto_approve():
    """Specific deny in always_ask beats broad allow in auto_approve."""
    h = _handler(
        auto_approve=["mcp__sentry__*"],                    # broad allow
        always_ask=["mcp__sentry__analyze_issue_with_seer"],  # specific deny
    )
    # Stub _round_trip so we don't actually try to round-trip through chat;
    # we only need to confirm the always_ask branch fired.
    h._round_trip = MagicMock(return_value={"permissionDecision": "deny"})

    res = h("mcp__sentry__analyze_issue_with_seer", {})
    assert h._round_trip.called, "always_ask match should trigger chat round-trip"
    assert res["permissionDecision"] == "deny"

    # Sanity: a non-overlapping Sentry tool still hits the broad allow.
    res2 = h("mcp__sentry__get_resource", {})
    assert res2["permissionDecision"] == "allow"


def test_call_unknown_tool_falls_through_to_ask():
    """Tool on neither list goes through chat (safe-by-default)."""
    h = _handler(auto_approve=["Read"], always_ask=["Bash"])
    h._round_trip = MagicMock(return_value={"permissionDecision": "deny"})
    h("BrandNewToolNobodyKnows", {})
    assert h._round_trip.called


def test_empty_lists_default_to_ask():
    h = _handler(auto_approve=[], always_ask=[])
    h._round_trip = MagicMock(return_value={"permissionDecision": "deny"})
    h("Read", {})
    assert h._round_trip.called


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as e:
            print(f"  ✗ {fn.__name__}: {e}")
            sys.exit(1)
    print(f"\n{len(fns)} tests passed.")
