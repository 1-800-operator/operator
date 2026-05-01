"""Headless Meet probe — bot invited as guest, signed in via operator profile.

Variant of headless_meet_baseline.py: uses operator's existing persistent
profile (already signed into heyoperator2026@gmail.com) but with
headless=True. Tests whether Meet's anti-bot heuristics block the bot
when it has a valid Google session AND has been invited to the meeting.

Goal: isolate "headless detected" from "no auth / no invite gating."

Usage:
    source venv/bin/activate
    python debug/headless_meet_invited.py <meet-url>
"""

import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

PROFILE = Path.home() / ".operator" / "browser_profile"
OUT = Path(__file__).parent / "headless_invited.png"


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

        title = page.title()
        final_url = page.url
        body_text = page.evaluate("() => document.body.innerText || ''")[:800]

        print(f"  title:      {title!r}")
        print(f"  final_url:  {final_url}")
        print(f"  body[:800]: {body_text!r}")

        page.screenshot(path=str(OUT), full_page=True)
        print(f"  screenshot: {OUT}")

        ctx.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python debug/headless_meet_invited.py <meet-url>")
        sys.exit(1)
    main(sys.argv[1])
