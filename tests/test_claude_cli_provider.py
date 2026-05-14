"""
Mock tests for ClaudeCLIProvider — interactive PTY-driven `claude` + hooks.

The provider was rewritten in Phase 14.22 (S227-S228) from per-@mention
`claude -p` shellouts to ONE long-lived interactive `claude` subprocess
per meeting, driven over a PTY. Turn boundaries come from the
operator-plugin's Stop hook (a new JSONL row in replies.jsonl); the reply
*text* is tailed live out of the Claude Code transcript JSONL. See the
`claude_cli` module docstring and `debug/14_22_pty_spike/DECISION.md`.

These tests are fully mocked — they do NOT spawn the real `claude` CLI.
A live smoke test of the new architecture needs the operator-plugin hook
scaffolding installed and a real PTY spawn; that path is exercised by the
DECISION.md 20-25 integration pass, not by this standalone script. What
this file pins:

  - the naked-spawn invariant (`_build_cmd` carries no harness-shaped flags)
  - replies.jsonl tailing (turn-boundary detection, timeout, teardown bail)
  - transcript tailing (real-time assistant-text narration, seek+buffer)
  - Stop-payload field extraction (wrapped + bare shapes)
  - foreign-hook detection (the "Stop hook feedback:" marker)
  - a full mocked turn through `_run_turn` — briefing-free, fake send,
    fake hook + transcript writes — covering streaming paragraphs,
    notices, and the respawn-after-crash detection path.

Run:
    source venv/bin/activate
    python tests/test_claude_cli_provider.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _1_800_operator.pipeline.providers.claude_cli import (
    ClaudeCLIProvider,
    ClaudeCLIProtocolError,
)
from _1_800_operator.pipeline.providers.base import ProviderResponse


# --- fakes ------------------------------------------------------------


class _FakeProc:
    """Stand-in for the inner-claude subprocess.

    `poll()` returns None while alive, the return code once dead — the
    exact contract `_run_turn` / `_wait_for_next_reply` check.
    """

    def __init__(self, alive=True, pid=999999, returncode=0):
        self._alive = alive
        self.pid = pid
        self.returncode = returncode

    def poll(self):
        return None if self._alive else self.returncode

    def wait(self, timeout=None):
        self._alive = False
        return self.returncode


def _new_provider(tmp):
    """Construct a provider with its session dir under `tmp`, no spawn."""
    return ClaudeCLIProvider(cwd=tmp, session_dir=Path(tmp) / "session")


def _assistant_event(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _tool_use_event(name="Read"):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": {}}]}}


def _user_event(content):
    return {"type": "user", "message": {"content": content}}


def _stop_row(text, session_id="sess-abc", transcript_path="/tmp/transcript.jsonl"):
    """A Stop hook payload row as the operator-plugin's stop.sh writes it."""
    return {
        "ts": time.time(),
        "kind": "stop",
        "input": {
            "hook_event_name": "Stop",
            "last_assistant_message": text,
            "session_id": session_id,
            "transcript_path": transcript_path,
        },
    }


