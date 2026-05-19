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


def _own_prefixes(prefix="[🤖 Claude] "):
    """Mirror AttachAdapter.__init__'s _own_prefixes derivation."""
    return tuple(dict.fromkeys(
        p for p in (prefix, "".join(c for c in prefix if c.isascii())) if p
    ))


def test_own_prefixes_cover_both_forms():
    """The two read-back forms: prefix verbatim and emoji-stripped.

    The iframe drops the 🤖 on render ('[🤖 Claude] ' → '[ Claude] '),
    which is what triggered the live S250 echo loop."""
    prefixes = _own_prefixes("[🤖 Claude] ")
    assert prefixes == ("[🤖 Claude] ", "[ Claude] "), prefixes
    assert any("[🤖 Claude] Standing by.".startswith(p) for p in prefixes)
    assert any("[ Claude] Standing by.".startswith(p) for p in prefixes)
    # Must NOT swallow ordinary participant messages.
    assert not any("hey @claude what's up".startswith(p) for p in prefixes)
    assert not any("Claude is great".startswith(p) for p in prefixes)
    # All-ASCII prefix collapses to a single form (deduped).
    assert _own_prefixes("[BOT] ") == ("[BOT] ",)
    assert _own_prefixes("") == ()
    print("  own-prefixes cover both forms: PASS")


def test_iframe_drops_own_echo():
    """Iframe read path drops the bot's own reply even with the emoji gone.

    Without this the bot re-reads '[ Claude] Standing by.' (sender 'You')
    as a fresh message and loops on itself."""
    own_echo = {"id": "MFivfrcBGcI", "sender": "You",
                "text": "[ Claude] Standing by.", "t_dom": 1779226033229}
    user_msg = {"id": "MFivfrcBGcJ", "sender": "Alice",
                "text": "@claude hello", "t_dom": 1779226033230}
    a = _iframe_adapter(drain_return=[own_echo, user_msg])
    a._reply_prefix = "[🤖 Claude] "
    a._own_prefixes = _own_prefixes("[🤖 Claude] ")
    msgs = a._do_read_chat(_make_page(drain_return=[]))
    texts = [m["text"] for m in msgs]
    assert texts == ["@claude hello"], f"own echo not dropped: {texts}"
    print("  iframe drops own echo: PASS")


def test_classic_drops_own_prefix():
    """Classic surface drops own replies too — the prefix is the single
    own-message filter on every surface (no sender/text-match anymore)."""
    own_echo = {"id": "spaces/AAA/messages/BBB", "sender": "You",
                "text": "[🤖 Claude] Standing by.", "t_dom": 2}
    user_msg = {"id": "spaces/AAA/messages/CCC", "sender": "Alice",
                "text": "@claude hi", "t_dom": 3}
    a = AttachAdapter()
    a._discover_gchat_target_ws = MagicMock(return_value=None)
    a._reply_prefix = "[🤖 Claude] "
    a._own_prefixes = _own_prefixes("[🤖 Claude] ")
    page = _make_page(drain_return=[own_echo, user_msg])
    msgs = a._do_read_chat(page)
    assert a._chat_surface == "classic"
    texts = [m["text"] for m in msgs]
    assert texts == ["@claude hi"], f"own echo not dropped on classic: {texts}"
    print("  classic drops own prefix: PASS")


def test_same_named_participant_not_dropped():
    """A participant whose display name equals the bot's tile name must NOT
    be filtered — own-message detection is by prefix, not sender. (This was
    the S250 name-collision bug: bot tile 'Jojo Shapiro' + participant
    'Jojo Shapiro' got muted.)"""
    same_name = {"id": "MFivfrcBGcZ", "sender": "Jojo Shapiro",
                 "text": "@claude are you there?", "t_dom": 10}
    a = _iframe_adapter(drain_return=[same_name])
    a._reply_prefix = "[🤖 Claude] "
    a._own_prefixes = _own_prefixes("[🤖 Claude] ")
    msgs = a._do_read_chat(_make_page(drain_return=[]))
    texts = [m["text"] for m in msgs]
    assert texts == ["@claude are you there?"], f"same-named participant dropped: {texts}"
    print("  same-named participant not dropped: PASS")


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
    test_own_prefixes_cover_both_forms()
    test_iframe_drops_own_echo()
    test_classic_drops_own_prefix()
    test_same_named_participant_not_dropped()
    test_send_routes_to_iframe_when_surface_iframe()
    print("\nAll tests passed.")
