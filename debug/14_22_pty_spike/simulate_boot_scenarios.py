"""
Simulate two user-facing boot scenarios, end to end through a REAL
ChatRunner + LLMClient, with a FakeConnector that prints every line the
meeting room would see (`ROOM SEES >>`).

  Scenario 1 — @mention arrives while inner-claude is still booting.
    A real fresh inner-claude spawn, but a ClaudeCLIProvider subclass
    injects a delay into _send_briefing so the boot is slow. The
    @mention is fired ~2s in, racing the boot. Expected: the room sees
    "still getting set up — give me a moment…", then the real reply —
    one clean reply, no turn-0 leak.

  Scenario 2 — boot exceeds the briefing ceiling (the >300s abort).
    No real spawn. A subclass overrides pre_warm to sleep briefly (a
    stand-in for the 300s ceiling) and then record the EXACT
    ClaudeCLIProtocolError that the real _send_briefing raises on a
    ceiling abort — same class, same message shape. The user-facing
    behavior is fully determined by that exception flowing through
    ChatRunner's real exception->chat mapping. Expected: the room sees
    "still getting set up…", then operator's failure line.

Scenario 1 spawns a real `claude` (costs subscription tokens, ~30s).
Scenario 2 spawns nothing and is instant.

Run from the repo root:
    source venv/bin/activate
    python debug/14_22_pty_spike/simulate_boot_scenarios.py [1|2|all]
"""
import logging
import sys
import threading
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO / "src"))

from _1_800_operator.connectors.base import MeetingConnector  # noqa: E402
from _1_800_operator.pipeline.chat_runner import ChatRunner  # noqa: E402
from _1_800_operator.pipeline.llm import LLMClient  # noqa: E402
from _1_800_operator.pipeline.meeting_record import MeetingRecord  # noqa: E402
from _1_800_operator.pipeline.providers.claude_cli import (  # noqa: E402
    ClaudeCLIProvider,
    ClaudeCLIProtocolError,
)


def _banner(text):
    print(f"\n{'=' * 70}\n{text}\n{'=' * 70}")


# --- fake connector — captures what the room would see ----------------


class FakeConnector(MeetingConnector):
    """Implements just enough of MeetingConnector for _dispatch_user_message
    to run. Every outbound line is printed as `ROOM SEES >>` so the
    user-facing surface is visible without a real Meet."""

    def __init__(self):
        super().__init__()
        self._n = 0

    def _show(self, message):
        self._n += 1
        for line in message.splitlines() or [""]:
            print(f"  ROOM SEES >> {line}")
        return f"fake-msg-{self._n}"

    def join(self, meeting_url):
        return None

    def send_chat(self, message):
        return self._show(message)

    def send_chat_raw(self, message):
        # _narrate_failure posts via the raw path (no [🤖 Claude] prefix).
        return self._show(message)

    def read_chat(self):
        return []

    def get_participant_count(self):
        return 2

    def get_participant_names(self):
        return ["Tester", "Operator"]

    def is_connected(self):
        return True

    def leave(self):
        pass


# --- provider subclasses — one per scenario ---------------------------


class SlowBriefingProvider(ClaudeCLIProvider):
    """Real spawn, real briefing — but with a delay injected before the
    briefing round-trip so the boot is reliably slow enough for an
    @mention to race it."""

    BRIEFING_DELAY = 25.0

    def _send_briefing(self):
        print(f"  [SIM] injecting a {self.BRIEFING_DELAY:.0f}s delay before the "
              f"briefing round-trip (simulates a slow boot)")
        time.sleep(self.BRIEFING_DELAY)
        return super()._send_briefing()


