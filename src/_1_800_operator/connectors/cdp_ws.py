"""Minimal synchronous CDP-over-websocket client for one Chrome target.

Why this exists (S250): a Google Meet attached to a Google Chat space renders
chat inside a cross-origin chat.google.com iframe. That iframe is an
out-of-process frame (OOPIF) under site isolation, and Playwright's
`connect_over_cdp` does NOT stitch it into `page.frames` — so the normal
`frame.evaluate` path can't reach it (verified: it's a plain type=iframe
target, setAutoAttach doesn't surface it, and Playwright's CDPSession can't
route to the flattened sub-session). The iframe IS reachable by its own CDP
target websocket, which is what every diagnostic probe used.

Playwright's sync API holds a running asyncio loop on the browser thread, so
`asyncio.run()` raises there and an async client would need a background-loop
bridge. To stay dependency-free (no `websockets`) and avoid that machinery,
this is a tiny synchronous websocket client over a raw socket — enough of
RFC 6455 to talk CDP to a localhost target: client-masked text frames, server
frame reassembly (7/16/64-bit lengths + continuation), ping→pong. It is NOT a
general-purpose websocket implementation.

Usage:
    cdp = CDPTarget(ws_debugger_url)   # ws://localhost:9222/devtools/page/<id>
    cdp.connect()
    val = cdp.evaluate("() => 1 + 1")  # arrow-fn source; returns the value
    cdp.close()

evaluate() returns the JS value (returnByValue) or raises on transport error;
callers decide how to degrade.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
from urllib.parse import urlparse


class CDPError(Exception):
    pass


class CDPTarget:
    def __init__(self, ws_url: str, timeout: float = 5.0):
        u = urlparse(ws_url)
        if u.scheme != "ws":
            raise CDPError(f"only ws:// supported, got {ws_url!r}")
        self._host = u.hostname or "localhost"
        self._port = u.port or 80
        self._path = u.path or "/"
        self._timeout = timeout
        self._sock: socket.socket | None = None
        self._mid = 0

    # --- lifecycle ---------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> None:
        sock = socket.create_connection((self._host, self._port), timeout=self._timeout)
        sock.settimeout(self._timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {self._path} HTTP/1.1\r\n"
            f"Host: {self._host}:{self._port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(req.encode())
        # Read response headers up to the blank line.
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(1)
            if not chunk:
                sock.close()
                raise CDPError("connection closed during websocket handshake")
            data += chunk
        status = data.split(b"\r\n", 1)[0]
        if b"101" not in status:
            sock.close()
            raise CDPError(f"websocket handshake failed: {status!r}")
        self._sock = sock

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # --- framing -----------------------------------------------------------

    def _recv_exact(self, n: int) -> bytes:
        assert self._sock is not None
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise CDPError("socket closed mid-frame")
            buf += chunk
        return buf

    def _send_text(self, text: str) -> None:
        assert self._sock is not None
        payload = text.encode("utf-8")
        n = len(payload)
        b0 = bytes([0x81])  # FIN + text opcode
        if n < 126:
            hdr = bytes([0x80 | n])
        elif n < 65536:
            hdr = bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr = bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        masked = bytes(payload[i] ^ mask[i % 4] for i in range(n))
        self._sock.sendall(b0 + hdr + mask + masked)

    def _recv_text(self) -> str:
        chunks: list[bytes] = []
        while True:
            b0, b1 = self._recv_exact(2)
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._recv_exact(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._recv_exact(8))[0]
            payload = self._recv_exact(ln) if ln else b""
            if opcode == 0x9:  # ping → pong (masked, empty)
                assert self._sock is not None
                self._sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
                continue
            if opcode == 0xA:  # pong — ignore
                continue
            if opcode == 0x8:  # close
                raise CDPError("server sent close frame")
            if opcode in (0x0, 0x1):  # continuation / text
                chunks.append(payload)
            if fin:
                break
        return b"".join(chunks).decode("utf-8")

    # --- CDP ---------------------------------------------------------------

    def call(self, method: str, params: dict | None = None) -> dict:
        if self._sock is None:
            raise CDPError("not connected")
        self._mid += 1
        mid = self._mid
        self._send_text(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self._recv_text())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise CDPError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            # Events (no id) and responses to other ids are ignored.

    _UNSET = object()

    def evaluate(self, arrow_fn_src: str, arg=_UNSET):
        """Evaluate an arrow-function source in the target and return its value.

        The chat_dom_js constants are `() => {...}` (no-arg) or `(x) => {...}`
        (one-arg) sources; we wrap them as an IIFE so Runtime.evaluate runs
        them. When `arg` is supplied it's JSON-encoded into the call so the
        value (e.g. a chat message with quotes/emoji) crosses safely.
        """
        if arg is CDPTarget._UNSET:
            expr = f"({arrow_fn_src})()"
        else:
            expr = f"({arrow_fn_src})({json.dumps(arg)})"
        res = self.call(
            "Runtime.evaluate",
            {"expression": expr, "returnByValue": True},
        )
        return res.get("result", {}).get("value")
