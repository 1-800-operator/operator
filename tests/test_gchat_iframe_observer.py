"""Unit tests for the Google Chat iframe chat-observer wiring (S250).

A Meet attached to a Google Chat space renders chat inside a cross-origin
chat.google.com iframe instead of the in-page [data-panel-id] panel. That
iframe is an OOPIF that Playwright's connect_over_cdp does NOT expose in
page.frames, so the adapter reaches it via its own CDP target websocket
(_discover_gchat_target_ws + _iframe_evaluate over cdp_ws.CDPTarget). These
tests cover the Python-side surface selection + drain routing + the
surface-scoped spaces/-id placeholder filter, mocking those two methods.
The in-frame extraction JS + the CDP transport were validated live against a
real space-attached meeting (S250); not re-tested here.

Asserts:
  - _install_chat_observer installs into the iframe (surface="iframe") when a
    gchat CDP target is discoverable, else the classic page panel
  - _do_read_chat drains via the iframe CDP target when surface is iframe,
    from the PAGE when classic
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
from _1_800_operator.connectors.chat_dom_js import OBSERVER_ATTACHED_CHECK_JS


def _make_page(drain_return=None):
    page = MagicMock()
    def _ev(js, *a, **k):
        if "operatorChatQueue" in js:
            return drain_return if drain_return is not None else []
        return True
    page.evaluate = MagicMock(side_effect=_ev)
    return page


def _iframe_adapter(drain_return=None):
    """Adapter whose iframe CDP target is 'present' and whose _iframe_evaluate
    returns True for attached-check/install and drain_return for drains."""
    a = AttachAdapter()
    a._discover_gchat_target_ws = MagicMock(return_value="ws://localhost:9222/devtools/page/X")
    def _ie(js, *args, **k):
        if "operatorChatQueue" in js:
            return drain_return if drain_return is not None else []
        return True  # install + attached-check
    a._iframe_evaluate = MagicMock(side_effect=_ie)
    return a


def test_install_selects_iframe_surface():
    a = _iframe_adapter()
    a._install_chat_observer(_make_page())
    assert a._observer_installed is True
    assert a._chat_surface == "iframe"
    assert a._iframe_evaluate.called
    print("  install selects iframe surface: PASS")


def test_install_selects_classic_surface():
    a = AttachAdapter()
    a._discover_gchat_target_ws = MagicMock(return_value=None)  # no iframe target
    page = _make_page()
    a._install_chat_observer(page)
    assert a._observer_installed is True
    assert a._chat_surface == "classic"
    assert page.evaluate.called
    print("  install selects classic surface: PASS")


def test_read_drains_from_iframe_and_bypasses_spaces_filter():
    gchat_msg = {"id": "MFivfrcBGcI", "sender": "Alice", "text": "hi", "t_dom": 1779226033229}
    a = _iframe_adapter(drain_return=[gchat_msg])
    msgs = a._do_read_chat(_make_page(drain_return=[]))
    assert a._chat_surface == "iframe"
    assert len(msgs) == 1, f"expected 1 iframe msg, got {msgs}"
    assert msgs[0]["id"] == "MFivfrcBGcI"
    assert "t_drained" in msgs[0]
    print("  read drains iframe + bypasses spaces/ filter: PASS")


def test_read_classic_still_applies_spaces_filter():
    a = AttachAdapter()
    a._discover_gchat_target_ws = MagicMock(return_value=None)
    placeholder = {"id": "placeholder123", "sender": "Bob", "text": "x", "t_dom": 1}
    canonical = {"id": "spaces/AAA/messages/BBB", "sender": "Bob", "text": "y", "t_dom": 2}
    page = _make_page(drain_return=[placeholder, canonical])
    msgs = a._do_read_chat(page)
    assert a._chat_surface == "classic"
    ids = [m["id"] for m in msgs]
    assert ids == ["spaces/AAA/messages/BBB"], f"placeholder not filtered: {ids}"
    print("  read classic applies spaces/ filter: PASS")


def test_send_routes_to_iframe_when_surface_iframe():
    a = AttachAdapter()
    a._reply_prefix = "[BOT] "
    a._chat_surface = "iframe"
    a._iframe_send = MagicMock(return_value=True)
    page = MagicMock()
    out = a._do_send_chat(page, "hello there")
    # iframe send is used, with the prefix applied; classic textarea is NOT touched
    a._iframe_send.assert_called_once_with("[BOT] hello there")
    assert out is None, "iframe send returns None (text-match dedup fallback)"
    assert not page.locator.called, "classic textarea path must not run for iframe"
    print("  send routes to iframe + applies prefix: PASS")


if __name__ == "__main__":
    print("Google Chat iframe observer wiring tests:")
    test_install_selects_iframe_surface()
    test_install_selects_classic_surface()
    test_read_drains_from_iframe_and_bypasses_spaces_filter()
    test_read_classic_still_applies_spaces_filter()
    test_send_routes_to_iframe_when_surface_iframe()
    print("\nAll tests passed.")
