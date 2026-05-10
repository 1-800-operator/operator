"""
Smoke + mock tests for ClaudeCLIProvider (per-@mention shellouts).

The provider was rewritten in Phase 14.22.3 (S211) from a long-lived
per-meeting subprocess to per-@mention spawn-and-exit shellouts. Each
turn launches a fresh `claude -p` with `--resume <session_id>` (after
the first turn captures the session id from `system_init`), drains the
stream until `result`, and exits. There is no persistent subprocess,
no `--append-system-prompt`, no `--mcp-config` tempfile. See
`memory/project_anthropic_detection_vector.md` for the architecture
constraint driving the shape.

The smoke tests are not mocked — they spawn the real `claude -p` CLI
under the user's Claude Max subscription. Skipped if `claude` is not
on PATH.

Run:
    source venv/bin/activate
    python tests/test_claude_cli_provider.py
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _1_800_operator.pipeline.providers.claude_cli import (
    ClaudeCLIProvider,
)


def _skip_if_no_claude():
    if shutil.which("claude") is None:
        print("SKIP: `claude` CLI not on PATH.")
        sys.exit(0)


def test_three_turn_conversation():
    """Three @mentions in a row exercise session continuity via --resume.

    Spawn-and-exit per turn: each `complete()` call launches its own
    subprocess. After turn 1 captures session_id from system_init,
    turns 2-3 spawn with `--resume <captured-id>` so claude rehydrates
    the prior message history from its on-disk session store. End
    result (2+2 -> *3 -> -1) should still be 11.
    """
    provider = ClaudeCLIProvider()
    history = []

    prompts = [
        ("What is 2+2? Reply with just the number, nothing else.", "4"),
        ("Now multiply that by 3. Reply with just the number, nothing else.", "12"),
        ("Now subtract 1. Reply with just the number, nothing else.", "11"),
    ]

    captured_session_id = None
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
        # After turn 1, session_id should be captured and re-used.
        if captured_session_id is None:
            captured_session_id = provider._session_id
            assert captured_session_id, "session_id should be captured on first turn"
        else:
            assert provider._session_id == captured_session_id, (
                f"session_id should survive across turns; expected "
                f"{captured_session_id} got {provider._session_id}"
            )

    print("  three-turn conversation OK")


def test_idempotent_stop():
    """stop() before any spawn is a no-op; double-stop is safe."""
    provider = ClaudeCLIProvider()
    provider.stop()
    provider.stop()
    print("  idempotent stop OK")


def test_warmup_is_noop():
    """warmup() is a no-op for the per-@mention provider — there is no
    persistent subprocess to pre-spawn. Still callable for ABC contract.
    """
    provider = ClaudeCLIProvider()
    result = provider.warmup(model=None)
    assert result is None
    print("  warmup no-op OK")


def test_build_provider_returns_claude_cli():
    """build_provider() returns a ClaudeCLIProvider in v1 (claude is the only brain).
    `resume_session_id=None` by default; the value is forwarded into the provider."""
    from _1_800_operator.pipeline.providers import build_provider

    provider = build_provider()
    assert isinstance(provider, ClaudeCLIProvider), (
        f"expected ClaudeCLIProvider, got {type(provider).__name__}"
    )
    assert provider._session_id is None

    bridged = build_provider(resume_session_id="abc-123-bridged-session")
    assert bridged._session_id == "abc-123-bridged-session", (
        "resume_session_id should pre-populate the provider's _session_id"
    )
    print("  build_provider returns ClaudeCLIProvider OK")


def test_streaming_paragraph_callback():
    """complete_streaming() flushes paragraphs to on_paragraph as they arrive.

    Multi-paragraph reply, paragraphs captured by on_paragraph, full
    response.text matches canonical text from terminal assistant event.
    """
    provider = ClaudeCLIProvider()
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
    joined_chars = sum(len(p) for p in paragraphs_seen)
    canonical_chars = len(response.text)
    coverage = joined_chars / canonical_chars if canonical_chars else 0
    assert coverage > 0.9, (
        f"streamed paragraphs covered only {coverage:.0%} of canonical reply"
    )
    print(f"  streaming OK ({coverage:.0%} coverage of canonical reply)")


def test_first_spawn_omits_resume_subsequent_spawns_pass_resume():
    """Mock-only: pin the cmd-array shape across two consecutive turns.

    First @mention without resume_session_id pre-population → spawn
    omits --resume. After the test fakes a session_id capture (what
    happens when the system_init event fires), the next spawn carries
    --resume <captured>.

    This pins the contract that future refactors can't drop the
    --resume flag without failing this test (which would silently
    regress session continuity across @mentions).
    """
    from _1_800_operator.pipeline.providers import claude_cli as cc_mod

    provider = ClaudeCLIProvider()

    # First spawn: no session_id, no --resume.
    cmd1 = provider._build_cmd()
    assert "--resume" not in cmd1, (
        f"first @mention (no captured session_id) should not pass --resume: {cmd1}"
    )

    # Simulate apiKeySource validation succeeding and session_id capture.
    provider._session_id = "abc-123-fake-session-id"

    # Subsequent spawn: --resume <captured-id>.
    cmd2 = provider._build_cmd()
    assert "--resume" in cmd2, (
        f"subsequent @mention should pass --resume: {cmd2}"
    )
    idx = cmd2.index("--resume")
    assert cmd2[idx + 1] == "abc-123-fake-session-id", (
        f"expected --resume abc-123-fake-session-id, got {cmd2[idx + 1]}"
    )

    # Confirm the naked-spawn invariant — no harness-shaped flags
    # snuck back in. These are the smoking-gun spawn-signature flags
    # that 14.22.3 explicitly stripped.
    for flag in ("--append-system-prompt", "--mcp-config"):
        assert flag not in cmd1, f"naked-spawn invariant violated in cmd1: {flag} in {cmd1}"
        assert flag not in cmd2, f"naked-spawn invariant violated in cmd2: {flag} in {cmd2}"

    print("  first spawn omits --resume; subsequent spawns pass --resume <id> OK")
    print("  naked-spawn invariant (no --append-system-prompt, no --mcp-config) OK")


def test_resume_session_pre_population_from_constructor():
    """When constructed with resume_session_id, the very first spawn
    passes --resume <id>. This is the path the plugin's slash command
    uses to bridge an existing Claude Code session into the meeting
    via `--resume-session ${CLAUDE_SESSION_ID}`.
    """
    provider = ClaudeCLIProvider(resume_session_id="plugin-bridged-id")
    cmd = provider._build_cmd()
    assert "--resume" in cmd, (
        f"first @mention with pre-populated session_id should pass --resume: {cmd}"
    )
    idx = cmd.index("--resume")
    assert cmd[idx + 1] == "plugin-bridged-id"
    print("  --resume-session pre-population OK")


def main():
    print("test_first_spawn_omits_resume_subsequent_spawns_pass_resume")
    test_first_spawn_omits_resume_subsequent_spawns_pass_resume()
    print("test_resume_session_pre_population_from_constructor")
    test_resume_session_pre_population_from_constructor()
    print("test_idempotent_stop")
    test_idempotent_stop()
    print("test_warmup_is_noop")
    test_warmup_is_noop()
    _skip_if_no_claude()
    print("test_build_provider_returns_claude_cli")
    test_build_provider_returns_claude_cli()
    print("test_three_turn_conversation")
    test_three_turn_conversation()
    print("test_streaming_paragraph_callback")
    test_streaming_paragraph_callback()
    print("\nAll claude_cli provider tests passed.")


if __name__ == "__main__":
    main()
