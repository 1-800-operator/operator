"""
Gap-fill unit tests for pipeline/chat_runner.py (test-plan Component E).

Picks up ChatRunner branches not covered by the existing test_911/912/913/915
and test_chat_hardening suites:

  M1 — 1-on-1 dispatch + participant auto-leave
  M2 — confirmation flow (_needs_confirmation / _handle_confirmation)
  M3 — skills routing (slash fast-path, load_skill tool, _tools_for_llm)
  M4 — intro-on-join (post-once + pre-intro buffer drain, both flag states)
  M5 — MCP server trip notifier (_record_mcp_outcome)
  M6 — _send own-message bookkeeping (success + send_chat failure)

Run: source venv/bin/activate && python tests/test_chat_runner_gaps.py
"""
import os
os.environ.setdefault("OPERATOR_BOT", "pm")

import sys
import threading
import time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from unittest.mock import MagicMock

from _1_800_operator import config
from _1_800_operator.pipeline.chat_runner import ChatRunner, LOAD_SKILL_TOOL
from _1_800_operator.pipeline.skills import Skill


# ---------------------------------------------------------------------------
# Shared scaffolding
# ---------------------------------------------------------------------------

def make_runner(
    *,
    mcp=None,
    skills=None,
    skills_progressive=True,
    read_chat_side_effect=None,
    participant_counts=None,
    connected=True,
):
    """Build a ChatRunner wired against MagicMock connector + llm.

    participant_counts: list of ints consumed one per get_participant_count()
    call. After exhausting, the last value sticks (like the live connector).
    """
    connector = MagicMock()
    connector.is_connected.return_value = connected
    connector.read_chat.side_effect = read_chat_side_effect or (lambda: [])

    if participant_counts is not None:
        counts = list(participant_counts)
        def _pc():
            return counts.pop(0) if len(counts) > 1 else counts[0]
        connector.get_participant_count.side_effect = _pc
    else:
        connector.get_participant_count.return_value = 2

    llm = MagicMock()
    runner = ChatRunner(
        connector, llm, mcp_client=mcp,
        skills=skills, skills_progressive=skills_progressive,
    )
    return runner, connector, llm


def run_loop_briefly(runner, duration=0.3):
    """Run ChatRunner._loop() in a thread, stop after duration."""
    done = threading.Event()

    def _go():
        runner._loop()
        done.set()

    t = threading.Thread(target=_go, daemon=True)
    t.start()
    time.sleep(duration)
    runner._stop_event.set()
    done.wait(timeout=2.0)
    return done.is_set()


# ===========================================================================
# M1 — 1-on-1 dispatch + auto-leave
# ===========================================================================

def test_one_on_one_dispatches_without_trigger():
    """participant_count <= ONE_ON_ONE_THRESHOLD → message routed to LLM
    even without the trigger phrase."""
    runner, _, llm = make_runner(participant_counts=[2])  # 2 ≤ threshold
    llm.ask.return_value = "hi"

    # Avoid touching the LLM scratchpad — stub the dispatch target.
    handled = []
    runner._handle_message = lambda t: handled.append(t)

    runner._dispatch_user_message("what time is it", one_on_one=True)

    assert handled == ["what time is it"], f"Expected LLM dispatch, got {handled}"
    print("PASS  test_one_on_one_dispatches_without_trigger")


def test_group_mode_requires_trigger():
    """participant_count > threshold AND no trigger → no dispatch."""
    runner, _, _ = make_runner()
    handled = []
    runner._handle_message = lambda t: handled.append(t)

    runner._dispatch_user_message("let's discuss strategy", one_on_one=False)

    assert handled == [], f"Non-triggered group msg should not dispatch, got {handled}"
    print("PASS  test_group_mode_requires_trigger")


