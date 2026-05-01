"""Headless Meet probe — caption observer.

Joins the meeting headless (same as headless_meet_send.py), then reuses
the existing captions_js helpers (enable_captions + CAPTION_OBSERVER_JS)
to turn captions on, install the MutationObserver, and bridge JS → Python
via the same `__onCaption` shape used by macos_adapter.

Logs every caption that arrives over a 30-second listen window. The user
should speak something during that window.

Usage:
    source venv/bin/activate
    python debug/headless_meet_captions.py <meet-url>
"""

import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

# Make the operator package importable
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from _1_800_operator.connectors.captions_js import (  # noqa: E402
    CAPTION_OBSERVER_JS,
    enable_captions,
)

PROFILE = Path.home() / ".operator" / "browser_profile"
OUT = Path(__file__).parent / "headless_captions.png"

LISTEN_SECONDS = 30
captured: list[tuple[str, str, int]] = []


def on_caption(speaker: str, text: str, ts: int) -> None:
    captured.append((speaker, text, ts))
    print(f"  caption: [{speaker}] {text[:120]}")


def main(url: str) -> None:
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            headless=True,
            viewport={"width": 1280, "height": 800},
        )
        # Bridge __onCaption BEFORE navigation, like macos_adapter does
        ctx.expose_function("__onCaption", on_caption)
        page = ctx.new_page()

        print(f"navigating to {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(8000)

        try:
            page.get_by_text("Continue without microphone and camera").click(timeout=5000)
            print("  dismissed mic/cam modal")
            page.wait_for_timeout(1500)
        except Exception:
            print("  (no modal — fast-path)")

        page.get_by_role("button", name="Join now").click(timeout=5000)
        print("  clicked Join now — waiting 12s for admission")
        page.wait_for_timeout(12000)

        # Enable captions + inject observer
        if enable_captions(page):
            page.evaluate(CAPTION_OBSERVER_JS)
            print("  captions ON + observer injected")
        else:
            print("  WARN: captions could not be enabled")
            page.screenshot(path=str(OUT), full_page=True)
            ctx.close()
            return

        print(f"  listening for {LISTEN_SECONDS}s — speak now")
        deadline = time.monotonic() + LISTEN_SECONDS
        while time.monotonic() < deadline:
            page.wait_for_timeout(500)

        page.screenshot(path=str(OUT), full_page=True)
        print(f"  screenshot: {OUT}")
        print(f"  total captions captured: {len(captured)}")
        ctx.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python debug/headless_meet_captions.py <meet-url>")
        sys.exit(1)
    main(sys.argv[1])
