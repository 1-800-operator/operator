"""Terminal connector — stdin/stdout MeetingConnector for `operator try`.

Treats the user's terminal as a 1-on-1 meeting: each stdin line becomes an
incoming "chat" message, each `send_chat` prints with a colored bot-name
prefix. Forces `ChatRunner` into 1-on-1 mode by pinning participant count
to 2, so the user does not need to prefix every line with `@operator`.

No captions, no join handshake, no browser. Everything above the connector
seam (LLM, MCP, skills, meeting record, tool confirmation) is unchanged.
"""
from __future__ import annotations

import os
import queue
import signal
import sys
import threading

from _1_800_operator.connectors.base import MeetingConnector


class TerminalConnector(MeetingConnector):
    def __init__(self, bot_name: str):
        super().__init__()
        self._bot_name = bot_name
        self._queue: queue.Queue[str] = queue.Queue()
        self._seen_ids = 0
        self._connected = True
        self._stdin_thread = threading.Thread(target=self._read_stdin, daemon=True)
        self._stdin_thread.start()

    def join(self, meeting_url):
        # Terminal mode has no meeting to join. Leave join_status as None so
        # ChatRunner skips the browser-join wait block entirely.
        return None

    def send_chat(self, message):
        print(f"\n\033[36m[{self._bot_name}]\033[0m {message}\n", flush=True)

    def read_chat(self):
        out = []
        while True:
            try:
                text = self._queue.get_nowait()
            except queue.Empty:
                break
            if text.strip() in ("/quit", "/exit"):
                # Mirror Ctrl+C so shutdown runs through the existing SIGINT path.
                os.kill(os.getpid(), signal.SIGINT)
                break
            self._seen_ids += 1
            out.append({"id": f"term-{self._seen_ids}", "sender": "you", "text": text})
        return out

    def get_participant_count(self):
        # Pinned at 2 → ChatRunner's ONE_ON_ONE_THRESHOLD (<=2) always holds.
        return 2

    def is_connected(self):
        return self._connected

    def set_caption_callback(self, fn):
        pass

    def leave(self):
        self._connected = False

    def _read_stdin(self):
        try:
            for line in sys.stdin:
                self._queue.put(line.rstrip("\n"))
        except (KeyboardInterrupt, EOFError):
            pass
        # stdin closed (Ctrl+D or EOF) — route through SIGINT like /quit.
        try:
            os.kill(os.getpid(), signal.SIGINT)
        except Exception:
            self._connected = False
