"""
Tests for CodexElicitationChatHandler — chat round-trip, decision mapping,
"yes always" → amendment-form decision, and the formatting helpers.

Uses fake connector + runner stubs so no playwright / real Meet needed.

Usage:
    python tests/test_codex_elicitation_handler.py
"""
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("OPERATOR_BOT", "pm")

from _1_800_operator import config  # noqa: E402
from _1_800_operator.pipeline.codex_elicitation_handler import (  # noqa: E402
    CodexElicitationChatHandler,
    _format_confirmation,
    _render_command,
)
from _1_800_operator.pipeline.confirmation import is_yes, is_yes_always  # noqa: E402


class FakeConnector:
    def __init__(self):
        self._queue: list[list[dict]] = []
        self._lock = threading.Lock()

    def queue_messages(self, msgs):
        with self._lock:
            self._queue.append(list(msgs))

    def read_chat(self):
        with self._lock:
            if not self._queue:
                return []
            return self._queue.pop(0)


class FakeRunner:
    """Mimics the parts of ChatRunner the handler touches."""

    def __init__(self):
        self._send_log: list[tuple[str, str]] = []
        self._seen_ids: set[str] = set()
        self._own_messages: set[str] = set()

    def _send(self, text, kind=None):
        self._send_log.append((text, kind or ""))


def _check(label, cond, detail=""):
    mark = "✓" if cond else "✗"
    print(f"  {mark} {label}{(' — ' + detail) if detail else ''}")
    if not cond:
        raise AssertionError(label)


def _make_params(cmd_body="echo hi > /tmp/x.txt", cwd="/tmp"):
    argv = ["/bin/zsh", "-lc", cmd_body]
    return {
        "message": f"Allow Codex to run `{cmd_body}` in `{cwd}`?",
        "codex_elicitation": "exec-approval",
        "codex_command": argv,
        "codex_cwd": cwd,
        "codex_parsed_cmd": [{"type": "unknown", "cmd": cmd_body}],
        "threadId": "thread-test",
    }


def test_is_yes_always_helpers():
    print("\n1. is_yes_always semantics")
    _check("'yes always' is yes_always", is_yes_always("yes always"))
    _check("'always' alone is yes_always", is_yes_always("always"))
    _check("'forever' is yes_always", is_yes_always("forever"))
    _check("'ok permanent' is yes_always", is_yes_always("ok permanent"))
    _check("'every time' is yes_always", is_yes_always("every time"))
    _check("plain 'yes' is NOT yes_always", not is_yes_always("yes"))
    _check("plain 'ok' is NOT yes_always", not is_yes_always("ok"))
    _check("'no always' fails negation gate", not is_yes_always("no always"))
    _check("plain 'yes' IS yes (sanity)", is_yes("yes"))


def test_render_command_strips_zsh_wrapper():
    print("\n2. _render_command strips /bin/zsh -lc wrapper")
    s = _render_command(["/bin/zsh", "-lc", "echo hi"])
    _check("inner command surfaces", s == "echo hi")
    _check("plain argv joined when no wrapper",
           _render_command(["python", "-c", "print(1)"]) == 'python -c print(1)')
    _check("non-list returns repr", _render_command("not-a-list") == repr("not-a-list"))
    long_body = "a" * 1000
    rendered = _render_command(["/bin/zsh", "-lc", long_body])
    _check("long bodies head-tail truncate", "…" in rendered and len(rendered) < 500)


def test_format_confirmation_voice_modes():
    print("\n3. _format_confirmation respects VOICE")
    params = _make_params()
    saved = getattr(config, "VOICE", "plain")
    try:
        config.VOICE = "plain"
        plain = _format_confirmation(params)
        _check("plain mode shows just the command",
               "echo hi > /tmp/x.txt" in plain and "/bin/zsh" not in plain)
        _check("plain mode includes cwd", "/tmp" in plain)

        config.VOICE = "technical"
        tech = _format_confirmation(params)
        _check("technical mode keeps full argv",
               "/bin/zsh" in tech and "argv:" in tech)
        _check("technical mode also shows cwd", "/tmp" in tech)
    finally:
        config.VOICE = saved


