"""
Phase 15.7.1 — structured MCP error classification.

Covers the four new code paths the foundation introduces:

  1. config._resolve_env_vars returns (resolved, missing_vars) and flags
     unfilled ${VAR} references while leaving literal values alone.
  2. _classify_startup_failure returns kind="missing_creds" with the var
     names when srv_config["missing_vars"] is non-empty, even if the
     wrapped exception is a noisier crash.
  3. _looks_like_auth_error matches empirically-captured substrings
     (GitHub "Bad credentials", Figma "Forbidden", slack "invalid_auth"),
     is case-insensitive, and rejects benign text that happens to
     contain digit 401 without the whitespace-padded form.
  4. record_tool_result tags runtime_failures[name] with kind="auth_failed"
     when at least one sub-threshold failure matched an auth pattern, and
     kind="runtime_failure" otherwise.

Run:
    source venv/bin/activate
    python tests/test_1571_error_classification.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
os.environ.setdefault("OPERATOR_BOT", "pm")

from _1_800_operator import config
from _1_800_operator.pipeline.mcp_client import (
    MCPClient,
    RUNTIME_FAILURE_THRESHOLD,
    _classify_startup_failure,
    _looks_like_auth_error,
)


# ---------------------------------------------------------------------------
# Test 1: _resolve_env_vars returns (resolved, missing_vars)
# ---------------------------------------------------------------------------

def test_resolve_env_vars_reports_missing():
    """Env block with unfilled ${VAR} → that var name lands in missing_vars."""
    prev = os.environ.pop("BC_TEST_PROBE_UNSET", None)
    try:
        resolved, missing = config._resolve_env_vars(
            {"PRESENT_KEY": "literal", "NEEDS_VAR": "${BC_TEST_PROBE_UNSET}"},
            server_name="probe",
        )
        assert resolved["PRESENT_KEY"] == "literal"
        assert resolved["NEEDS_VAR"] == ""
        assert missing == ["BC_TEST_PROBE_UNSET"], missing
        print("PASS  test_resolve_env_vars_reports_missing")
    finally:
        if prev is not None:
            os.environ["BC_TEST_PROBE_UNSET"] = prev


def test_resolve_env_vars_empty_when_all_present():
    """All ${VAR} refs resolve → missing_vars is empty; literals pass through."""
    os.environ["BC_TEST_PROBE_SET"] = "value"
    try:
        resolved, missing = config._resolve_env_vars(
            {"A": "${BC_TEST_PROBE_SET}", "B": "literal"},
            server_name="probe",
        )
        assert resolved == {"A": "value", "B": "literal"}
        assert missing == []
        print("PASS  test_resolve_env_vars_empty_when_all_present")
    finally:
        del os.environ["BC_TEST_PROBE_SET"]


# ---------------------------------------------------------------------------
# Test 2: missing_creds precedence in _classify_startup_failure
# ---------------------------------------------------------------------------

def test_classify_missing_creds_beats_noisy_crash():
    """srv_config['missing_vars'] non-empty + any exception → kind=missing_creds."""
    exc = RuntimeError("process exited with code 1")
    info = _classify_startup_failure(
        exc,
        {"command": "linear-mcp", "missing_vars": ["LINEAR_API_KEY"]},
    )
    assert info["kind"] == "missing_creds", info
    assert info["vars"] == ["LINEAR_API_KEY"]
    assert "LINEAR_API_KEY" in info["fix"]
    # Raw trace should still be preserved for debug.
    assert "process exited" in info["raw"].lower()
    print("PASS  test_classify_missing_creds_beats_noisy_crash")


def test_classify_missing_vars_empty_falls_through():
    """Empty/absent missing_vars → classifier behaves like before (binary_missing etc)."""
    exc = FileNotFoundError(2, "No such file or directory", "some-binary")
    info = _classify_startup_failure(exc, {"command": "some-binary", "missing_vars": []})
    assert info["kind"] == "binary_missing", info
    assert "was not found" in info["fix"]
    print("PASS  test_classify_missing_vars_empty_falls_through")


# ---------------------------------------------------------------------------
# Test 3: _looks_like_auth_error — empirical pattern matching
# ---------------------------------------------------------------------------

def test_auth_sniff_matches_github_real_error():
    """Exact empirically-captured GitHub bad-token error → True."""
    text = "failed to get user: GET https://api.github.com/user: 401 Bad credentials []"
    assert _looks_like_auth_error(text) is True
    print("PASS  test_auth_sniff_matches_github_real_error")


def test_auth_sniff_matches_figma_real_error():
    """Exact empirically-captured Figma bad-token error → True."""
    text = "Error fetching file: Figma API returned 403 Forbidden for '/files/xxx'."
    assert _looks_like_auth_error(text) is True
    print("PASS  test_auth_sniff_matches_figma_real_error")


def test_auth_sniff_matches_slack_documented_error():
    """Slack's documented auth-error codes → True."""
    for text in ("invalid_auth", "not_authed", "token_expired"):
        assert _looks_like_auth_error(text) is True, text
    print("PASS  test_auth_sniff_matches_slack_documented_error")


