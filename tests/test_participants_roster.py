"""
Tests for the participant roster file written by ChatRunner and read
by the transcript MCP's list_participants tool.

What this exercises:
  - ChatRunner._refresh_roster_file writes currently_present and the
    cumulative attended union to ~/.operator/.current_meeting_participants.json
  - The bot's own display name is excluded from both lists
  - Attended grows monotonically — someone leaving doesn't shrink it
  - The transcript MCP's list_participants reads the file correctly
    (present, attended, freshness)
  - Empty-state prose when no file exists

The chat_runner test injects a fake connector with controllable
get_participant_names/get_self_name. The MCP test points the module's
PARTICIPANTS_FILE at a tmp path.
"""
import importlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _1_800_operator import config
from _1_800_operator.pipeline.chat_runner import ChatRunner


class RosterConnector:
    """Connector stub with controllable participant_names/self_name."""

    def __init__(self, names=None, self_name="Operator-Bot"):
        self._names = list(names or [])
        self._self = self_name
        self.sent: list[str] = []
        self.chat_messages: list[dict] = []
        self.join_status = None

    def send_chat(self, text):
        self.sent.append(text)
        return f"id-{len(self.sent)}"

    def read_chat(self):
        return []

    def is_connected(self):
        return True

    def get_participant_count(self):
        return len(self._names)

    def get_participant_names(self):
        return list(self._names)

    def get_self_name(self):
        return self._self


class StubLLM:
    def __init__(self):
        self._provider = None

    def set_record(self, r):
        pass


def make_runner_with_roster_path(connector, tmp_path):
    """Builds a ChatRunner and points the config'd roster path at tmp_path."""
    config.CURRENT_MEETING_PARTICIPANTS_PATH = str(tmp_path)
    return ChatRunner(connector, StubLLM(), meeting_record=None)


# ---- ChatRunner-side tests --------------------------------------------------

def test_roster_writer_excludes_self_and_writes_file():
    with tempfile.TemporaryDirectory() as tmp:
        roster_path = Path(tmp) / ".current_meeting_participants.json"
        conn = RosterConnector(
            names=["Operator-Bot", "Alice", "Bob"],
            self_name="Operator-Bot",
        )
        runner = make_runner_with_roster_path(conn, roster_path)
        runner._refresh_roster_file()
        assert roster_path.exists()
        data = json.loads(roster_path.read_text())
        # Bot is filtered out of currently_present.
        assert "Operator-Bot" not in data["currently_present"]
        assert set(data["currently_present"]) == {"Alice", "Bob"}
        # Attended mirrors present on first tick.
        assert set(data["attended"]) == {"Alice", "Bob"}
        assert data["self_name"] == "Operator-Bot"
    print("  writer excludes bot, writes present + attended: OK")


def test_attended_is_cumulative_when_someone_leaves():
    with tempfile.TemporaryDirectory() as tmp:
        roster_path = Path(tmp) / ".current_meeting_participants.json"
        conn = RosterConnector(
            names=["Operator-Bot", "Alice", "Bob", "Charlie"],
            self_name="Operator-Bot",
        )
        runner = make_runner_with_roster_path(conn, roster_path)
        runner._refresh_roster_file()
        # Charlie leaves.
        conn._names = ["Operator-Bot", "Alice", "Bob"]
        runner._refresh_roster_file()
        data = json.loads(roster_path.read_text())
        assert set(data["currently_present"]) == {"Alice", "Bob"}
        # Charlie persists in attended even though they left.
        assert set(data["attended"]) == {"Alice", "Bob", "Charlie"}
    print("  attended grows monotonically (Charlie left, still in attended): OK")


def test_late_joiner_is_added_to_both():
    with tempfile.TemporaryDirectory() as tmp:
        roster_path = Path(tmp) / ".current_meeting_participants.json"
        conn = RosterConnector(names=["Operator-Bot", "Alice"], self_name="Operator-Bot")
        runner = make_runner_with_roster_path(conn, roster_path)
        runner._refresh_roster_file()
        # Bob shows up late.
        conn._names = ["Operator-Bot", "Alice", "Bob"]
        runner._refresh_roster_file()
        data = json.loads(roster_path.read_text())
        assert "Bob" in data["currently_present"]
        assert "Bob" in data["attended"]
        assert set(data["attended"]) == {"Alice", "Bob"}
    print("  late joiner enters present + attended: OK")