def test_auto_leave_after_grace():
    """Once peers seen and now alone for ALONE_EXIT_GRACE_SECONDS → connector.leave()."""
    # Patch to short windows so the test runs sub-second.
    import _1_800_operator.pipeline.chat_runner as cr
    orig_grace = config.ALONE_EXIT_GRACE_SECONDS
    orig_interval = cr.PARTICIPANT_CHECK_INTERVAL
    orig_poll = cr.POLL_INTERVAL
    config.ALONE_EXIT_GRACE_SECONDS = 0.2
    cr.PARTICIPANT_CHECK_INTERVAL = 0.0  # check every loop pass
    cr.POLL_INTERVAL = 0.05
    try:
        # First call: 2 peers (saw_others=True); subsequent: 1 (alone).
        calls = {"n": 0}
        def _pc():
            calls["n"] += 1
            return 2 if calls["n"] == 1 else 1

        runner, connector, _ = make_runner()
        connector.get_participant_count.side_effect = _pc

        done = threading.Event()
        def _go():
            runner._loop()
            done.set()
        t = threading.Thread(target=_go, daemon=True)
        t.start()

        exited = done.wait(timeout=2.0)
        assert exited, "Loop did not exit after alone-grace expired"
        connector.leave.assert_called_once()
    finally:
        config.ALONE_EXIT_GRACE_SECONDS = orig_grace
        cr.PARTICIPANT_CHECK_INTERVAL = orig_interval
        cr.POLL_INTERVAL = orig_poll
    print("PASS  test_auto_leave_after_grace")


# ===========================================================================
# M2 — confirmation flow
# ===========================================================================

def test_needs_confirmation_branches(monkeypatch=None):
    """read_tools auto, confirm_tools forced, unknown server → safe-default confirm."""
    runner, _, _ = make_runner()

    # Swap config.MCP_SERVERS for a deterministic fixture.
    orig = config.MCP_SERVERS
    config.MCP_SERVERS = {
        "linear": {
            "read_tools": {"list_issues"},
            "confirm_tools": {"delete_issue"},
        },
    }
    try:
        # read_tool → no confirmation
        assert runner._needs_confirmation({"name": "linear__list_issues"}) is False
        # confirm_tool → confirmation required even if also in read_tools
        assert runner._needs_confirmation({"name": "linear__delete_issue"}) is True
        # tool not declared → safe default (confirm)
        assert runner._needs_confirmation({"name": "linear__create_issue"}) is True
        # unknown server (no `__` split) → safe default (confirm)
        assert runner._needs_confirmation({"name": "load_skill"}) is True
        # declared server but missing from MCP_SERVERS → safe default
        assert runner._needs_confirmation({"name": "github__list_repos"}) is True
    finally:
        config.MCP_SERVERS = orig
    print("PASS  test_needs_confirmation_branches")


def test_request_confirmation_renders_all_args_and_truncates_long_values():
    """Every arg shows; long values get head…tail truncation + log pointer."""
    runner, _, _ = make_runner()
    sent = []
    runner._send = lambda text, kind="chat": sent.append((text, kind))

    short = "ENG-123"
    long_body = "x" * 500
    tc = {
        "id": "c1",
        "name": "linear__create_issue",
        "arguments": {
            "team": short,
            "title": "bug: thing broke",
            "description": long_body,
            "priority": 2,
            "labels": ["bug", "urgent"],
            "parent": "ENG-100",
        },
    }
    runner._request_confirmation(tc)

    assert len(sent) == 1 and sent[0][1] == "confirmation"
    msg = sent[0][0]
    # All 6 args surface — previously only the first 5 were shown
    for k in ("team=", "title=", "description=", "priority=", "labels=", "parent="):
        assert k in msg, f"missing {k!r} in confirmation: {msg}"
    # The long value was truncated — full 500-char body must not appear
    assert long_body not in msg, "Full long value leaked through untruncated"
    assert "…" in msg, f"Expected head…tail ellipsis, got: {msg}"
    # Log-pointer present so the user can cross-reference
    assert "/tmp/operator.log" in msg
    # Short values survive unchanged
    assert "ENG-123" in msg
    print("PASS  test_request_confirmation_renders_all_args_and_truncates_long_values")


def test_request_confirmation_no_truncation_on_short_args():
    """No pointer when nothing was truncated — keeps the short prompt clean."""
    runner, _, _ = make_runner()
    sent = []
    runner._send = lambda text, kind="chat": sent.append((text, kind))

    tc = {
        "id": "c2",
        "name": "linear__list_issues",
        "arguments": {"team": "ENG", "state": "open"},
    }
    runner._request_confirmation(tc)
    msg = sent[0][0]
    assert "/tmp/operator.log" not in msg, f"Unexpected log pointer: {msg}"
    assert "…" not in msg, f"Unexpected truncation marker: {msg}"
    print("PASS  test_request_confirmation_no_truncation_on_short_args")


