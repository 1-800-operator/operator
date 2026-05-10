"""
Unit tests for Component C — LLMClient.

Covers pipeline/llm.py:
  ask() — single provider.complete call with the new user turn forwarded
  as a one-element messages list.

Pre-14.22.5 LLMClient also built a chat-tail message list; provider
(claude_cli) only ever read messages[-1] because inner-claude rehydrates
prior conversation through `--resume`. Phase 14.22.5 deleted the dead
tail-building chain (_tail_messages, _build_messages, HISTORY_MESSAGES).

Pre-14.22.3.5 there was an `intro()` test here too; intro itself was
deleted in that audit step.

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


def make_client(responses=None):
    """Build an LLMClient with a mock provider and an in-memory MeetingRecord."""
    provider = MagicMock()
    if isinstance(responses, list):
        provider.complete.side_effect = responses
    elif responses is not None:
        provider.complete.return_value = responses
    record = MeetingRecord()
    client = LLMClient(provider, record=record)
    return client, provider, record


def test_ask_calls_provider_once_with_single_user_turn():
    """ask() forwards exactly one user-role message containing the new turn.

    Inner-claude carries prior conversation through --resume, so operator
    only forwards the new turn — no tail-building, no role mapping.
    """
    client, provider, _ = make_client(ProviderResponse(text="Hello back."))

    reply = client.ask("hey there")

    assert reply == "Hello back."
    provider.complete.assert_called_once()
    kwargs = provider.complete.call_args.kwargs
    assert kwargs["system"] == ""
    assert kwargs["model"] == ""
    assert kwargs["max_tokens"] == config.MAX_TOKENS
    msgs = kwargs["messages"]
    assert len(msgs) == 1
    assert msgs[0] == {"role": "user", "content": "hey there"}
    print("PASS  test_ask_calls_provider_once_with_single_user_turn")


if __name__ == "__main__":
    tests = [test_ask_calls_provider_once_with_single_user_turn]
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