class CeilingAbortProvider(ClaudeCLIProvider):
    """No real spawn. pre_warm sleeps briefly (a stand-in for the real
    boot ceiling) and then leaves the provider in EXACTLY the state a
    real ceiling abort leaves it: _spawn_exc set to the
    ClaudeCLIProtocolError _send_briefing raises, _last_failure_phase
    tagged 'boot', _boot_done set. This mirrors the real pre_warm
    contract (record the failure, signal boot-done) without waiting the
    full ceiling or spawning claude. _pty_tail is also overridden to
    return a plausible synthetic tail so the failure snapshot doctor
    captures looks like a real one (real PTY tail would carry inner-
    claude's actual TUI bytes at the moment the wedge was detected)."""

    BOOT_DELAY = 10.0

    # Plausible-looking synthetic PTY tail — what inner-claude would
    # have printed by the moment a real boot ceiling tripped. Lets the
    # doctor smoke-test see a non-empty `pty_tail` block in the
    # snapshot. Mirrors the shape of real Claude Code TUI output: a
    # welcome line, session-loading note, then MCP attach messages
    # trailing off into the wedge.
    _SYNTHETIC_PTY_TAIL = (
        "Welcome to Claude Code\n"
        "Resuming session 24f38462-b1ab-4fc8-a938-78c0c5260d78\n"
        "Loading 8243 messages from session transcript…\n"
        "Connecting MCP servers: github, transcript, Linear, Gmail, Drive\n"
        "  [MCP] github: connected\n"
        "  [MCP] transcript: connected\n"
        "  [MCP] Linear: connecting…\n"
    )

    def pre_warm(self):
        if self._stopping:
            return
        with self._spawn_lock:
            if self._spawn_in_progress:
                return
            self._spawn_in_progress = True
            self._boot_done.clear()
            self._spawn_exc = None
        try:
            print(f"  [SIM] simulating a boot that exceeds the boot ceiling "
                  f"({self.BOOT_DELAY:.0f}s stand-in for {180}s)")
            time.sleep(self.BOOT_DELAY)
            self._spawn_exc = ClaudeCLIProtocolError(
                "briefing (turn 0) produced no reply before the 180s boot "
                "ceiling — inner-claude is wedged, or the operator-plugin "
                "Stop hook is not writing replies.jsonl."
            )
        finally:
            with self._spawn_lock:
                self._spawn_in_progress = False
                self._boot_done.set()

    def _pty_tail(self, n_bytes=2000):
        # Real provider returns the captured PTY bytes; we substitute a
        # synthetic plausible tail so the failure snapshot is non-empty
        # without spawning claude. Parent's snapshot_failure_context()
        # consumes this; no need to override the snapshot method.
        return self._SYNTHETIC_PTY_TAIL[-n_bytes:]


# --- harness ----------------------------------------------------------


def _make_runner(provider):
    record = MeetingRecord(
        slug="sim-boot-scenarios",
        meta={"meet_url": "https://meet.google.com/sim-boot", "mode": "slip"},
    )
    llm = LLMClient(provider)
    llm.set_record(record)
    runner = ChatRunner(FakeConnector(), llm, record)
    runner._wire_provider()
    return runner


def _fire_mention_after(runner, delay, text):
    """Run pre_warm has already started; wait `delay` then dispatch an
    @mention on this thread (blocks inside _run_turn until the turn
    resolves — exactly what the polling loop does)."""
    time.sleep(delay)
    print(f"  [SIM] t+{delay:.0f}s — Tester posts: {text!r}")
    runner._dispatch_user_message(text, sender="Tester")


def scenario_1():
    _banner("SCENARIO 1 — @mention arrives while inner-claude is booting")
    provider = SlowBriefingProvider(cwd=str(_REPO))  # fresh spawn, no resume
    runner = _make_runner(provider)
    print("  [SIM] starting pre_warm on a background thread…")
    threading.Thread(target=provider.pre_warm, daemon=True).start()
    try:
        _fire_mention_after(
            runner, 2.0,
            "@claude say hello in one short sentence",
        )
    finally:
        provider.stop()
    print("\n  EXPECT: a 'still getting set up…' line, then ONE real reply — "
          "no 'No response requested.' leak, no duplicate.")


def scenario_2():
    _banner("SCENARIO 2 — boot exceeds the briefing ceiling (the >300s abort)")
    provider = CeilingAbortProvider(cwd=str(_REPO))
    runner = _make_runner(provider)
    print("  [SIM] starting pre_warm on a background thread…")
    threading.Thread(target=provider.pre_warm, daemon=True).start()
    try:
        _fire_mention_after(
            runner, 2.0,
            "@claude what's on the agenda?",
        )
    finally:
        provider.stop()
    print("\n  EXPECT: 'still getting set up…', then ONE retry attempt "
          "(second '[SIM] simulating…' line), then the unavailable line "
          "('claude is unavailable — run /operator:doctor to see what's "
          "wrong').")


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which not in ("1", "2", "all"):
        print("usage: simulate_boot_scenarios.py [1|2|all]")
        sys.exit(2)
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    if which in ("1", "all"):
        scenario_1()
    if which in ("2", "all"):
        scenario_2()
    print()


if __name__ == "__main__":
    main()
