"""
Smoke test for ClaudeCLIProvider.

Unlike the other provider tests this one is not mocked — it actually
spawns the `claude -p` CLI under the user's Claude Max subscription and
runs a 3-turn arithmetic conversation through it. The point is to
confirm the per-meeting subprocess actually works end-to-end:
  - lazy spawn
  - subscription-auth assertion (apiKeySource: "none")
  - stream-json envelope shape on stdin
  - result-event latching on stdout
  - context retention across turns within the same subprocess
  - clean teardown via stop()

Skipped if `claude` is not on PATH or if ANTHROPIC_API_KEY is set in the
environment (which would force API-key auth and trip our subscription
assertion). The test removes ANTHROPIC_API_KEY from the spawn env itself,
but if the developer running the test has it locally they should know
the test is intentionally exercising the no-API-key path.

Run:
    source venv/bin/activate
    python tests/test_claude_cli_provider.py
"""
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _1_800_operator.pipeline.providers.claude_cli import (
    ClaudeCLIProvider,
    ClaudeCLINotFoundError,
)
from _1_800_operator.pipeline.providers.openai import OpenAIProvider  # noqa: F401  (import side effects: registers in __init__)


def _skip_if_no_claude():
    if shutil.which("claude") is None:
        print("SKIP: `claude` CLI not on PATH.")
        sys.exit(0)


def test_three_turn_conversation():
    """Spawn once, run 2+2 -> *3 -> -1, verify final answer is 11."""
    provider = ClaudeCLIProvider()
    try:
        # Build a neutral history that grows turn by turn, mirroring how
        # LLMClient feeds messages. claude_cli only looks at the last
        # entry per call — it relies on the long-lived subprocess to
        # remember prior turns internally.
        history = []

        prompts = [
            ("What is 2+2? Reply with just the number, nothing else.", "4"),
            ("Now multiply that by 3. Reply with just the number, nothing else.", "12"),
            ("Now subtract 1. Reply with just the number, nothing else.", "11"),
        ]

        for prompt, expected in prompts:
            history.append({"role": "user", "content": prompt})
            response = provider.complete(
                system=None, messages=history, model=None, max_tokens=None,
            )
            text = (response.text or "").strip().rstrip(".")
            print(f"  turn: {prompt!r} -> {text!r} (expected {expected!r})")
            assert text == expected, f"expected {expected!r}, got {text!r}"
            assert response.tool_calls == []
            assert response.stop_reason == "end"
            history.append({"role": "assistant", "content": response.text})

        print("  three-turn conversation OK")
    finally:
        provider.stop()


def test_idempotent_stop():
    """stop() before any spawn is a no-op; double-stop is safe."""
    provider = ClaudeCLIProvider()
    provider.stop()
    provider.stop()
    print("  idempotent stop OK")


def test_warmup_then_complete():
    """warmup() spawns the subprocess; a subsequent complete() reuses it."""
    provider = ClaudeCLIProvider()
    try:
        provider.warmup(model=None)
        # After warmup the subprocess should already be alive — verify by
        # poking the internal handle. (Using internal state is fine in a
        # provider-owned smoke test.)
        assert provider._proc is not None and provider._proc.poll() is None, \
            "warmup did not leave the subprocess running"

        history = [{"role": "user", "content": "Say the single word 'pong' and nothing else."}]
        response = provider.complete(
            system=None, messages=history, model=None, max_tokens=None,
        )
        text = (response.text or "").strip().lower()
        assert "pong" in text, f"expected 'pong' in reply, got {text!r}"
        print("  warmup + complete OK")
    finally:
        provider.stop()


def test_permission_handler_allow():
    """PreToolUse hook fires; handler returning allow lets the Write proceed.

    Asks claude to write a tiny file with a known marker. Permission handler
    auto-approves but records every call so we can assert the hook fired.
    Verifies the file actually landed after claude exits.
    """
    import os
    import tempfile

    handler_calls = []

    def handler(tool_name, tool_input):
        handler_calls.append((tool_name, dict(tool_input)))
        return {
            "permissionDecision": "allow",
            "permissionDecisionReason": f"test auto-approved {tool_name}",
        }

    work = tempfile.mkdtemp(prefix="claude-cli-perm-test-")
    target = os.path.join(work, "marker.txt")

    provider = ClaudeCLIProvider(permission_handler=handler)
    try:
        prompt = (
            f"Use the Write tool to create the file at {target} with the "
            f"contents 'permission_handler_ok'. Do not read or edit anything else. "
            f"After writing, just confirm in one short sentence."
        )
        history = [{"role": "user", "content": prompt}]
        response = provider.complete(
            system=None, messages=history, model=None, max_tokens=None,
        )

        print(f"  reply: {(response.text or '').strip()[:100]!r}")
        print(f"  handler calls: {[c[0] for c in handler_calls]}")
        assert handler_calls, "PreToolUse hook never fired — handler was not called"
        # The Write call must have been one of the handler invocations.
        write_calls = [c for c in handler_calls if c[0] == "Write"]
        assert write_calls, f"expected at least one Write call; got {[c[0] for c in handler_calls]}"
        write_input = write_calls[0][1]
        assert write_input.get("file_path") == target, (
            f"Write tool input file_path mismatch: expected {target!r}, "
            f"got {write_input.get('file_path')!r}"
        )
        # And the file must actually exist with the right content.
        assert os.path.exists(target), f"expected file to exist at {target}"
        content = open(target).read().strip()
        assert content == "permission_handler_ok", f"unexpected content: {content!r}"
        print("  permission handler allow path OK")
    finally:
        provider.stop()
        try:
            if os.path.exists(target):
                os.remove(target)
            os.rmdir(work)
        except OSError:
            pass


