"""Validate grouped-sender carry-forward in DRAIN_GCHAT_QUEUE_JS (S250).

Google Chat omits the author heading on consecutive same-author messages, so
those topic nodes carry no name of their own — only data-creator-id. The drain
re-resolves an empty sender from any rendered sibling with the same creator-id
that does have a heading. Robust to the Python history cap: resolution happens
in the live DOM before the message enters any capped buffer.

Runs the real DRAIN_GCHAT_QUEUE_JS against a synthetic DOM that mirrors the
Google Chat structure (probed live S250). Launches headless chromium via
Playwright (already a project dep).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from playwright.sync_api import sync_playwright

from _1_800_operator.connectors.chat_dom_js import DRAIN_GCHAT_QUEUE_JS

_HTML = """
<div role="main">
  <c-wiz data-topic-id="m1" data-is-user-topic="true" data-creator-id="C1">
    <span data-message-id="m1" role="heading"><span class="njhDLd">Alice</span></span>
    <div jsname="bgckF">first by alice</div>
  </c-wiz>
  <c-wiz data-topic-id="m2" data-is-user-topic="true" data-creator-id="C1">
    <div jsname="bgckF">second by alice (grouped, no heading)</div>
  </c-wiz>
  <c-wiz data-topic-id="m3" data-is-user-topic="true" data-creator-id="C2">
    <span data-message-id="m3" role="heading"><span class="njhDLd">Name loading...</span></span>
    <div jsname="bgckF">hi from bob</div>
  </c-wiz>
</div>
"""


def test_grouped_sender_carry_forward():
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.set_content(_HTML)
        pg.evaluate(
            "() => { window.__operatorChatQueue = ["
            "{id:'m1', sender:'Alice', text:'first', t_dom:1},"
            "{id:'m2', sender:'', text:'second', t_dom:2},"
            "{id:'m3', sender:'Name loading...', text:'bob msg', t_dom:3},"
            "{id:'gone', sender:'', text:'orphan', t_dom:4}"
            "]; }"
        )
        # Simulate the Name-loading placeholder resolving to a real name.
        pg.evaluate(
            "() => { document.querySelector('[data-message-id=\"m3\"] .njhDLd')"
            ".textContent = 'Bob'; }"
        )
        drained = pg.evaluate(f"({DRAIN_GCHAT_QUEUE_JS})()")
        b.close()

    by_id = {m["id"]: m["sender"] for m in drained}
    assert by_id["m1"] == "Alice", by_id
    assert by_id["m2"] == "Alice", f"grouped continuation must inherit Alice via creator-id, got {by_id['m2']!r}"
    assert by_id["m3"] == "Bob", f"Name-loading must re-resolve to Bob, got {by_id['m3']!r}"
    assert by_id["gone"] == "", f"orphan id (no topic in DOM) left as-is, got {by_id['gone']!r}"
    print("  grouped-sender carry-forward + name-loading + orphan: PASS")


if __name__ == "__main__":
    print("Google Chat grouped-sender tests:")
    test_grouped_sender_carry_forward()
    print("\nAll tests passed.")
