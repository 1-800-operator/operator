"""
Test: Verify send_chat() and read_chat() work in a live Google Meet session.

Joins a meeting via MacOSAdapter, reads any existing messages, sends a test
message, waits, then reads again to confirm the round-trip works.

Usage:
    source venv/bin/activate
    python scripts/test_chat_io.py https://meet.google.com/xxx-yyyy-zzz

You should be in the meeting yourself. Try sending a chat message while
the script is running — it should show up in the second read.
"""
import sys
import os
import time

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
os.environ.setdefault("OPERATOR_BOT", "pm")

from _1_800_operator import config  # noqa: E402
from _1_800_operator.connectors.macos_adapter import MacOSAdapter  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/test_chat_io.py <meeting-url>")
        sys.exit(1)

    meeting_url = sys.argv[1]
    print(f"Meeting URL: {meeting_url}")

    adapter = MacOSAdapter()
    adapter.join(meeting_url)

    # Wait for the bot to actually be in the meeting
    print("Waiting for bot to join meeting...")
    adapter.join_status.ready.wait(timeout=30)
    if not adapter.join_status.success:
        print(f"FAILED to join: {adapter.join_status.failure_reason}")
        sys.exit(1)
    print("Joined meeting successfully.")

    # Give Meet a moment to settle after join
    print("Waiting 5s for meeting to settle...")
    time.sleep(5)

    # --- Test 1: read_chat() picks up existing messages ---
    print("\n=== TEST 1: Initial read_chat() ===")
    messages = adapter.read_chat()
    if messages:
        print(f"  Found {len(messages)} existing message(s):")
        for m in messages:
            print(f"    [{m['id'][-12:]}] {m['text']}")
    else:
        print("  No existing messages (expected if chat is empty).")

    # --- Test 2: send_chat() posts a message ---
    print("\n=== TEST 2: send_chat() ===")
    test_msg = "echo test from operator"
    print(f"  Sending: {test_msg!r}")
    adapter.send_chat(test_msg)
    print("  send_chat() returned (check meeting chat to confirm).")

    # --- Test 3: read_chat() picks up new messages ---
    print("\n=== TEST 3: Waiting 10s for new messages ===")
    print("  Send a message in the meeting chat now!")
    for i in range(5):
        time.sleep(2)
        new = adapter.read_chat()
        if new:
            print(f"  [{i*2+2}s] Got {len(new)} new message(s):")
            for m in new:
                print(f"    [{m['id'][-12:]}] {m['text']}")
        else:
            print(f"  [{i*2+2}s] No new messages yet...")

    # --- Done ---
    print("\n=== CLEANUP ===")
    adapter.leave()
    print("Left meeting. Done.")


if __name__ == "__main__":
    main()
