"""Unit tests for the minimal sync CDP-over-websocket client (cdp_ws.py).

Exercises the RFC 6455 framing we hand-rolled (S250) against a fake socket:
client masking, server frame decode at 7/16-bit lengths, fragmentation
reassembly, ping→pong, and the call() id-correlation. The live transport was
validated against a real Chrome target separately; this pins the framing.
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from _1_800_operator.connectors.cdp_ws import CDPTarget, CDPError


def _server_text_frame(payload: bytes, fin=True, opcode=0x1) -> bytes:
    """Build a server→client (unmasked) frame."""
    b0 = (0x80 if fin else 0x00) | opcode
    n = len(payload)
    if n < 126:
        hdr = bytes([n])
    elif n < 65536:
        hdr = bytes([126]) + struct.pack(">H", n)
    else:
        hdr = bytes([127]) + struct.pack(">Q", n)
    return bytes([b0]) + hdr + payload


class _FakeSock:
    """Feeds queued inbound bytes to recv(); records sent bytes."""
    def __init__(self, inbound: bytes):
        self._in = inbound
        self.sent = b""
    def recv(self, n):
        chunk = self._in[:n]
        self._in = self._in[n:]
        return chunk
    def sendall(self, b):
        self.sent += b
    def close(self):
        pass
    def settimeout(self, t):
        pass


def _target_with(inbound: bytes) -> CDPTarget:
    t = CDPTarget("ws://localhost:9222/devtools/page/ABC")
    t._sock = _FakeSock(inbound)
    return t


def test_send_text_is_masked_and_well_formed():
    t = _target_with(b"")
    t._send_text("hi")
    sent = t._sock.sent
    assert sent[0] == 0x81, "FIN+text opcode"
    assert sent[1] & 0x80, "mask bit set on client frame"
    assert (sent[1] & 0x7F) == 2, "payload length 2"
    mask = sent[2:6]
    masked = sent[6:8]
    unmasked = bytes(masked[i] ^ mask[i % 4] for i in range(2))
    assert unmasked == b"hi"
    print("  send_text masked + well-formed: PASS")


def test_recv_small_frame():
    t = _target_with(_server_text_frame(b'{"ok":1}'))
    assert json.loads(t._recv_text()) == {"ok": 1}
    print("  recv small frame: PASS")


def test_recv_16bit_length_frame():
    payload = json.dumps({"data": "x" * 300}).encode()
    assert len(payload) >= 126  # forces 16-bit length path
    t = _target_with(_server_text_frame(payload))
    assert json.loads(t._recv_text())["data"] == "x" * 300
    print("  recv 16-bit length frame: PASS")


def test_recv_fragmented_message():
    # text frame (fin=0) + continuation (fin=1)
    f1 = _server_text_frame(b'{"a":', fin=False, opcode=0x1)
    f2 = _server_text_frame(b'1}', fin=True, opcode=0x0)
    t = _target_with(f1 + f2)
    assert json.loads(t._recv_text()) == {"a": 1}
    print("  recv fragmented message: PASS")


def test_ping_gets_pong_then_message():
    ping = bytes([0x89, 0x00])  # server ping, no payload
    msg = _server_text_frame(b'{"v":7}')
    t = _target_with(ping + msg)
    assert json.loads(t._recv_text()) == {"v": 7}
    # a masked pong (0x8A) should have been sent in response to the ping
    assert t._sock.sent[0] == 0x8A, "pong opcode"
    assert t._sock.sent[1] & 0x80, "pong masked"
    print("  ping→pong then message: PASS")


def test_close_frame_raises():
    t = _target_with(bytes([0x88, 0x00]))  # server close
    try:
        t._recv_text()
        assert False, "expected CDPError on close frame"
    except CDPError:
        pass
    print("  close frame raises: PASS")


def test_call_correlates_by_id():
    # Server replies to id=1 (Runtime.enable would be id=1 in real use, but
    # call() increments per call; here first call → id 1).
    reply = _server_text_frame(json.dumps({"id": 1, "result": {"value": 42}}).encode())
    t = _target_with(reply)
    res = t.call("Runtime.evaluate", {"expression": "x"})
    assert res == {"value": 42}
    print("  call correlates by id: PASS")


def test_call_raises_on_protocol_error():
    reply = _server_text_frame(json.dumps({"id": 1, "error": {"message": "boom"}}).encode())
    t = _target_with(reply)
    try:
        t.call("Bad.method")
        assert False, "expected CDPError"
    except CDPError:
        pass
    print("  call raises on protocol error: PASS")


if __name__ == "__main__":
    print("cdp_ws framing tests:")
    test_send_text_is_masked_and_well_formed()
    test_recv_small_frame()
    test_recv_16bit_length_frame()
    test_recv_fragmented_message()
    test_ping_gets_pong_then_message()
    test_close_frame_raises()
    test_call_correlates_by_id()
    test_call_raises_on_protocol_error()
    print("\nAll tests passed.")
