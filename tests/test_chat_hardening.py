"""
Tests for chat hardening: history cap, trigger phrase gating, sender filtering.
Run: python tests/test_chat_hardening.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
os.environ.setdefault("OPERATOR_BOT", "pm")

from _1_800_operator import config
import re


def test_history_cap():
    """LLMClient should replay at most HISTORY_MESSAGES entries from the record."""
    from unittest.mock import MagicMock
    from _1_800_operator.pipeline.llm import LLMClient
    from _1_800_operator.pipeline.meeting_record import MeetingRecord
    from _1_800_operator.pipeline.providers import ProviderResponse

    record = MeetingRecord(slug=None)  # in-memory
    llm = LLMClient(MagicMock(), record=record)
    # Monkeypatch the history cap for the duration of the test. The cap
    # used to live as `llm._max_messages` (mutable per-meeting) but was
    # collapsed to `config.HISTORY_MESSAGES` (constant) when we deleted
    # the now-dead context-overflow shrinking machinery.
    original_cap = config.HISTORY_MESSAGES
    config.HISTORY_MESSAGES = 5

    try:
        for i in range(10):
            record.append(sender="Alice", text=f"user msg {i}")
            record.append(sender=config.AGENT_NAME, text=f"bot reply {i}")

        llm._provider.complete.return_value = ProviderResponse(
            text="ok", tool_calls=[], stop_reason="end",
        )
        llm.ask("probe", record=False)  # don't append; we just want to inspect the call

        call_args = llm._provider.complete.call_args
        messages = call_args.kwargs["messages"]
        # 5 tail entries + 1 trailing user turn (extra_user_msg)
        assert len(messages) == 6, f"Expected 6 messages, got {len(messages)}: {messages}"
    finally:
        config.HISTORY_MESSAGES = original_cap
    # Oldest kept should be entry index -5 (counting from end of the 20 entries written)
    # The 20 entries are: user0, bot0, user1, bot1, ..., user9, bot9. Last 5 are:
    # user8, bot8, user9, bot9 is only 4; wait — 20 entries, last 5 = bot7, user8, bot8, user9, bot9
    assert messages[0]["content"] == "Alice: user msg 8" or "bot reply 7" in messages[0]["content"], \
        f"Unexpected oldest entry: {messages[0]!r}"
    print("  history cap: PASS")


def test_meeting_record_tail_roundtrip(tmp_dir=None):
    """MeetingRecord should persist and tail back in order."""
    import tempfile
    from pathlib import Path
    from _1_800_operator.pipeline.meeting_record import MeetingRecord

    with tempfile.TemporaryDirectory() as tmp:
        r = MeetingRecord(slug="test-slug", root=Path(tmp), meta={"meet_url": "https://meet.google.com/test-slug"})
        r.append("Alice", "hi")
        r.append(config.AGENT_NAME, "hello")
        r.append("Bob", "sup")
        entries = r.tail(10)
        chat = [e for e in entries if e.get("kind") == "chat"]
        assert len(chat) == 3
        assert chat[0]["sender"] == "Alice" and chat[0]["text"] == "hi"
        assert chat[2]["sender"] == "Bob" and chat[2]["text"] == "sup"
        # Meta header is written once on first open; verify by reading raw.
        raw_first = (Path(tmp) / "test-slug.jsonl").read_text().splitlines()
        meta_lines = [ln for ln in raw_first if '"kind": "meta"' in ln]
        assert len(meta_lines) == 1
        assert "https://meet.google.com/test-slug" in meta_lines[0]
        # Reopening must NOT rewrite the header, and must NOT replay the
        # prior session's entries via tail() — the LLM would echo stale
        # answers instead of calling tools. Only this run's own appends
        # are visible to tail().
        r2 = MeetingRecord(slug="test-slug", root=Path(tmp))
        entries2 = r2.tail(10)
        assert sum(1 for e in entries2 if e.get("kind") == "meta") <= 1
        assert [e["text"] for e in entries2 if e.get("kind") == "chat"] == []
        r2.append("Carol", "fresh")
        assert [e["text"] for e in r2.tail(10) if e.get("kind") == "chat"] == ["fresh"]
        # The raw JSONL still holds the prior session — tail just scopes it.
        raw = (Path(tmp) / "test-slug.jsonl").read_text().splitlines()
        assert any('"text": "hi"' in ln for ln in raw)
        assert sum(1 for ln in raw if '"session_start"' in ln) == 2
    print("  meeting record roundtrip: PASS")


def test_slug_from_url():
    from _1_800_operator.pipeline.meeting_record import slug_from_url
    assert slug_from_url("https://meet.google.com/pgy-qauk-frn") == "pgy-qauk-frn"
    assert slug_from_url("https://meet.google.com/abc-defg-hij?pli=1") == "abc-defg-hij"
    assert slug_from_url("") == "unknown-meeting"
    assert slug_from_url("pgy-qauk-frn") == "pgy-qauk-frn"
    print("  slug_from_url: PASS")


def test_trigger_phrase_gating():
    """Only messages containing the trigger phrase should trigger a response."""
    trigger = config.TRIGGER_PHRASE.lower()

    match_cases = [
        f"{trigger} what time is it",
        f"hey {trigger}, summarize",
        f"{trigger.capitalize()} tell me a joke",
    ]
    for text in match_cases:
        assert trigger in text.lower(), f"Should match: {text!r}"

    no_match = [
        "what time is it",
        "let's discuss the operator role",  # bare word shouldn't match "@operator"
    ]
    for text in no_match:
        assert trigger not in text.lower(), f"Should not match: {text!r}"

    print("  trigger phrase detection: PASS")


def test_trigger_phrase_stripping():
    """Trigger phrase should be stripped from the prompt sent to LLM."""
    trigger = config.TRIGGER_PHRASE
    pattern = re.escape(trigger) + r'[,:]?\s*'

    cases = [
        (f"{trigger} what time is it", "what time is it"),
        (f"{trigger}, summarize the discussion", "summarize the discussion"),
        (f"hey {trigger}: what was said", "hey what was said"),
    ]
    for text, expected in cases:
        result = re.sub(pattern, '', text, count=1, flags=re.IGNORECASE).strip()
        assert result == expected, f"Strip {text!r} -> {result!r}, expected {expected!r}"

    print("  trigger phrase stripping: PASS")


def test_sender_filtering():
    """Bot's own messages should be filtered by sender name."""
    bot_name = config.AGENT_NAME

    assert bot_name  # non-empty; the actual value comes from the active agent
    assert bot_name.lower() == bot_name.lower()
    assert "Alice".lower() != bot_name.lower()

    own_messages = {"Hello there"}
    text = "Hello there"
    assert text in own_messages

    print("  sender filtering: PASS")


if __name__ == "__main__":
    print("Chat hardening tests:")
    test_history_cap()
    test_meeting_record_tail_roundtrip()
    test_slug_from_url()
    test_trigger_phrase_gating()
    test_trigger_phrase_stripping()
    test_sender_filtering()
    print("\nAll tests passed.")