def test_handle_confirmation_affirmative_executes():
    """Affirmative words → clears pending, executes tool."""
    runner, _, _ = make_runner(mcp=MagicMock())
    runner._pending_tool_call = {"id": "c1", "name": "srv__foo", "arguments": {}}
    executed = []
    runner._execute_and_respond = lambda tc: executed.append(tc)

    runner._handle_confirmation("yes please")

    assert runner._pending_tool_call is None, "Pending should be cleared"
    assert len(executed) == 1 and executed[0]["id"] == "c1", "Tool should execute"
    print("PASS  test_handle_confirmation_affirmative_executes")


def test_handle_confirmation_non_affirmative_is_correction_not_cancel():
    """Non-affirmative text → re-prompts LLM with correction signpost, NOT cancellation."""
    mcp = MagicMock()
    mcp.get_openai_tools.return_value = []
    runner, _, llm = make_runner(mcp=mcp)
    runner._pending_tool_call = {"id": "c2", "name": "srv__foo", "arguments": {}}
    llm.send_tool_result.return_value = {"type": "text", "content": "revised proposal"}
    sent = []
    runner._send = lambda text, kind="chat": sent.append((text, kind))

    runner._handle_confirmation("actually make it priority high")

    assert runner._pending_tool_call is None
    # LLM was fed the user's correction text as the tool "result"
    call = llm.send_tool_result.call_args
    assert call.args[0] == "c2"
    assert "actually make it priority high" in call.args[2]
    assert "adjust" in call.args[2].lower() or "re-propose" in call.args[2].lower()
    # The LLM's follow-up text was dispatched back to chat
    assert any("revised proposal" in s[0] for s in sent), f"Expected LLM follow-up sent, got {sent}"
    print("PASS  test_handle_confirmation_non_affirmative_is_correction_not_cancel")


def test_pending_confirmation_messages_tagged_in_record():
    """When _pending_tool_call is set, inbound chat is persisted with kind='confirmation'."""
    runner, connector, _ = make_runner()
    # Stub the meeting record to capture append() calls.
    rec = MagicMock()
    runner._record = rec
    runner._pending_tool_call = {"id": "c3", "name": "srv__foo", "arguments": {}}
    runner._handle_confirmation = lambda text: None  # don't actually process

    connector.read_chat.side_effect = [
        [{"id": "m1", "text": "ok", "sender": "Alice"}],
        [],
    ]

    import _1_800_operator.pipeline.chat_runner as cr
    orig_poll = cr.POLL_INTERVAL
    cr.POLL_INTERVAL = 0.05
    try:
        run_loop_briefly(runner, duration=0.2)
    finally:
        cr.POLL_INTERVAL = orig_poll

    append_calls = [c for c in rec.append.call_args_list]
    assert len(append_calls) >= 1, "Inbound message should be appended"
    kinds = [c.kwargs.get("kind") for c in append_calls]
    assert "confirmation" in kinds, f"Expected kind='confirmation' in appends, got {kinds}"
    print("PASS  test_pending_confirmation_messages_tagged_in_record")


# ===========================================================================
# M3 — skills routing
# ===========================================================================

def test_tools_for_llm_gating():
    """load_skill appears only when skills loaded AND progressive=True."""
    mcp = MagicMock()
    mcp.get_openai_tools.return_value = []

    # No skills → no load_skill tool
    runner, _, _ = make_runner(mcp=mcp, skills=[], skills_progressive=True)
    assert runner._tools_for_llm() is None

    # Skills + progressive=False → no load_skill tool
    sk = Skill(name="review", description="review things", body="BODY")
    runner, _, _ = make_runner(mcp=mcp, skills=[sk], skills_progressive=False)
    assert runner._tools_for_llm() is None

    # Skills + progressive=True → load_skill appears with enum of names
    runner, _, _ = make_runner(mcp=mcp, skills=[sk], skills_progressive=True)
    tools = runner._tools_for_llm()
    assert tools is not None
    ls = [t for t in tools if t["function"]["name"] == LOAD_SKILL_TOOL]
    assert len(ls) == 1
    assert ls[0]["function"]["parameters"]["properties"]["name"]["enum"] == ["review"]
    print("PASS  test_tools_for_llm_gating")


