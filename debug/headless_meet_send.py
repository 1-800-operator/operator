"""Headless Meet probe — full join + send chat.

Builds on headless_meet_join.py: after joining, opens the chat panel,
types a test message, sends it, and screenshots the chat panel state.

Captures:
  1. headless_send_pre.png   — pre-join screen
  2. headless_send_chat.png  — chat panel open, message visible

Usage:
    source venv/bin/activate
    python debug/headless_meet_send.py <meet-url>
"""

import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

PROFILE = Path.home() / ".operator" / "browser_profile"
OUT = Path(__file__).parent
PRE = OUT / "headless_send_pre.png"
CHAT = OUT / "headless_send_chat.png"

MESSAGE = "headless test from bot"


def shot(page, dest: Path, label: str) -> None:
    page.screenshot(path=str(dest), full_page=True)
    print(f"  {label}: {dest}")


def main(url: str) -> None:
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            headless=True,
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()

        print(f"navigating to {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(8000)
        shot(page, PRE, "pre-join")

        try:
            page.get_by_text("Continue without microphone and camera").click(timeout=5000)
            print("  dismissed mic/cam modal")
            page.wait_for_timeout(1500)
        except Exception as e:
            print(f"  WARN: modal dismiss failed: {e}")

        try:
            page.get_by_role("button", name="Join now").click(timeout=5000)
            print("  clicked Join now — waiting 15s for admission")
        except Exception as e:
            print(f"  WARN: Join now failed: {e}")
        page.wait_for_timeout(15000)

        # Open chat panel
        try:
            page.get_by_role("button", name="Chat with everyone").click(timeout=5000)
            print("  opened chat panel")
            page.locator('textarea[aria-label="Send a message"]').wait_for(
                state="visible", timeout=5000
            )
        except Exception as e:
            print(f"  WARN: chat open failed: {e}")
            shot(page, CHAT, "chat (failed)")
            ctx.close()
            return

        # Send the test message
        try:
            box = page.locator('textarea[aria-label="Send a message"]')
            box.fill(MESSAGE)
            box.press("Enter")
            print(f"  sent: {MESSAGE!r}")
            page.wait_for_timeout(2500)
        except Exception as e:
            print(f"  WARN: send failed: {e}")

        body_text = page.evaluate("() => document.body.innerText || ''")[-800:]
        print(f"  body[-800:]:{body_text!r}")
        shot(page, CHAT, "chat")

        ctx.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python debug/headless_meet_send.py <meet-url>")
        sys.exit(1)
    main(sys.argv[1])