def test_auth_sniff_is_case_insensitive():
    """Patterns match regardless of casing in the source error text."""
    assert _looks_like_auth_error("UNAUTHENTICATED: google API rejected the token") is True
    assert _looks_like_auth_error("Bad Credentials!") is True
    print("PASS  test_auth_sniff_is_case_insensitive")


def test_auth_sniff_rejects_benign_text():
    """Benign tool-error strings don't match; empty/None returns False."""
    assert _looks_like_auth_error(None) is False
    assert _looks_like_auth_error("") is False
    assert _looks_like_auth_error("issue INC-401 assigned to someone") is False, (
        "bare '401' inside an identifier must not trip the sniff"
    )
    assert _looks_like_auth_error("rate limit exceeded, try again in 5m") is False
    print("PASS  test_auth_sniff_rejects_benign_text")


# ---------------------------------------------------------------------------
# Test 4: record_tool_result tags kind="auth_failed" vs "runtime_failure"
# ---------------------------------------------------------------------------

def _fresh_client():
    return MCPClient()


def test_record_tool_result_tags_auth_failed_when_sniff_matches():
    """One of N sub-threshold failures carries auth text → tripped kind is auth_failed."""
    client = _fresh_client()
    # First N-1 failures with auth text, final one with benign text.
    client.record_tool_result("linear", success=False, error_text="401 Unauthorized")
    for _ in range(RUNTIME_FAILURE_THRESHOLD - 2):
        client.record_tool_result("linear", success=False, error_text="generic tool error")
    tripped = client.record_tool_result("linear", success=False, error_text="generic tool error")
    assert tripped, "expected trip on Nth failure"
    entry = client.runtime_failures["linear"]
    assert entry["kind"] == "auth_failed", entry
    assert "auth-error" in entry["reason"]
    print("PASS  test_record_tool_result_tags_auth_failed_when_sniff_matches")


def test_record_tool_result_tags_runtime_failure_when_no_auth_signal():
    """All failures benign → tripped kind is runtime_failure, no auth tag."""
    client = _fresh_client()
    for _ in range(RUNTIME_FAILURE_THRESHOLD):
        client.record_tool_result("github", success=False, error_text="upstream 500 server error")
    entry = client.runtime_failures["github"]
    assert entry["kind"] == "runtime_failure", entry
    assert "auth" not in entry["reason"].lower()
    print("PASS  test_record_tool_result_tags_runtime_failure_when_no_auth_signal")


def test_record_tool_result_success_clears_auth_signal():
    """A success between failures wipes the auth-error memo for that server."""
    client = _fresh_client()
    client.record_tool_result("figma", success=False, error_text="403 Forbidden")
    client.record_tool_result("figma", success=True)
    # Now trip with only benign failures — should NOT tag auth_failed.
    for _ in range(RUNTIME_FAILURE_THRESHOLD):
        client.record_tool_result("figma", success=False, error_text="generic 500")
    assert client.runtime_failures["figma"]["kind"] == "runtime_failure"
    print("PASS  test_record_tool_result_success_clears_auth_signal")


def test_record_tool_result_no_error_text_is_safe():
    """Timeout-path calls pass error_text=None; must not crash the sniff."""
    client = _fresh_client()
    for _ in range(RUNTIME_FAILURE_THRESHOLD):
        client.record_tool_result("notion", success=False)
    assert client.runtime_failures["notion"]["kind"] == "runtime_failure"
    print("PASS  test_record_tool_result_no_error_text_is_safe")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_resolve_env_vars_reports_missing,
        test_resolve_env_vars_empty_when_all_present,
        test_classify_missing_creds_beats_noisy_crash,
        test_classify_missing_vars_empty_falls_through,
        test_auth_sniff_matches_github_real_error,
        test_auth_sniff_matches_figma_real_error,
        test_auth_sniff_matches_slack_documented_error,
        test_auth_sniff_is_case_insensitive,
        test_auth_sniff_rejects_benign_text,
        test_record_tool_result_tags_auth_failed_when_sniff_matches,
        test_record_tool_result_tags_runtime_failure_when_no_auth_signal,
        test_record_tool_result_success_clears_auth_signal,
        test_record_tool_result_no_error_text_is_safe,
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
