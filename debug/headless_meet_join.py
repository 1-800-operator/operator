"""Headless Meet probe — full join flow.

Builds on headless_meet_invited.py: same operator profile + headless,
but actually clicks through the mic/cam modal and the Join now button,
then screenshots the in-meeting state.

Captures three screenshots so we can diagnose any step that fails:
  1. headless_join_pre.png   — initial pre-join screen (modal up)
  2. headless_join_modal.png — after dismissing mic/cam modal
  3. headless_join_inroom.png — ~8s after clicking Join now (in-meeting
                                 state if admission succeeded, lobby
                                 wait screen otherwise)

Usage:
    source venv/bin/activate
    python debug/headless_meet_join.py <meet-url>

Be ready to admit "Operator" from the host browser — the bot will knock
on the lobby once Join now is clicked.
"""

import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

PROFILE = Path.home() / ".operator" / "browser_profile"
OUT = Path(__file__).parent
PRE = OUT / "headless_join_pre.png"
POST_MODAL = OUT / "headless_join_modal.png"
INROOM = OUT / "headless_join_inroom.png"


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

        print(f"navigating to {url} (headless, profile={PROFILE})")
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(8000)
        shot(page, PRE, "pre-join")

        # Step 1 — dismiss mic/cam permission modal
        try:
            cont = page.get_by_text("Continue without microphone and camera")
            cont.wait_for(state="visible", timeout=5000)
            cont.click()
            print("  clicked: Continue without microphone and camera")
            page.wait_for_timeout(2000)
        except Exception as e:
            print(f"  WARN: mic/cam modal not dismissed: {e}")
        shot(page, POST_MODAL, "post-modal")

        # Step 2 — click Join now
        try:
            join = page.get_by_role("button", name="Join now")
            join.wait_for(state="visible", timeout=5000)
            join.click()
            print("  clicked: Join now")
        except Exception as e:
            print(f"  WARN: Join now click failed: {e}")

        # Wait for lobby admission / in-meeting render
        print("  waiting 15s for host admission + in-meeting render...")
        page.wait_for_timeout(15000)

        title = page.title()
        final_url = page.url
        body_text = page.evaluate("() => document.body.innerText || ''")[:1000]
        print(f"  title:      {title!r}")
        print(f"  final_url:  {final_url}")
        print(f"  body[:1000]:{body_text!r}")
        shot(page, INROOM, "in-room")

        ctx.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python debug/headless_meet_join.py <meet-url>")
        sys.exit(1)
    main(sys.argv[1])
