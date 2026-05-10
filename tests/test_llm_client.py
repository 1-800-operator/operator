"""
Unit tests for Component C — LLMClient (Boundary depth).

Covers pipeline/llm.py:
  1. ask() — single provider.complete call, empty system + chat tail wired
  2. _tail_messages — agent sender to assistant role; others get "first: text";
     captions are dropped (transcript MCP delivers them on demand)

Pre-14.22.3.5 there was an `intro()` test here; intro itself was deleted in
that audit step (operator-authored prompt fed into claude -p — the same
harness-shaped pattern stripped from the heartbeat side-channel). Slip mode
never called intro (`quiet_mode=True` skipped it); after the audit, no
caller remains.

Uses MagicMock for the provider and an in-memory MeetingRecord.

Run:
    source venv/bin/activate
    python tests/test_llm_client.py
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
os.environ.setdefault("OPERATOR_BOT", "pm")

from unittest.mock import MagicMock

from _1_800_operator import config
from _1_800_operator.pipeline.llm import LLMClient
from _1_800_operator.pipeline.meeting_record import MeetingRecord
from _1_800_operator.pipeline.providers import ProviderResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_client(responses=None):
    """Build an LLMClient with a mock provider and an in-memory MeetingRecord.

    responses: list of ProviderResponse returned in order from provider.complete.
               (Or a single ProviderResponse to always return.)
    """
    provider = MagicMock()
    if isinstance(responses, list):
        provider.complete.side_effect = responses
    elif responses is not None:
        provider.complete.return_value = responses
    record = MeetingRecord()  # in-memory
    client = LLMClient(provider, record=record)
    return client, provider, record


# ---------------------------------------------------------------------------
# Test 1: ask() — basic wiring
# ---------------------------------------------------------------------------

def test_ask_no_tools_calls_provider_once():
    """Plain ask returns text; provider.complete called once with system + tail messages."""
    client, provider, record = make_client(ProviderResponse(text="Hello back."))
    # The user turn is expected to already be in the record (ChatRunner appends first).
    record.append("Alice", "hey there")

    reply = client.ask("hey there")

    assert reply == "Hello back."
    provider.complete.assert_called_once()
    kwargs = provider.complete.call_args.kwargs
    # system is empty: post-S204, captions no longer enter the prompt (transcript
    # MCP delivers them on demand), so the SAFETY_RULES <spoken>-block guidance
    # has no input to protect against. claude_cli composes its own framework-level
    # pre-tool voice rule at spawn time regardless of what this layer passes.
    assert kwargs["system"] == ""
    assert kwargs["model"] == ""
    assert kwargs["max_tokens"] == config.MAX_TOKENS
    # Tail should be the single user message from the record
    msgs = kwargs["messages"]
    assert len(msgs) == 1 and msgs[0]["role"] == "user"
    assert msgs[0]["content"].startswith("Alice:")
    print("PASS  test_ask_no_tools_calls_provider_once")


# ---------------------------------------------------------------------------
# Test 2: _tail_messages shape
# ---------------------------------------------------------------------------

def test_tail_messages_shape():
    """Agent sender → assistant; user chat → 'first: text'; captions dropped.

    Captions reach inner-claude via the bundled transcript MCP server, not
    through the prompt tail. The 40-slot context budget goes 100% to chat.
    """
    client, _, record = make_client(ProviderResponse(text=""))
    agent = config.AGENT_NAME
    record.append("Alice Smith", "hello")
    record.append("Alice Smith", "and another")
    record.append(agent, "acknowledged")
    record.append("Bob Jones", "ambient talk", kind="caption")
    record.append("Bob Jones", "direct msg")
    msgs = client._tail_messages()

    # Agent mapped to assistant role
    assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0]["content"] == "acknowledged"

    # User chat messages render as "First: text"
    alice_msgs = [m for m in msgs if m["role"] == "user" and m["content"].startswith("Alice")]
    assert len(alice_msgs) == 2
    assert alice_msgs[0]["content"] == "Alice: hello"
    assert alice_msgs[1]["content"] == "Alice: and another"

    # Captions are skipped entirely — no <spoken> block reaches the prompt
    spoken_msgs = [m for m in msgs if "<spoken" in m.get("content", "")]
    assert len(spoken_msgs) == 0

    # Bob's chat message still renders normally
    bob_chat = [m for m in msgs if m["role"] == "user"
                and m["content"].startswith("Bob: direct msg")]
    assert len(bob_chat) == 1

    # Total: 2 alice chat + 1 agent + 1 bob chat = 4. Caption is dropped.
    assert len(msgs) == 4
    print("PASS  test_tail_messages_shape")


# ---------------------------------------------------------------------------
# Run all
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_ask_no_tools_calls_provider_once,
        test_tail_messages_shape,
    ]
    failures = []
    for t in tests:
        try:
            t()
        except Exception as e:
            import traceback
            print(f"FAIL  {t.__name__}: {e}")
            traceback.print_exc()
            failures.append(t.__name__)

    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
