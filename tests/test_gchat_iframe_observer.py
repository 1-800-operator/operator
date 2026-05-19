"""Unit tests for the Google Chat iframe chat-observer wiring (S250).

A Meet attached to a Google Chat space renders chat inside a cross-origin
chat.google.com iframe instead of the in-page [data-panel-id] panel. These
tests cover the Python-side surface selection + drain routing + the
surface-scoped spaces/-id placeholder filter, with a fake Playwright page /
frame. The in-frame extraction JS itself was validated live via CDP against
a real space-attached meeting (S250); not re-tested here.

Asserts:
  - _find_gchat_frame returns the chat.google.com frame when present, else None
  - _install_chat_observer installs into the iframe (surface="iframe") when a
    gchat frame exists, else the classic page panel (surface="classic")
  - _do_read_chat drains from the FRAME when surface is iframe, from the PAGE
    when classic
  - the spaces/-id placeholder filter is bypassed for iframe messages
    (Google Chat ids like "MFivfrcBGcI" don't start with "spaces/") but still
    applied for classic messages
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from _1_800_operator.connectors.attach_adapter import AttachAdapter


def _make_frame(url: str, drain_return=None):
    fr = MagicMock()
    fr.url = url
    fr.evaluate = MagicMock(return_value=True)
    if drain_return is not None:
        # frame.evaluate is used for install (returns True) AND drain (returns
        # the list). Route by call: install→True, drain→list. Simplest: a
        # side_effect that returns the list when asked to drain.
        def _ev(js, *a, **k):
            if "operatorChatQueue" in js:
                return drain_return
            return True
        fr.evaluate.side_effect = _ev
    return fr


def _make_page(frames, drain_return=None):
    page = MagicMock()
    page.frames = frames
    def _ev(js, *a, **k):
        if "operatorChatQueue" in js:
            return drain_return if drain_return is not None else []
        return True
    page.evaluate = MagicMock(side_effect=_ev)
    return page


def test_find_gchat_frame():
    adapter = AttachAdapter()
    main = _make_frame("https://meet.google.com/abc-defg-hij")
    chat = _make_frame("https://chat.google.com/u/2/embed/space/AAQ")
    page = _make_page([main, chat])
    assert adapter._find_gchat_frame(page) is chat
    # No gchat frame → None
    page2 = _make_page([main])
    assert adapter._find_gchat_frame(page2) is None
    print("  find_gchat_frame: PASS")


def test_install_selects_iframe_surface():
    adapter = AttachAdapter()
    main = _make_frame("https://meet.google.com/abc-defg-hij")
    chat = _make_frame("https://chat.google.com/u/2/embed/space/AAQ")
    page = _make_page([main, chat])
    adapter._install_chat_observer(page)
    assert adapter._observer_installed is True
    assert adapter._chat_surface == "iframe"
    # The classic page.evaluate(INSTALL_CHAT_OBSERVER_JS) must NOT have run —
    # install happened on the frame.
    assert chat.evaluate.called
    print("  install selects iframe surface: PASS")


def test_install_selects_classic_surface():
    adapter = AttachAdapter()
    main = _make_frame("https://meet.google.com/abc-defg-hij")
    page = _make_page([main])
    adapter._install_chat_observer(page)
    assert adapter._observer_installed is True
    assert adapter._chat_surface == "classic"
    assert page.evaluate.called
    print("  install selects classic surface: PASS")


def test_read_drains_from_iframe_and_bypasses_spaces_filter():
    adapter = AttachAdapter()
    main = _make_frame("https://meet.google.com/abc-defg-hij")
    # Google Chat id does NOT start with "spaces/" — must survive the filter.
    gchat_msg = {"id": "MFivfrcBGcI", "sender": "Alice", "text": "hi", "t_dom": 1779226033229}
    chat = _make_frame("https://chat.google.com/u/2/embed/space/AAQ", drain_return=[gchat_msg])
    page = _make_page([main, chat], drain_return=[])  # page drain returns nothing
    msgs = adapter._do_read_chat(page)
    assert adapter._chat_surface == "iframe"
    assert len(msgs) == 1, f"expected 1 iframe msg, got {msgs}"
    assert msgs[0]["id"] == "MFivfrcBGcI"
    assert "t_drained" in msgs[0]
    print("  read drains iframe + bypasses spaces/ filter: PASS")


def test_read_classic_still_applies_spaces_filter():
    adapter = AttachAdapter()
    main = _make_frame("https://meet.google.com/abc-defg-hij")
    # Classic: a placeholder id (no spaces/ prefix) must be dropped; a
    # canonical spaces/ id must pass.
    placeholder = {"id": "placeholder123", "sender": "Bob", "text": "x", "t_dom": 1}
    canonical = {"id": "spaces/AAA/messages/BBB", "sender": "Bob", "text": "y", "t_dom": 2}
    page = _make_page([main], drain_return=[placeholder, canonical])
    msgs = adapter._do_read_chat(page)
    assert adapter._chat_surface == "classic"
    ids = [m["id"] for m in msgs]
    assert ids == ["spaces/AAA/messages/BBB"], f"placeholder not filtered: {ids}"
    print("  read classic applies spaces/ filter: PASS")


if __name__ == "__main__":
    print("Google Chat iframe observer wiring tests:")
    test_find_gchat_frame()
    test_install_selects_iframe_surface()
    test_install_selects_classic_surface()
    test_read_drains_from_iframe_and_bypasses_spaces_filter()
    test_read_classic_still_applies_spaces_filter()
    print("\nAll tests passed.")