def test_slash_invocation_prepends_skill_body():
    """/<skill-name> → extra_system contains the skill body."""
    mcp = MagicMock()
    mcp.get_openai_tools.return_value = []
    sk = Skill(name="review", description="", body="SKILL BODY TEXT")
    runner, _, llm = make_runner(mcp=mcp, skills=[sk])
    llm.ask.return_value = "done"
    runner._send = lambda text, kind="chat": None

    runner._handle_message("/review please look at PR 42")

    call = llm.ask.call_args
    extra = call.kwargs.get("extra_system", "")
    assert "SKILL BODY TEXT" in extra, f"Expected skill body in extra_system, got: {extra!r}"
    assert runner._load_skill_calls == 1
    assert runner._load_skill_by_name == {"review": 1}
    print("PASS  test_slash_invocation_prepends_skill_body")


def test_slash_unknown_falls_through():
    """Unknown /<name> → normal LLM call, no extra_system injected."""
    mcp = MagicMock()
    mcp.get_openai_tools.return_value = []
    runner, _, llm = make_runner(mcp=mcp, skills=[Skill("review", "", "BODY")])
    llm.ask.return_value = "ok"
    runner._send = lambda text, kind="chat": None

    runner._handle_message("/nonexistent do something")

    call = llm.ask.call_args
    assert call.kwargs.get("extra_system", "") == ""
    assert runner._load_skill_calls == 0
    print("PASS  test_slash_unknown_falls_through")


def test_handle_load_skill_valid():
    """load_skill tool call with known name → body fed back to LLM as tool result."""
    mcp = MagicMock()
    mcp.get_openai_tools.return_value = []
    sk = Skill(name="triage", description="", body="TRIAGE STEPS")
    runner, _, llm = make_runner(mcp=mcp, skills=[sk])
    llm.send_tool_result.return_value = {"type": "text", "content": "triaged"}
    sent = []
    runner._send = lambda text, kind="chat": sent.append(text)

    runner._handle_load_skill({
        "id": "ls1", "name": LOAD_SKILL_TOOL, "arguments": {"name": "triage"},
    })

    call = llm.send_tool_result.call_args
    assert call.args[0] == "ls1"
    assert "TRIAGE STEPS" in call.args[2]
    assert runner._load_skill_calls == 1
    assert sent == ["triaged"]
    print("PASS  test_handle_load_skill_valid")


def test_handle_load_skill_unknown():
    """load_skill with unknown name → error content mentions available skills."""
    mcp = MagicMock()
    mcp.get_openai_tools.return_value = []
    sk = Skill(name="triage", description="", body="X")
    runner, _, llm = make_runner(mcp=mcp, skills=[sk])
    llm.send_tool_result.return_value = {"type": "text", "content": "no-op"}
    runner._send = lambda text, kind="chat": None

    runner._handle_load_skill({
        "id": "ls2", "name": LOAD_SKILL_TOOL, "arguments": {"name": "bogus"},
    })

    content = llm.send_tool_result.call_args.args[2]
    assert "bogus" in content.lower() or "no skill" in content.lower()
    assert "triage" in content, "Available-skills list should include existing skills"
    print("PASS  test_handle_load_skill_unknown")


# ===========================================================================
# M4 — intro-on-join
# ===========================================================================

def test_intro_on_join_posts_and_drains_buffer():
    """INTRO_ON_JOIN=True: intro posts once, then pre-intro messages dispatch in order."""
    runner, _, llm = make_runner()
    # Simulate the pre-intro phase: _intro_posted is False until _intro_ready fires.
    runner._intro_posted = False
    runner._intro_text = "Hi, I'm the operator."
    runner._intro_ready.set()
    runner._pre_intro_buffer = [
        {"text": "first", "one_on_one": True},
        {"text": "second", "one_on_one": True},
    ]

    sent = []
    runner._send = lambda text, kind="chat": sent.append(text)
    dispatched = []
    runner._dispatch_user_message = lambda t, o: dispatched.append((t, o))

    # Drive one _loop iteration manually by invoking the drain block.
    # Easier: call _loop in a thread and stop it.
    import _1_800_operator.pipeline.chat_runner as cr
    orig_poll = cr.POLL_INTERVAL
    cr.POLL_INTERVAL = 0.05
    try:
        run_loop_briefly(runner, duration=0.15)
    finally:
        cr.POLL_INTERVAL = orig_poll

    assert runner._intro_posted is True, "intro should be marked posted"
    assert sent == ["Hi, I'm the operator."], f"Expected intro sent once, got {sent}"
    assert dispatched == [("first", True), ("second", True)], \
        f"Expected buffered msgs dispatched in order, got {dispatched}"
    assert runner._pre_intro_buffer == []
    print("PASS  test_intro_on_join_posts_and_drains_buffer")