def test_yes_reply_returns_approved():
    print("\n4. Plain 'yes' reply → {'decision': 'approved'}")
    handler = CodexElicitationChatHandler(FakeConnector(), FakeRunner())
    handler._connector.queue_messages([{"id": "m1", "text": "yes", "sender": "alice"}])
    decision = handler("codex", _make_params())
    _check("decision is single-shot approved", decision == {"decision": "approved"})


def test_yes_always_reply_returns_amendment():
    print("\n5. 'yes always' reply → amendment-form decision with full argv")
    handler = CodexElicitationChatHandler(FakeConnector(), FakeRunner())
    handler._connector.queue_messages([{"id": "m2", "text": "yes always", "sender": "alice"}])
    decision = handler("codex", _make_params(cmd_body="echo abc > /tmp/y.txt"))
    expected_argv = ["/bin/zsh", "-lc", "echo abc > /tmp/y.txt"]
    _check("decision is dict-form amendment",
           isinstance(decision.get("decision"), dict)
           and "approved_execpolicy_amendment" in decision["decision"])
    inner = decision["decision"]["approved_execpolicy_amendment"]
    _check("amendment carries the full argv",
           inner["proposed_execpolicy_amendment"] == expected_argv)


def test_no_reply_returns_abort():
    print("\n6. 'no' reply → abort")
    handler = CodexElicitationChatHandler(FakeConnector(), FakeRunner())
    handler._connector.queue_messages([{"id": "m3", "text": "no", "sender": "alice"}])
    decision = handler("codex", _make_params())
    _check("decision is abort", decision == {"decision": "abort"})


def test_send_failure_aborts():
    print("\n7. Failure to post the chat prompt aborts cleanly")
    runner = FakeRunner()

    def boom(text, kind=None):
        raise RuntimeError("connector down")

    runner._send = boom
    handler = CodexElicitationChatHandler(FakeConnector(), runner)
    decision = handler("codex", _make_params())
    _check("decision is abort", decision == {"decision": "abort"})


def test_chat_seen_ids_claimed():
    print("\n8. Claimed message id added to runner._seen_ids")
    handler = CodexElicitationChatHandler(FakeConnector(), FakeRunner())
    handler._connector.queue_messages([{"id": "m4", "text": "ok", "sender": "alice"}])
    handler("codex", _make_params())
    _check("id claimed", "m4" in handler._runner._seen_ids)


def test_own_message_skipped():
    print("\n9. Bot-own message and AGENT_NAME-sender are skipped, then user reply consumed")
    handler = CodexElicitationChatHandler(FakeConnector(), FakeRunner())
    # Pretend we previously sent a confirmation prompt.
    bot_text = "Run? `echo z > /tmp/z.txt` in `/tmp`\nOK?"
    handler._runner._own_messages.add(bot_text)
    handler._connector.queue_messages([
        # Echo of our own confirmation (no sender, exact text match)
        {"id": "m5a", "text": bot_text, "sender": ""},
        # Same-named bot — also skipped
        {"id": "m5b", "text": "approve", "sender": config.AGENT_NAME},
        # Real user reply
        {"id": "m5c", "text": "yes", "sender": "alice"},
    ])
    decision = handler("codex", _make_params())
    _check("user reply consumed", decision == {"decision": "approved"})
    _check("user message id claimed", "m5c" in handler._runner._seen_ids)


def main():
    print("=" * 50)
    print("CodexElicitationChatHandler")
    print("=" * 50)
    failed = 0
    for fn in [
        test_is_yes_always_helpers,
        test_render_command_strips_zsh_wrapper,
        test_format_confirmation_voice_modes,
        test_yes_reply_returns_approved,
        test_yes_always_reply_returns_amendment,
        test_no_reply_returns_abort,
        test_send_failure_aborts,
        test_chat_seen_ids_claimed,
        test_own_message_skipped,
    ]:
        try:
            fn()
        except AssertionError as e:
            print(f"  FAILED: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {e}")
            failed += 1
    print("\n" + "=" * 50)
    if failed == 0:
        print("All tests passed!")
        return 0
    print(f"{failed} test(s) failed")
    return 1


if __name__ == "__main__":
    sys.exit(main())