def _write_jsonl(path, rows):
    with open(path, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


# --- spawn-shape tests ------------------------------------------------


def test_build_cmd_naked_spawn_invariant():
    """_build_cmd carries --dangerously-skip-permissions and nothing
    harness-shaped. The naked-spawn invariant constrains spawn *flags*:
    no -p, no --append-system-prompt, no --mcp-config. See
    memory/project_anthropic_detection_vector.md.
    """
    if shutil.which("claude") is None:
        print("  SKIP: `claude` not on PATH (cmd[0] resolution needs it)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        provider = _new_provider(tmp)
        cmd = provider._build_cmd()
        assert "--dangerously-skip-permissions" in cmd, (
            f"--dangerously-skip-permissions is unconditional now: {cmd}"
        )
        for flag in ("-p", "--print", "--append-system-prompt", "--mcp-config",
                     "--input-format", "--output-format"):
            assert flag not in cmd, f"naked-spawn invariant violated: {flag} in {cmd}"
        assert "--resume" not in cmd, f"no resume id → no --resume: {cmd}"
    print("  naked-spawn invariant (skip-permissions, no -p / harness flags) OK")


def test_build_cmd_resume_session_id():
    """A constructor-supplied resume_session_id rides on spawn as
    --resume <id> — the path the plugin slash command uses to bridge an
    existing Claude Code session into the meeting.
    """
    if shutil.which("claude") is None:
        print("  SKIP: `claude` not on PATH")
        return
    with tempfile.TemporaryDirectory() as tmp:
        provider = ClaudeCLIProvider(
            cwd=tmp, session_dir=Path(tmp) / "s", resume_session_id="bridged-id-7",
        )
        cmd = provider._build_cmd()
        assert "--resume" in cmd, f"resume_session_id should pass --resume: {cmd}"
        assert cmd[cmd.index("--resume") + 1] == "bridged-id-7"
    print("  --resume <id> on resume_session_id OK")


def test_build_provider_returns_claude_cli():
    """build_provider() returns a ClaudeCLIProvider in v1 and forwards
    resume_session_id into it.
    """
    from _1_800_operator.pipeline.providers import build_provider

    with tempfile.TemporaryDirectory() as tmp:
        provider = build_provider(session_dir=Path(tmp) / "a")
        assert isinstance(provider, ClaudeCLIProvider), (
            f"expected ClaudeCLIProvider, got {type(provider).__name__}"
        )
        assert provider._resume_session_id is None

        bridged = build_provider(
            resume_session_id="plugin-bridged-id", session_dir=Path(tmp) / "b",
        )
        assert bridged._resume_session_id == "plugin-bridged-id"
    print("  build_provider returns ClaudeCLIProvider, forwards resume id OK")


def test_construction_creates_session_dir():
    """Construction creates the session dir and pins the file paths the
    plugin hooks + provider agree on.
    """
    with tempfile.TemporaryDirectory() as tmp:
        provider = _new_provider(tmp)
        assert provider._session_dir.is_dir(), "session dir created on construction"
        assert provider._replies_path == provider._session_dir / "replies.jsonl"
        assert provider._ready_flag_path == provider._session_dir / "ready.flag"
        assert provider._transcript_path is None, "transcript path unknown until turn 0"
    print("  construction creates session dir + pins paths OK")


# --- lifecycle stubs --------------------------------------------------


def test_idempotent_stop():
    """stop() before any spawn is a no-op; double-stop is safe."""
    with tempfile.TemporaryDirectory() as tmp:
        provider = _new_provider(tmp)
        provider.stop()
        provider.stop()
        assert provider._stopping is True
    print("  idempotent stop OK")


def test_warmup_is_noop():
    """warmup() is a no-op — pre_warm() is the meaningful spawn for this
    provider. Still callable for the ABC contract.
    """
    with tempfile.TemporaryDirectory() as tmp:
        provider = _new_provider(tmp)
        assert provider.warmup(model=None) is None
    print("  warmup no-op OK")


def test_run_turn_rejects_bad_messages():
    """_run_turn requires a non-empty history whose last message is a
    user turn — claude owns its own conversation memory, operator only
    forwards the latest user turn.
    """
    with tempfile.TemporaryDirectory() as tmp:
        provider = _new_provider(tmp)
        try:
            provider.complete(system=None, messages=[], model=None, max_tokens=None)
            assert False, "empty messages should raise"
        except ValueError:
            pass
        try:
            provider.complete(
                system=None,
                messages=[{"role": "assistant", "content": "hi"}],
                model=None, max_tokens=None,
            )
            assert False, "non-user last message should raise"
        except ValueError:
            pass

        provider._stopping = True
        try:
            provider.complete(
                system=None,
                messages=[{"role": "user", "content": "hi"}],
                model=None, max_tokens=None,
            )
            assert False, "complete() while stopping should raise"
        except ClaudeCLIProtocolError:
            pass
    print("  _run_turn rejects empty / non-user / stopping OK")


def test_spawn_failure_surfaces_detail():
    """When the subprocess can't be spawned, _run_turn raises
    ClaudeCLIProtocolError carrying the stored _spawn_exc detail rather
    than the misleading generic message.
    """
    with tempfile.TemporaryDirectory() as tmp:
        provider = _new_provider(tmp)
        provider._spawn_exc = RuntimeError("claude binary vanished")
        # pre_warm is a no-op here: leave _proc None to simulate a failed spawn.
        provider.pre_warm = lambda: None
        try:
            provider.complete(
                system=None,
                messages=[{"role": "user", "content": "hi"}],
                model=None, max_tokens=None,
            )
            assert False, "should raise when _proc never comes up"
        except ClaudeCLIProtocolError as exc:
            assert "claude binary vanished" in str(exc), (
                f"spawn failure should surface the real cause: {exc}"
            )
    print("  spawn failure surfaces _spawn_exc detail OK")


# --- replies.jsonl tailing -------------------------------------------


def test_count_and_read_replies():
    """_count_replies counts rows (0 if the file is absent);
    _read_reply_at parses the row at a given index.
    """
    with tempfile.TemporaryDirectory() as tmp:
        provider = _new_provider(tmp)
        assert provider._count_replies() == 0, "absent file → 0"

        _write_jsonl(provider._replies_path, [_stop_row("first"), _stop_row("second")])
        assert provider._count_replies() == 2
        row0 = provider._read_reply_at(0)
        assert provider._extract_assistant_text(row0) == "first"
        row1 = provider._read_reply_at(1)
        assert provider._extract_assistant_text(row1) == "second"
    print("  _count_replies / _read_reply_at OK")


def test_wait_for_next_reply_picks_up_new_row():
    """_wait_for_next_reply returns the parsed Stop payload as soon as a
    row past prev_count lands — and fires the tick callback each poll.
    """
    with tempfile.TemporaryDirectory() as tmp:
        provider = _new_provider(tmp)
        provider._proc = _FakeProc(alive=True)
        ticks = []
        provider.set_tick_callback(lambda: ticks.append(1))

        prev = provider._count_replies()  # 0

        def _writer():
            time.sleep(0.3)
            _write_jsonl(provider._replies_path, [_stop_row("the reply")])

        threading.Thread(target=_writer, daemon=True).start()
        reply = provider._wait_for_next_reply(prev, timeout=5.0)
        assert reply is not None, "should pick up the row the writer thread appended"
        assert provider._extract_assistant_text(reply) == "the reply"
        assert len(ticks) >= 1, "tick callback should fire on every poll"
    print("  _wait_for_next_reply picks up a new row + fires tick OK")


def test_wait_for_next_reply_times_out():
    """No new row before the deadline → None (not an exception)."""
    with tempfile.TemporaryDirectory() as tmp:
        provider = _new_provider(tmp)
        provider._proc = _FakeProc(alive=True)
        t0 = time.monotonic()
        reply = provider._wait_for_next_reply(0, timeout=0.5)
        assert reply is None, "timeout should return None"
        assert time.monotonic() - t0 >= 0.5, "should actually wait the timeout"
    print("  _wait_for_next_reply times out → None OK")


def test_wait_for_next_reply_bails_on_stopping():
    """The teardown flag short-circuits the tail loop with None — an
    orderly shutdown, not a crash. (No _proc.poll() alarm raised.)
    """
    with tempfile.TemporaryDirectory() as tmp:
        provider = _new_provider(tmp)
        provider._proc = _FakeProc(alive=True)
        provider._stopping = True
        reply = provider._wait_for_next_reply(0, timeout=5.0)
        assert reply is None, "stopping flag should bail immediately with None"
    print("  _wait_for_next_reply bails on _stopping OK")


def test_wait_for_next_reply_raises_on_dead_proc():
    """If the subprocess dies mid-tail, the loop raises a protocol error
    carrying the return code — the crash path, distinct from a timeout.
    """
    with tempfile.TemporaryDirectory() as tmp:
        provider = _new_provider(tmp)
        provider._proc = _FakeProc(alive=False, returncode=137)
        try:
            provider._wait_for_next_reply(0, timeout=5.0)
            assert False, "dead proc should raise"
        except ClaudeCLIProtocolError as exc:
            assert "137" in str(exc), f"return code should surface: {exc}"
    print("  _wait_for_next_reply raises on dead proc OK")


# --- Stop-payload extraction -----------------------------------------


def test_extract_helpers_tolerate_both_shapes():
    """The extract helpers read both the operator-plugin's wrapped
    {ts, kind, input: ...} row and a bare Claude Code hook payload.
    """
    wrapped = _stop_row("hello", session_id="sid-1", transcript_path="/tmp/t.jsonl")
    bare = wrapped["input"]

    for shape, label in ((wrapped, "wrapped"), (bare, "bare")):
        assert ClaudeCLIProvider._extract_assistant_text(shape) == "hello", label
        assert ClaudeCLIProvider._extract_session_id(shape) == "sid-1", label
        tp = ClaudeCLIProvider._extract_transcript_path(shape)
        assert tp == Path("/tmp/t.jsonl"), label

    # Garbage in → None out, no exception.
    assert ClaudeCLIProvider._extract_assistant_text("not a dict") is None
    assert ClaudeCLIProvider._extract_session_id({}) is None
    assert ClaudeCLIProvider._extract_transcript_path({"input": {}}) is None
    print("  Stop-payload extraction tolerates wrapped + bare shapes OK")


# --- transcript tailing ----------------------------------------------


def test_assistant_texts_filters_blocks():
    """_assistant_texts pulls assistant text blocks in order, skips
    tool_use blocks and non-assistant events, tolerates bare-string
    content, drops empties.
    """
    events = [
        _assistant_event("let me grab that file"),
        _tool_use_event("Read"),
        _user_event("some user turn"),
        {"type": "assistant", "message": {"content": "bare string content"}},
        _assistant_event("   "),  # whitespace-only → dropped
        _assistant_event("here's what I found"),
    ]
    texts = ClaudeCLIProvider._assistant_texts(events)
    assert texts == [
        "let me grab that file",
        "bare string content",
        "here's what I found",
    ], texts
    print("  _assistant_texts filters tool_use / non-assistant / empty OK")


def test_read_transcript_lines_seek_and_buffer():
    """_read_transcript_lines reads only past `offset`, returns parsed
    events, and holds a partial trailing line in the buffer for the next
    call — same seek+buffer discipline replies tailing uses.
    """
    with tempfile.TemporaryDirectory() as tmp:
        provider = _new_provider(tmp)
        tpath = Path(tmp) / "transcript.jsonl"
        provider._transcript_path = tpath

        # Two complete lines + a partial third (no trailing newline).
        full = json.dumps(_assistant_event("one")) + "\n"
        full += json.dumps(_assistant_event("two")) + "\n"
        partial = json.dumps(_assistant_event("three"))[:20]
        tpath.write_bytes((full + partial).encode("utf-8"))

        offset, buf, events = provider._read_transcript_lines(0, b"")
        assert len(events) == 2, f"two complete lines parsed, got {len(events)}"
        assert ClaudeCLIProvider._assistant_texts(events) == ["one", "two"]
        assert buf, "partial trailing line held in buffer"

        # Append the rest of line 3 + a line 4; the buffered partial completes.
        rest = json.dumps(_assistant_event("three"))[20:] + "\n"
        rest += json.dumps(_assistant_event("four")) + "\n"
        with open(tpath, "ab") as f:
            f.write(rest.encode("utf-8"))

        offset, buf, events = provider._read_transcript_lines(offset, buf)
        assert ClaudeCLIProvider._assistant_texts(events) == ["three", "four"], events
        assert buf == b"", "buffer drained once the line completed"
    print("  _read_transcript_lines seek + partial-line buffering OK")


def test_has_foreign_hook_feedback():
    """_has_foreign_hook_feedback fires only on a user-role event
    carrying the literal 'Stop hook feedback:' marker (string or block
    content) — the signature of a foreign Stop hook running decision=block.
    """
    clean = [
        _assistant_event("Stop hook feedback: this is the assistant talking, not a hook"),
        _user_event("normal user turn"),
    ]
    assert ClaudeCLIProvider._has_foreign_hook_feedback(clean) is False, (
        "marker in an assistant block is not foreign-hook feedback"
    )

    string_form = [_user_event("Stop hook feedback: go do something else")]
    assert ClaudeCLIProvider._has_foreign_hook_feedback(string_form) is True

    block_form = [_user_event([{"type": "text", "text": "Stop hook feedback: redirect"}])]
    assert ClaudeCLIProvider._has_foreign_hook_feedback(block_form) is True
    print("  _has_foreign_hook_feedback detects the marker on user turns only OK")


# --- full mocked turn -------------------------------------------------


def _drive_turn(provider, user_text, transcript_events, stop_text,
                foreign_hook_event=None, on_paragraph=None):
    """Run one _run_turn with a fake send that simulates inner-claude:
    on send, the transcript gains `transcript_events` and replies.jsonl
    gains the Stop row. Returns the ProviderResponse.
    """
    tpath = provider._transcript_path

    def _fake_send(msg):
        events = list(transcript_events)
        if foreign_hook_event is not None:
            events.append(foreign_hook_event)
        with open(tpath, "a", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")
        _write_jsonl(
            provider._replies_path,
            [_stop_row(stop_text, transcript_path=str(tpath))],
        )

    provider._send_message = _fake_send
    messages = [{"role": "user", "content": user_text}]
    if on_paragraph is not None:
        return provider.complete_streaming(
            system=None, messages=messages, model=None, max_tokens=None,
            on_paragraph=on_paragraph,
        )
    return provider.complete(system=None, messages=messages, model=None, max_tokens=None)


def test_full_turn_streams_transcript_paragraphs():
    """A full mocked turn: _run_turn tails the transcript, flushes each
    assistant text block to on_paragraph in real time, and returns a
    ProviderResponse whose .text is the joined narration.
    """
    with tempfile.TemporaryDirectory() as tmp:
        provider = _new_provider(tmp)
        provider._proc = _FakeProc(alive=True)
        provider._transcript_path = Path(tmp) / "transcript.jsonl"
        provider._transcript_path.touch()

        paragraphs = []
        resp = _drive_turn(
            provider,
            user_text="read mvp.md and summarize",
            transcript_events=[
                _assistant_event("let me grab that file"),
                _tool_use_event("Read"),
                _assistant_event("here's the summary: it's a meeting bot"),
            ],
            stop_text="here's the summary: it's a meeting bot",
            on_paragraph=lambda p: paragraphs.append(p),
        )
        assert isinstance(resp, ProviderResponse)
        assert resp.stop_reason == "end"
        assert resp.tool_calls == []
        assert paragraphs == [
            "let me grab that file",
            "here's the summary: it's a meeting bot",
        ], paragraphs
        assert resp.text == (
            "let me grab that file\n\n"
            "here's the summary: it's a meeting bot"
        ), resp.text
        assert resp.notices == [], "no foreign hook → no notices"
        # session id captured off the Stop payload.
        assert provider._captured_session_id == "sess-abc"
    print("  full turn streams transcript paragraphs + builds .text OK")


def test_full_turn_stop_text_backstop():
    """If the final block never came through the transcript tail (no
    transcript path, or a write race), the Stop payload's
    last_assistant_message is posted so the turn isn't silent.
    """
    with tempfile.TemporaryDirectory() as tmp:
        provider = _new_provider(tmp)
        provider._proc = _FakeProc(alive=True)
        # No transcript path set and none captured → transcript tail is inert.
        provider._transcript_path = None

        def _fake_send(msg):
            _write_jsonl(
                provider._replies_path,
                [_stop_row("the only thing the room should see", transcript_path="")],
            )

        provider._send_message = _fake_send
        paragraphs = []
        resp = provider.complete_streaming(
            system=None,
            messages=[{"role": "user", "content": "hi"}],
            model=None, max_tokens=None,
            on_paragraph=lambda p: paragraphs.append(p),
        )
        assert resp.text == "the only thing the room should see"
        assert paragraphs == ["the only thing the room should see"], paragraphs
    print("  full turn falls back to Stop-payload text when transcript is silent OK")


def test_full_turn_foreign_hook_notice():
    """A 'Stop hook feedback:' user event in the turn's transcript →
    ProviderResponse.notices carries the heads-up for the room.
    """
    with tempfile.TemporaryDirectory() as tmp:
        provider = _new_provider(tmp)
        provider._proc = _FakeProc(alive=True)
        provider._transcript_path = Path(tmp) / "transcript.jsonl"
        provider._transcript_path.touch()

        resp = _drive_turn(
            provider,
            user_text="do the thing",
            transcript_events=[_assistant_event("on it")],
            stop_text="on it",
            foreign_hook_event=_user_event("Stop hook feedback: actually do something else"),
        )
        assert len(resp.notices) == 1, f"expected one notice, got {resp.notices}"
        assert "hook" in resp.notices[0].lower()
        assert resp.text == "on it", "the reply text itself is unaffected"
    print("  full turn surfaces foreign-hook notice OK")


def test_run_turn_respawns_after_crash():
    """If the prior subprocess died, _run_turn tears it down and respawns
    via pre_warm before running the turn — a crashed inner-claude on turn
    N shouldn't break turn N+1.
    """
    with tempfile.TemporaryDirectory() as tmp:
        provider = _new_provider(tmp)
        provider._proc = _FakeProc(alive=False, returncode=1)  # crashed
        provider._transcript_path = Path(tmp) / "transcript.jsonl"
        provider._transcript_path.touch()

        respawned = []

        def _fake_pre_warm():
            respawned.append(True)
            provider._proc = _FakeProc(alive=True)

        provider.pre_warm = _fake_pre_warm

        resp = _drive_turn(
            provider,
            user_text="still there?",
            transcript_events=[_assistant_event("yep, back up")],
            stop_text="yep, back up",
        )
        assert respawned, "a dead _proc should trigger a pre_warm respawn"
        assert resp.text == "yep, back up"
        assert provider._proc.poll() is None, "respawned proc should be alive"
    print("  _run_turn respawns after a crashed inner-claude OK")


# --- runner -----------------------------------------------------------


def main():
    tests = [
        test_build_cmd_naked_spawn_invariant,
        test_build_cmd_resume_session_id,
        test_build_provider_returns_claude_cli,
        test_construction_creates_session_dir,
        test_idempotent_stop,
        test_warmup_is_noop,
        test_run_turn_rejects_bad_messages,
        test_spawn_failure_surfaces_detail,
        test_count_and_read_replies,
        test_wait_for_next_reply_picks_up_new_row,
        test_wait_for_next_reply_times_out,
        test_wait_for_next_reply_bails_on_stopping,
        test_wait_for_next_reply_raises_on_dead_proc,
        test_extract_helpers_tolerate_both_shapes,
        test_assistant_texts_filters_blocks,
        test_read_transcript_lines_seek_and_buffer,
        test_has_foreign_hook_feedback,
        test_full_turn_streams_transcript_paragraphs,
        test_full_turn_stop_text_backstop,
        test_full_turn_foreign_hook_notice,
        test_run_turn_respawns_after_crash,
    ]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} claude_cli provider tests passed.")


if __name__ == "__main__":
    main()