def test_intro_failure_skips_post_but_still_drains():
    """Intro generation failed (_intro_text='') → skip post, still drain buffer."""
    runner, _, _ = make_runner()
    runner._intro_posted = False
    runner._intro_text = ""  # generation failed
    runner._intro_ready.set()
    runner._pre_intro_buffer = [{"text": "msg1", "one_on_one": True}]

    sent = []
    runner._send = lambda text, kind="chat": sent.append(text)
    dispatched = []
    runner._dispatch_user_message = lambda t, o: dispatched.append((t, o))

    import _1_800_operator.pipeline.chat_runner as cr
    orig_poll = cr.POLL_INTERVAL
    cr.POLL_INTERVAL = 0.05
    try:
        run_loop_briefly(runner, duration=0.15)
    finally:
        cr.POLL_INTERVAL = orig_poll

    assert sent == [], f"No intro send expected on failure, got {sent}"
    assert runner._intro_posted is True
    assert dispatched == [("msg1", True)], f"Buffer should still drain, got {dispatched}"
    print("PASS  test_intro_failure_skips_post_but_still_drains")


def test_intro_disabled_no_buffering():
    """INTRO_ON_JOIN=False → _intro_posted=True at init; messages dispatch immediately."""
    orig = config.INTRO_ON_JOIN
    config.INTRO_ON_JOIN = False
    try:
        runner, _, _ = make_runner()
        assert runner._intro_posted is True, \
            "When INTRO_ON_JOIN=False, _intro_posted should be True at init"
        # Simulate a user message arriving → would bypass buffering since _intro_posted.
        # Exercise the branch in _loop by checking state post-ctor.
        assert runner._pre_intro_buffer == []
    finally:
        config.INTRO_ON_JOIN = orig
    print("PASS  test_intro_disabled_no_buffering")


def test_intro_waits_for_human_in_empty_room():
    """Bot alone in meeting (participant_count=1) → intro stays buffered.
    The moment another participant appears, intro posts on the next iteration.

    Guards the meet.new path: bot joins instantly, opens chat, but the user
    is still on Meet's pre-join screen and would miss any chat sent before
    they actually appear in the meeting.
    """
    runner, _, _ = make_runner(participant_counts=[1, 1, 2])
    runner._intro_posted = False
    runner._intro_text = "Hi, I'm the operator."
    runner._intro_ready.set()

    sent = []
    runner._send = lambda text, kind="chat": sent.append(text)

    import _1_800_operator.pipeline.chat_runner as cr
    orig_poll = cr.POLL_INTERVAL
    orig_interval = cr.PARTICIPANT_CHECK_INTERVAL
    cr.POLL_INTERVAL = 0.05
    cr.PARTICIPANT_CHECK_INTERVAL = 0.05  # force a fresh participant check each loop
    try:
        run_loop_briefly(runner, duration=0.3)
    finally:
        cr.POLL_INTERVAL = orig_poll
        cr.PARTICIPANT_CHECK_INTERVAL = orig_interval

    assert runner._intro_posted is True, "intro should post after a human is seen"
    assert sent == ["Hi, I'm the operator."], \
        f"Expected intro sent exactly once after participant appeared, got {sent}"
    print("PASS  test_intro_waits_for_human_in_empty_room")


# ===========================================================================
# M5 — server trip notifier
# ===========================================================================