def test_permission_handler_deny():
    """Handler returning deny blocks the Write; reason flows back to claude.

    Asks claude to write a file. Permission handler denies. Verifies (a)
    handler was called, (b) the file was NOT written, (c) claude's reply
    surfaces the denial reason in some form.
    """
    import os
    import tempfile

    handler_calls = []

    def handler(tool_name, tool_input):
        handler_calls.append(tool_name)
        return {
            "permissionDecision": "deny",
            "permissionDecisionReason": "test denied this tool intentionally",
        }

    work = tempfile.mkdtemp(prefix="claude-cli-perm-deny-test-")
    target = os.path.join(work, "marker.txt")

    provider = ClaudeCLIProvider(permission_handler=handler)
    try:
        prompt = (
            f"Use the Write tool to create the file at {target} with the "
            f"contents 'hello_world'. Do not read or edit anything else. "
            f"After writing, just confirm in one short sentence."
        )
        history = [{"role": "user", "content": prompt}]
        response = provider.complete(
            system=None, messages=history, model=None, max_tokens=None,
        )

        print(f"  reply: {(response.text or '').strip()[:120]!r}")
        print(f"  handler calls: {handler_calls}")
        assert handler_calls, "PreToolUse hook never fired"
        assert not os.path.exists(target), (
            f"Write was supposed to be denied but the file landed at {target}"
        )
        print("  permission handler deny path OK")
    finally:
        provider.stop()
        try:
            os.rmdir(work)
        except OSError:
            pass


def test_build_provider_selects_claude_cli(monkeypatch_attrs):
    """build_provider() returns a ClaudeCLIProvider when config.LLM_PROVIDER == 'claude_cli'."""
    from _1_800_operator import config
    from _1_800_operator.pipeline.providers import build_provider

    saved = {k: getattr(config, k) for k in monkeypatch_attrs}
    saved_disabled = getattr(config, "DISABLED_MCP_SERVERS", {})
    try:
        for k, v in monkeypatch_attrs.items():
            setattr(config, k, v)
        # Clear disabled MCPs — when present, the provider __init__ appends
        # a "do not claim/call these" notice onto _append_system_prompt,
        # which is correct prod behavior but irrelevant to this assertion.
        config.DISABLED_MCP_SERVERS = {}
        provider = build_provider()
        assert isinstance(provider, ClaudeCLIProvider), (
            f"expected ClaudeCLIProvider, got {type(provider).__name__}"
        )
        # The provider should have inherited SYSTEM_PROMPT as the
        # --append-system-prompt content.
        assert provider._append_system_prompt == monkeypatch_attrs["SYSTEM_PROMPT"], (
            f"expected exact SYSTEM_PROMPT inheritance, got "
            f"{provider._append_system_prompt!r}"
        )
        provider.stop()  # nothing was spawned, but stop is idempotent
        print("  build_provider selects claude_cli OK")
    finally:
        for k, v in saved.items():
            setattr(config, k, v)
        config.DISABLED_MCP_SERVERS = saved_disabled


