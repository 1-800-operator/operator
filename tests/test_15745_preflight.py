"""
Phase 15.7.4.5 — Runtime MCP pre-flight.

Covers `preflight_mcp_readiness`:
  1. All-ok state returns PREFLIGHT_OK silently (no prompts, no output).
  2. missing_env: default Y continues; explicit 'n' returns
     PREFLIGHT_USER_ABORT and surfaces the var names in the output.
  3. oauth_needed: default N (blank input) continues without calling
     run_auth; explicit 'y' invokes run_auth; run_auth success prints
     the ✓ line; run_auth failure prints the ⚠ line and continues.
  4. prereq_missing: default Y continues; 'n' returns USER_ABORT.
  5. Non-interactive stdin (EOFError on input) falls back to default —
     critical for piped launches without --no-preflight.
  6. Multiple problems in one run process in insertion order and each
     is prompted independently.
  7. Glyphs + URLs render in the summary output (so users see what's
     wrong even if they never interact with a prompt).

The helper is input/output/run_auth injectable — no real subprocess,
no real stdin, no real stdout manipulation needed. Full I/O is
stubbed via list-append callbacks.

Run:
    source venv/bin/activate
    python tests/test_15745_preflight.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _1_800_operator.pipeline.readiness import (
    PREFLIGHT_OK,
    PREFLIGHT_USER_ABORT,
    preflight_mcp_readiness,
)


# ---------------------------------------------------------------------------
# Test 1 — all-ok state is silent
# ---------------------------------------------------------------------------

def test_all_ok_returns_ok_silently():
    outputs: list[str] = []

    def _forbid_prompt(_p):
        raise AssertionError("should not prompt when everything is ok")

    servers = {
        "github": {"enabled": True, "env": {}, "missing_vars": []},
        "linear": {"enabled": True, "auth": "oauth",
                   "auth_url": "", "env": {}},  # empty auth_url → oauth_cache_exists=False → oauth_needed
    }
    # The linear entry would normally produce oauth_needed, so to
    # synthesize an all-ok world we drop linear entirely.
    servers.pop("linear")

    rc = preflight_mcp_readiness(
        servers,
        input_fn=_forbid_prompt,
        output_fn=outputs.append,
        run_auth_fn=lambda n: 0,
    )
    assert rc == PREFLIGHT_OK, rc
    assert outputs == [], outputs
    print("PASS  test_all_ok_returns_ok_silently")


# ---------------------------------------------------------------------------
# Test 2 — missing_env prompts, default continues
# ---------------------------------------------------------------------------

def test_missing_env_default_continues():
    outputs: list[str] = []
    rc = preflight_mcp_readiness(
        {"slack": {"enabled": True, "env": {},
                   "missing_vars": ["SLACK_BOT_TOKEN"],
                   "credentials_url": "https://api.slack.com/apps"}},
        input_fn=lambda _p: "",  # default
        output_fn=outputs.append,
        run_auth_fn=lambda n: 0,
    )
    assert rc == PREFLIGHT_OK, rc
    joined = "\n".join(outputs)
    assert "SLACK_BOT_TOKEN" in joined, joined
    assert "https://api.slack.com/apps" in joined, joined
    print("PASS  test_missing_env_default_continues")


def test_missing_env_n_aborts():
    outputs: list[str] = []
    rc = preflight_mcp_readiness(
        {"slack": {"enabled": True, "env": {},
                   "missing_vars": ["SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET"]}},
        input_fn=lambda _p: "n",
        output_fn=outputs.append,
        run_auth_fn=lambda n: 0,
    )
    assert rc == PREFLIGHT_USER_ABORT, rc
    joined = "\n".join(outputs)
    assert "Aborting" in joined, joined
    # Both missing vars should be surfaced in the abort message.
    assert "SLACK_BOT_TOKEN" in joined and "SLACK_SIGNING_SECRET" in joined, joined
    print("PASS  test_missing_env_n_aborts")


# ---------------------------------------------------------------------------
# Test 3 — oauth_needed prompts, default skips auth
# ---------------------------------------------------------------------------

def test_oauth_needed_default_skips_auth_invocation():
    outputs: list[str] = []
    auth_calls: list[str] = []
    rc = preflight_mcp_readiness(
        {"linear": {"enabled": True, "auth": "oauth",
                    "auth_url": "https://unused-in-tests.invalid/oauth", "env": {}}},
        input_fn=lambda _p: "",  # default N
        output_fn=outputs.append,
        run_auth_fn=lambda n: auth_calls.append(n) or 0,
    )
    assert rc == PREFLIGHT_OK, rc
    assert auth_calls == [], auth_calls
    print("PASS  test_oauth_needed_default_skips_auth_invocation")


def test_oauth_needed_y_invokes_run_auth_success():
    outputs: list[str] = []
    auth_calls: list[str] = []

    def _auth(name):
        auth_calls.append(name)
        return 0

    rc = preflight_mcp_readiness(
        {"linear": {"enabled": True, "auth": "oauth",
                    "auth_url": "https://unused-in-tests.invalid/oauth", "env": {}}},
        input_fn=lambda _p: "y",
        output_fn=outputs.append,
        run_auth_fn=_auth,
    )
    assert rc == PREFLIGHT_OK, rc
    assert auth_calls == ["linear"], auth_calls
    joined = "\n".join(outputs)
    assert "authorized" in joined, joined
    print("PASS  test_oauth_needed_y_invokes_run_auth_success")


def test_oauth_needed_y_with_run_auth_failure_continues():
    outputs: list[str] = []

    def _failing_auth(_name):
        return 1  # user bailed or mcp-remote errored

    rc = preflight_mcp_readiness(
        {"linear": {"enabled": True, "auth": "oauth",
                    "auth_url": "https://unused-in-tests.invalid/oauth", "env": {}}},
        input_fn=lambda _p: "y",
        output_fn=outputs.append,
        run_auth_fn=_failing_auth,
    )
    # Non-fatal: user may have opted out mid-browser, but the bot can
    # still launch with linear runtime-disabled. PREFLIGHT_OK by design.
    assert rc == PREFLIGHT_OK, rc
    joined = "\n".join(outputs)
    assert "not authorized" in joined, joined
    assert "operator auth linear" in joined, joined
    print("PASS  test_oauth_needed_y_with_run_auth_failure_continues")


# ---------------------------------------------------------------------------
# Test 4 — prereq_missing prompts (claude-code branch)
# ---------------------------------------------------------------------------

def test_prereq_missing_default_continues(monkey_shutil=None):
    from unittest.mock import patch
    outputs: list[str] = []
    # Force claude-code into prereq_missing by stubbing shutil.which to None.
    with patch("_1_800_operator.pipeline.readiness.shutil.which", return_value=None):
        rc = preflight_mcp_readiness(
            {"claude-code": {"enabled": True, "env": {},
                             "credentials_url": "https://docs.claude.com/x"}},
            input_fn=lambda _p: "",  # default Y
            output_fn=outputs.append,
            run_auth_fn=lambda n: 0,
        )
    assert rc == PREFLIGHT_OK, rc
    joined = "\n".join(outputs)
    assert "git" in joined.lower() or "claude" in joined.lower(), joined
    print("PASS  test_prereq_missing_default_continues")


def test_prereq_missing_n_aborts():
    from unittest.mock import patch
    outputs: list[str] = []
    with patch("_1_800_operator.pipeline.readiness.shutil.which", return_value=None):
        rc = preflight_mcp_readiness(
            {"claude-code": {"enabled": True, "env": {},
                             "credentials_url": "https://docs.claude.com/x"}},
            input_fn=lambda _p: "n",
            output_fn=outputs.append,
            run_auth_fn=lambda n: 0,
        )
    assert rc == PREFLIGHT_USER_ABORT, rc
    joined = "\n".join(outputs)
    assert "Aborting" in joined, joined
    print("PASS  test_prereq_missing_n_aborts")


# ---------------------------------------------------------------------------
# Test 5 — non-interactive stdin
# ---------------------------------------------------------------------------

def test_eof_on_input_falls_back_to_default():
    """Pipe or /dev/null on stdin must not hang; default answer wins."""
    outputs: list[str] = []

    def _eof(_p):
        raise EOFError()

    # missing_env with EOF → default Y → PREFLIGHT_OK
    rc = preflight_mcp_readiness(
        {"slack": {"enabled": True, "env": {}, "missing_vars": ["X"]}},
        input_fn=_eof,
        output_fn=outputs.append,
        run_auth_fn=lambda n: 0,
    )
    assert rc == PREFLIGHT_OK, rc
    # oauth_needed with EOF → default N → PREFLIGHT_OK, no auth called
    auth_calls: list[str] = []
    rc = preflight_mcp_readiness(
        {"linear": {"enabled": True, "auth": "oauth",
                    "auth_url": "https://unused-in-tests.invalid/oauth", "env": {}}},
        input_fn=_eof,
        output_fn=outputs.append,
        run_auth_fn=lambda n: auth_calls.append(n) or 0,
    )
    assert rc == PREFLIGHT_OK, rc
    assert auth_calls == [], auth_calls
    print("PASS  test_eof_on_input_falls_back_to_default")


# ---------------------------------------------------------------------------
# Test 6 — multi-server problems processed in order
# ---------------------------------------------------------------------------

def test_multiple_problems_each_prompted():
    prompts_seen: list[str] = []
    outputs: list[str] = []

    def _record_then_default(prompt):
        prompts_seen.append(prompt)
        return ""

    rc = preflight_mcp_readiness(
        {
            "linear": {"enabled": True, "auth": "oauth",
                       "auth_url": "https://x", "env": {}},
            "slack": {"enabled": True, "env": {}, "missing_vars": ["ST"]},
        },
        input_fn=_record_then_default,
        output_fn=outputs.append,
        run_auth_fn=lambda n: 0,
    )
    assert rc == PREFLIGHT_OK, rc
    assert len(prompts_seen) == 2, prompts_seen
    assert "linear" in prompts_seen[0] and "slack" in prompts_seen[1], prompts_seen
    print("PASS  test_multiple_problems_each_prompted")


# ---------------------------------------------------------------------------
# Test 7 — status summary includes glyph + url
# ---------------------------------------------------------------------------

def test_summary_surfaces_glyph_and_fix_url():
    outputs: list[str] = []
    rc = preflight_mcp_readiness(
        {"slack": {"enabled": True, "env": {},
                   "missing_vars": ["SLACK_BOT_TOKEN"],
                   "credentials_url": "https://api.slack.com/apps"}},
        input_fn=lambda _p: "",
        output_fn=outputs.append,
        run_auth_fn=lambda n: 0,
    )
    assert rc == PREFLIGHT_OK, rc
    joined = "\n".join(outputs)
    # Glyph and URL both surface in the pre-prompt summary so user can
    # eyeball the state before answering.
    assert "✗" in joined, joined
    assert "https://api.slack.com/apps" in joined, joined
    print("PASS  test_summary_surfaces_glyph_and_fix_url")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_all_ok_returns_ok_silently,
        test_missing_env_default_continues,
        test_missing_env_n_aborts,
        test_oauth_needed_default_skips_auth_invocation,
        test_oauth_needed_y_invokes_run_auth_success,
        test_oauth_needed_y_with_run_auth_failure_continues,
        test_prereq_missing_default_continues,
        test_prereq_missing_n_aborts,
        test_eof_on_input_falls_back_to_default,
        test_multiple_problems_each_prompted,
        test_summary_surfaces_glyph_and_fix_url,
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