def test_record_mcp_outcome_trips_server_and_reinjects():
    """Tripped server → user-facing notice + inject_mcp_status with updated lists."""
    mcp = MagicMock()
    mcp.server_for_tool.return_value = "linear"
    mcp.record_tool_result.return_value = True  # tripped
    mcp.startup_failures = {}
    mcp.runtime_failures = {"linear": {"kind": "runtime_failure", "reason": "3 consecutive failures"}}
    runner, _, llm = make_runner(mcp=mcp)

    sent = []
    runner._send = lambda text, kind="chat": sent.append(text)

    # Stub config.MCP_SERVERS to include linear + a still-loaded one
    orig = config.MCP_SERVERS
    config.MCP_SERVERS = {
        "linear": {"read_tools": set(), "confirm_tools": set()},
        "github": {"read_tools": set(), "confirm_tools": set()},
    }
    try:
        runner._record_mcp_outcome("linear__list_issues", success=False)
    finally:
        config.MCP_SERVERS = orig

    # User was notified
    assert len(sent) == 1 and "linear" in sent[0].lower()
    assert "issues" in sent[0].lower() or "skipping" in sent[0].lower()
    # LLM got updated status
    llm.inject_mcp_status.assert_called_once()
    call = llm.inject_mcp_status.call_args
    loaded = call.args[0]
    assert "github" in loaded and "linear" not in loaded, \
        f"loaded list should drop tripped server, got {loaded}"
    print("PASS  test_record_mcp_outcome_trips_server_and_reinjects")


def test_record_mcp_outcome_no_trip_is_silent():
    """Non-tripping outcome → no send, no inject."""
    mcp = MagicMock()
    mcp.server_for_tool.return_value = "linear"
    mcp.record_tool_result.return_value = False  # no trip
    runner, _, llm = make_runner(mcp=mcp)
    sent = []
    runner._send = lambda text, kind="chat": sent.append(text)

    runner._record_mcp_outcome("linear__list_issues", success=True)

    assert sent == []
    llm.inject_mcp_status.assert_not_called()
    print("PASS  test_record_mcp_outcome_no_trip_is_silent")


# ===========================================================================
# M6 — _send own-message bookkeeping
# ===========================================================================

def test_send_tracks_own_message_and_appends_record():
    """_send: text added to _own_messages, appended to record with the given kind."""
    runner, connector, _ = make_runner()
    rec = MagicMock()
    runner._record = rec

    runner._send("hello there", kind="chat")

    assert "hello there" in runner._own_messages
    connector.send_chat.assert_called_once_with("hello there")
    rec.append.assert_called_once()
    kwargs = rec.append.call_args.kwargs
    assert kwargs.get("text") == "hello there"
    assert kwargs.get("kind") == "chat"
    print("PASS  test_send_tracks_own_message_and_appends_record")


def test_send_discards_own_message_on_connector_failure():
    """_send: if send_chat raises, remove text from _own_messages so retry isn't filtered."""
    runner, connector, _ = make_runner()
    connector.send_chat.side_effect = RuntimeError("chat panel gone")

    runner._send("retry me")

    assert "retry me" not in runner._own_messages, \
        "Failed send should not leave text in own_messages (would filter a future retry)"
    print("PASS  test_send_discards_own_message_on_connector_failure")


# ===========================================================================
# M7 — Phase 15.7.2 proactive failure banner
# ===========================================================================

def test_failure_banner_silent_when_no_mcp():
    """No MCP wired → banner path must not crash or post."""
    runner, _, _ = make_runner(mcp=None)
    sent = []
    runner._send = lambda t, kind="chat": sent.append(t)
    runner._post_mcp_failure_banner()
    assert sent == []
    print("PASS  test_failure_banner_silent_when_no_mcp")


def test_failure_banner_silent_when_no_failures():
    """MCP wired but startup_failures empty → no banner."""
    mcp = MagicMock()
    mcp.startup_failures = {}
    runner, _, _ = make_runner(mcp=mcp)
    sent = []
    runner._send = lambda t, kind="chat": sent.append(t)
    runner._post_mcp_failure_banner()
    assert sent == []
    print("PASS  test_failure_banner_silent_when_no_failures")


def test_failure_banner_missing_creds_single_var():
    """missing_creds w/ one var → banner names the var inline."""
    mcp = MagicMock()
    mcp.startup_failures = {
        "slack": {"kind": "missing_creds", "vars": ["SLACK_BOT_TOKEN"], "fix": "set it in .env"}
    }
    runner, _, _ = make_runner(mcp=mcp)
    sent = []
    runner._send = lambda t, kind="chat": sent.append(t)
    runner._post_mcp_failure_banner()
    assert len(sent) == 1, sent
    line = sent[0]
    assert line.startswith("Heads-up — "), line
    assert "slack didn't load" in line
    assert "SLACK_BOT_TOKEN" in line
    assert line.endswith("Ask for details.")
    print("PASS  test_failure_banner_missing_creds_single_var")