def test_writer_handles_connector_failure_silently():
    """If get_participant_names raises, we log and skip — must not
    affect the auto-leave path that shares the tick."""
    with tempfile.TemporaryDirectory() as tmp:
        roster_path = Path(tmp) / ".current_meeting_participants.json"
        class BrokenConnector(RosterConnector):
            def get_participant_names(self):
                raise RuntimeError("DOM not ready")
        conn = BrokenConnector(self_name="Operator-Bot")
        runner = make_runner_with_roster_path(conn, roster_path)
        runner._refresh_roster_file()  # no crash
        # And the file is not written.
        assert not roster_path.exists()
    print("  connector failure on names → silent no-op, no file: OK")


# ---- MCP-side tests ---------------------------------------------------------

def test_mcp_list_participants_reads_file_correctly():
    """list_participants formats the roster as plain text."""
    from _1_800_operator.mcp_servers import transcript_server as ts_mod

    with tempfile.TemporaryDirectory() as tmp:
        roster_path = Path(tmp) / ".current_meeting_participants.json"
        payload = {
            "currently_present": ["Alice", "Bob"],
            "attended": ["Alice", "Bob", "Charlie"],
            "self_name": "Operator-Bot",
            "updated_at": time.time() - 5,
        }
        roster_path.write_text(json.dumps(payload))

        # Point the module's PARTICIPANTS_FILE at our temp file.
        orig = ts_mod.PARTICIPANTS_FILE
        try:
            ts_mod.PARTICIPANTS_FILE = roster_path
            result = ts_mod.list_participants()
        finally:
            ts_mod.PARTICIPANTS_FILE = orig

        assert "Currently in the meeting (2):" in result
        assert "- Alice" in result
        assert "- Bob" in result
        # Cumulative section appears because attended != currently.
        assert "Attended at some point (3):" in result
        assert "Charlie" in result
        assert "(left)" in result
        # Bot name disclosed for context.
        assert "Operator-Bot" in result
        # Freshness annotation present.
        assert "refreshed" in result and "s ago)" in result
    print("  list_participants formats present + attended + freshness: OK")


def test_mcp_list_participants_empty_state():
    """No file → friendly empty-state prose, not an exception."""
    from _1_800_operator.mcp_servers import transcript_server as ts_mod
    orig = ts_mod.PARTICIPANTS_FILE
    try:
        ts_mod.PARTICIPANTS_FILE = Path("/nonexistent/.current_meeting_participants.json")
        result = ts_mod.list_participants()
    finally:
        ts_mod.PARTICIPANTS_FILE = orig
    assert "No participant roster" in result
    print("  empty state (no file): friendly prose, no crash: OK")


def test_mcp_list_participants_no_drift_when_present_equals_attended():
    """When nobody has left, the 'Attended at some point' section is
    suppressed — keeps output tight."""
    from _1_800_operator.mcp_servers import transcript_server as ts_mod
    with tempfile.TemporaryDirectory() as tmp:
        roster_path = Path(tmp) / ".current_meeting_participants.json"
        payload = {
            "currently_present": ["Alice", "Bob"],
            "attended": ["Alice", "Bob"],
            "self_name": "Operator-Bot",
            "updated_at": time.time(),
        }
        roster_path.write_text(json.dumps(payload))
        orig = ts_mod.PARTICIPANTS_FILE
        try:
            ts_mod.PARTICIPANTS_FILE = roster_path
            result = ts_mod.list_participants()
        finally:
            ts_mod.PARTICIPANTS_FILE = orig
    assert "Currently in the meeting" in result
    assert "Attended at some point" not in result, result
    print("  no drift between present + attended → cumulative section omitted: OK")


if __name__ == "__main__":
    print("Participant roster tests:")
    test_roster_writer_excludes_self_and_writes_file()
    test_attended_is_cumulative_when_someone_leaves()
    test_late_joiner_is_added_to_both()
    test_writer_handles_connector_failure_silently()
    test_mcp_list_participants_reads_file_correctly()
    test_mcp_list_participants_empty_state()
    test_mcp_list_participants_no_drift_when_present_equals_attended()
    print("\nAll 7 participant roster tests passed.")
