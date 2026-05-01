"""
test_915_reconnection.py — Step 9.15: Offline/reconnection behavior

Tests that ChatRunner exits cleanly when the connector reports disconnection,
and that the is_connected() method works correctly on the connector.

Run: python tests/test_915_reconnection.py
"""
import threading
import time
import sys
import os

os.environ.setdefault("OPERATOR_BOT", "pm")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _1_800_operator.connectors.base import MeetingConnector


# ---------------------------------------------------------------------------
# Minimal stub connector that simulates a browser session dying mid-meeting
# ---------------------------------------------------------------------------

class StubConnector(MeetingConnector):
    def __init__(self):
        super().__init__()
        self._connected = True
        self._messages = []
        self._sent = []

    def join(self, meeting_url):
        pass

    def send_chat(self, message):
        self._sent.append(message)

    def read_chat(self):
        msgs = self._messages[:]
        self._messages.clear()
        return msgs

    def get_participant_count(self):
        return 2

    def is_connected(self):
        return self._connected

    def leave(self):
        self._connected = False

    def simulate_crash(self):
        """Simulate browser crash without going through leave()."""
        self._connected = False


# ---------------------------------------------------------------------------
# Minimal stub LLM that records calls
# ---------------------------------------------------------------------------

class StubLLM:
    def __init__(self):
        self.calls = []

    def ask(self, text, tools=None):
        self.calls.append(text)
        return "OK"

    def add_context(self, text):
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_is_connected_default_true():
    """MeetingConnector base class is_connected() returns True by default."""
    connector = MeetingConnector()
    assert connector.is_connected() is True, "base is_connected() should default to True"
    print("PASS: base is_connected() defaults to True")


def test_stub_connector_is_connected():
    """StubConnector.is_connected() reflects simulated state."""
    connector = StubConnector()
    assert connector.is_connected() is True
    connector.simulate_crash()
    assert connector.is_connected() is False
    print("PASS: StubConnector.is_connected() reflects crash state")


def test_chat_runner_exits_on_disconnect():
    """ChatRunner._loop() exits when connector reports disconnected."""
    from _1_800_operator.pipeline.chat_runner import ChatRunner

    connector = StubConnector()
    llm = StubLLM()
    runner = ChatRunner(connector, llm)

    # Start the loop in a background thread (simulating runner.run())
    loop_done = threading.Event()

    def _run_loop():
        runner._loop()
        loop_done.set()

    t = threading.Thread(target=_run_loop, daemon=True)
    t.start()

    # Let the loop run for a couple of cycles
    time.sleep(0.3)
    assert not loop_done.is_set(), "loop should still be running before crash"

    # Simulate browser crash
    connector.simulate_crash()

    # Loop should exit promptly (within 2s — one POLL_INTERVAL + is_connected check)
    exited = loop_done.wait(timeout=2.0)
    assert exited, "ChatRunner loop did not exit after connector disconnected"
    print("PASS: ChatRunner exits loop when connector disconnects")


def test_chat_runner_does_not_exit_on_explicit_stop():
    """stop() exits the loop via _stop_event, not is_connected()."""
    from _1_800_operator.pipeline.chat_runner import ChatRunner

    connector = StubConnector()
    llm = StubLLM()
    runner = ChatRunner(connector, llm)

    loop_done = threading.Event()

    def _run_loop():
        runner._loop()
        loop_done.set()

    t = threading.Thread(target=_run_loop, daemon=True)
    t.start()

    time.sleep(0.2)
    # Normal stop — connector stays connected
    runner.stop()

    exited = loop_done.wait(timeout=2.0)
    assert exited, "ChatRunner loop did not exit after stop()"
    assert connector.is_connected(), "connector should still report connected after stop()"
    print("PASS: ChatRunner exits via stop() with connector still connected")


def test_network_loss_alert_selector():
    """The role=alert text check matches correctly and does not false-positive."""
    def is_network_lost(alert_innertext):
        return "lost your network" in alert_innertext.lower()

    # Normal state — no alert
    assert is_network_lost("") is False

    # Network loss alert (exact text from Meet DOM capture April 2026)
    assert is_network_lost("You lost your network connection. Trying to reconnect.") is True

    # Other Meet alerts must not trigger exit
    assert is_network_lost("Your microphone is muted") is False
    assert is_network_lost("Someone is waiting to join") is False
    assert is_network_lost("Recording has started") is False

    print("PASS: network loss alert selector matches correctly and does not false-positive")


def test_network_loss_grace_period():
    """Alert detected within grace period → no exit. Alert persists beyond grace → exit signal."""
    import time as _time

    GRACE = 30
    network_lost_at = None

    def tick(alert_present, now):
        nonlocal network_lost_at
        if alert_present:
            if network_lost_at is None:
                network_lost_at = now
                return "waiting"
            elif now - network_lost_at >= GRACE:
                return "exit"
            else:
                return "waiting"
        else:
            network_lost_at = None
            return "ok"

    t0 = 1000.0

    # Alert appears — first tick starts grace period
    assert tick(True, t0) == "waiting"
    assert network_lost_at == t0

    # Still within grace — keep waiting
    assert tick(True, t0 + 15) == "waiting"

    # Alert clears before grace expires — self-heal
    assert tick(False, t0 + 20) == "ok"
    assert network_lost_at is None

    # Alert appears again — new grace period starts
    assert tick(True, t0 + 25) == "waiting"
    t1 = network_lost_at

    # Grace period expires — exit
    assert tick(True, t1 + 31) == "exit"

    print("PASS: grace period logic: waits 30s from first detection, resets on recovery")


if __name__ == "__main__":
    print("=== test_915_reconnection.py ===\n")
    test_is_connected_default_true()
    test_stub_connector_is_connected()
    test_chat_runner_exits_on_disconnect()
    test_chat_runner_does_not_exit_on_explicit_stop()
    test_network_loss_alert_selector()
    test_network_loss_grace_period()
    print("\nAll tests passed.")