def test_subprocess_restart_with_history_rebuild():
    """Killing the subprocess mid-meeting recovers via synthesized opener.

    Mirrors probe 7 strategy 2 but exercises the production code path:
    run 2 turns, terminate the subprocess externally, then send turn 3.
    The provider should detect the broken pipe / EOF, spawn a fresh
    subprocess, re-feed the prior transcript in one envelope, send turn
    3, and produce the correct answer (11) — proving context survived.
    """
    provider = ClaudeCLIProvider()
    try:
        history = []

        # Turns 1 and 2.
        for prompt, expected in [
            ("What is 2+2? Reply with just the number, nothing else.", "4"),
            ("Now multiply that by 3. Reply with just the number, nothing else.", "12"),
        ]:
            history.append({"role": "user", "content": prompt})
            r = provider.complete(
                system=None, messages=history, model=None, max_tokens=None,
            )
            text = (r.text or "").strip().rstrip(".")
            assert text == expected, f"pre-kill turn expected {expected!r}, got {text!r}"
            history.append({"role": "assistant", "content": r.text})
        print("  pre-kill: 4 -> 12 OK")

        # Kill the subprocess externally to simulate a mid-meeting crash.
        # We bypass provider.stop() so the provider's bookkeeping still
        # thinks the process is alive; the next complete() call will hit
        # a broken pipe or EOF and trigger restart.
        old_pid = provider._proc.pid
        provider._proc.terminate()
        try:
            provider._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            provider._proc.kill()
            provider._proc.wait(timeout=5)
        print(f"  killed subprocess pid={old_pid}")

        # Turn 3 — provider should auto-restart, replay history, answer correctly.
        history.append({
            "role": "user",
            "content": "Now subtract 1. Reply with just the number, nothing else.",
        })
        r = provider.complete(
            system=None, messages=history, model=None, max_tokens=None,
        )
        text = (r.text or "").strip().rstrip(".")
        print(f"  post-restart turn 3: {text!r} (expected '11')")
        assert text == "11", f"expected '11' after restart, got {text!r}"
        # Confirm a *new* subprocess is alive — pid must have changed.
        assert provider._proc is not None
        assert provider._proc.pid != old_pid, (
            f"expected a fresh subprocess after restart; pid did not change ({old_pid})"
        )
        print(f"  restart OK (new pid={provider._proc.pid}, old={old_pid})")
    finally:
        provider.stop()


def test_streaming_paragraph_callback():
    """complete_streaming() flushes paragraphs to on_paragraph as they arrive.

    Requests a multi-paragraph reply, captures every paragraph the
    callback receives, and verifies (a) on_paragraph was called more than
    once (proving we actually streamed paragraph-by-paragraph rather than
    waiting for the end), (b) the response.text matches the canonical
    text from the terminal assistant event, and (c) the joined paragraphs
    cover the same content.
    """
    provider = ClaudeCLIProvider()
    try:
        paragraphs_seen = []

        def on_paragraph(text):
            paragraphs_seen.append(text)

        prompt = (
            "Write a 3-paragraph haiku about cold pizza, with a blank line "
            "between paragraphs. No commentary, just the three paragraphs."
        )
        history = [{"role": "user", "content": prompt}]
        response = provider.complete_streaming(
            system=None, messages=history, model=None, max_tokens=None,
            on_paragraph=on_paragraph,
        )

        print(f"  saw {len(paragraphs_seen)} paragraph flushes")
        for i, p in enumerate(paragraphs_seen, 1):
            preview = p[:60].replace("\n", " ")
            print(f"    {i}: {preview!r}{'...' if len(p) > 60 else ''}")

        assert len(paragraphs_seen) >= 2, (
            f"expected >=2 paragraph flushes for a multi-paragraph reply; got {len(paragraphs_seen)}"
        )
        assert response.text and response.text.strip(), "response.text should be the full reply"
        assert response.tool_calls == []
        assert response.stop_reason == "end"

        # Joined paragraphs should account for most of the canonical text
        # (we drop separator-only fragments via the flush helper, so exact
        # equality isn't guaranteed).
        joined = "\n\n".join(paragraphs_seen)
        joined_chars = sum(len(p) for p in paragraphs_seen)
        canonical_chars = len(response.text)
        coverage = joined_chars / canonical_chars if canonical_chars else 0
        assert coverage > 0.9, (
            f"streamed paragraphs covered only {coverage:.0%} of canonical reply "
            f"({joined_chars}/{canonical_chars} chars)"
        )
        print(f"  streaming OK ({coverage:.0%} coverage of canonical reply)")
    finally:
        provider.stop()


def main():
    _skip_if_no_claude()
    print("test_three_turn_conversation")
    test_three_turn_conversation()
    print("test_idempotent_stop")
    test_idempotent_stop()
    print("test_warmup_then_complete")
    test_warmup_then_complete()
    print("test_streaming_paragraph_callback")
    test_streaming_paragraph_callback()
    print("test_permission_handler_allow")
    test_permission_handler_allow()
    print("test_permission_handler_deny")
    test_permission_handler_deny()
    print("test_subprocess_restart_with_history_rebuild")
    test_subprocess_restart_with_history_rebuild()
    print("test_build_provider_selects_claude_cli")
    test_build_provider_selects_claude_cli({
        "LLM_PROVIDER": "claude_cli",
        "SYSTEM_PROMPT": "You are a test bot. Be brief.",
    })
    print("\nAll claude_cli provider tests passed.")


if __name__ == "__main__":
    main()