def test_failure_banner_missing_creds_multi_var_uses_generic_label():
    """missing_creds w/ 2+ vars → generic 'credentials' label (not a comma-list)."""
    mcp = MagicMock()
    mcp.startup_failures = {
        "sentry": {"kind": "missing_creds", "vars": ["SENTRY_AUTH_TOKEN", "SENTRY_ORG"]}
    }
    runner, _, _ = make_runner(mcp=mcp)
    sent = []
    runner._send = lambda t, kind="chat": sent.append(t)
    runner._post_mcp_failure_banner()
    assert "credentials" in sent[0], sent[0]
    assert "SENTRY_AUTH_TOKEN" not in sent[0], "multi-var banner should not inline var names"
    print("PASS  test_failure_banner_missing_creds_multi_var_uses_generic_label")


def test_failure_banner_joins_multiple_failures():
    """Multiple failed servers → semicolon-joined fragments, single post."""
    mcp = MagicMock()
    mcp.startup_failures = {
        "slack":  {"kind": "missing_creds", "vars": ["SLACK_BOT_TOKEN"]},
        "figma":  {"kind": "binary_missing"},
        "notion": {"kind": "handshake_crash"},
    }
    runner, _, _ = make_runner(mcp=mcp)
    sent = []
    runner._send = lambda t, kind="chat": sent.append(t)
    runner._post_mcp_failure_banner()
    assert len(sent) == 1
    line = sent[0]
    assert "slack didn't load" in line
    assert "figma didn't load" in line and "binary not found" in line
    assert "notion didn't load" in line and "crashed on startup" in line
    assert line.count("; ") == 2, f"expected 2 semicolon separators, got: {line}"
    print("PASS  test_failure_banner_joins_multiple_failures")


def test_failure_banner_unknown_kind_falls_back_to_error():
    """Classifier produced kind=unknown → compact 'error' label still posts."""
    mcp = MagicMock()
    mcp.startup_failures = {"exotic": {"kind": "unknown", "raw": "whatever"}}
    runner, _, _ = make_runner(mcp=mcp)
    sent = []
    runner._send = lambda t, kind="chat": sent.append(t)
    runner._post_mcp_failure_banner()
    assert "exotic didn't load (error)" in sent[0], sent[0]
    print("PASS  test_failure_banner_unknown_kind_falls_back_to_error")


# ===========================================================================
# Runner
# ===========================================================================

if __name__ == "__main__":
    tests = [
        # M1
        test_one_on_one_dispatches_without_trigger,
        test_group_mode_requires_trigger,
        test_auto_leave_after_grace,
        # M2
        test_needs_confirmation_branches,
        test_request_confirmation_renders_all_args_and_truncates_long_values,
        test_request_confirmation_no_truncation_on_short_args,
        test_handle_confirmation_affirmative_executes,
        test_handle_confirmation_non_affirmative_is_correction_not_cancel,
        test_pending_confirmation_messages_tagged_in_record,
        # M3
        test_tools_for_llm_gating,
        test_slash_invocation_prepends_skill_body,
        test_slash_unknown_falls_through,
        test_handle_load_skill_valid,
        test_handle_load_skill_unknown,
        # M4
        test_intro_on_join_posts_and_drains_buffer,
        test_intro_failure_skips_post_but_still_drains,
        test_intro_disabled_no_buffering,
        test_intro_waits_for_human_in_empty_room,
        # M5
        test_record_mcp_outcome_trips_server_and_reinjects,
        test_record_mcp_outcome_no_trip_is_silent,
        # M6
        test_send_tracks_own_message_and_appends_record,
        test_send_discards_own_message_on_connector_failure,
        # M7
        test_failure_banner_silent_when_no_mcp,
        test_failure_banner_silent_when_no_failures,
        test_failure_banner_missing_creds_single_var,
        test_failure_banner_missing_creds_multi_var_uses_generic_label,
        test_failure_banner_joins_multiple_failures,
        test_failure_banner_unknown_kind_falls_back_to_error,
    ]
    failures = []
    for t in tests:
        try:
            t()
        except Exception as e:
            failures.append((t.__name__, e))
            print(f"FAIL  {t.__name__}: {e}")
    print()
    if failures:
        print(f"{len(failures)}/{len(tests)} FAILED")
        sys.exit(1)
    print(f"{len(tests)}/{len(tests)} passed")
