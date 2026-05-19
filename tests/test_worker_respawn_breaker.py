"""Respawn-storm circuit breaker for whisper_worker.

When the worker crashes immediately on every spawn (broken module, missing
dep, etc.), `_send_worker_frame` would detect the dead proc on every audio
frame and trigger a fresh respawn. At ~100 frames/sec that's a respawn
storm: PID churn, log spam (~12k lines/min), wasted CPU.

The circuit breaker caps respawn attempts: after _RESPAWN_BREAKER_THRESHOLD
attempts inside _RESPAWN_BREAKER_WINDOW_S, further respawns are disabled
for the meeting and a one-shot ERROR is logged.

Asserts:
  - graceful path: Popen raising OSError is caught, _audio_worker_proc = None
  - storm path: 5 send_worker_frame calls with a crashing worker cap at
    THRESHOLD spawn attempts (not 5), _respawn_disabled is set, an ERROR
    line was logged exactly once
"""
from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from _1_800_operator.connectors.attach_adapter import (
    AttachAdapter,
    _RESPAWN_BREAKER_THRESHOLD,
)


def make_bare_adapter(jsonl: Path) -> AttachAdapter:
    """Construct an AttachAdapter without __init__'s browser/audio side effects.

    Mirrors the subset of attributes _spawn_audio_worker, _send_worker_frame,
    and _maybe_respawn_worker read.
    """
    from collections import deque
    a = AttachAdapter.__new__(AttachAdapter)
    a._jsonl_path = jsonl
    a._audio_worker_proc = None
    a._audio_worker_pid = None
    a._audio_worker_shutdown_sent = False
    a._audio_worker_respawn_lock = threading.Lock()
    a._audio_worker_lock = threading.Lock()
    a._speaking_lock = threading.Lock()
    a._speaking_history = []
    a._mic_label = None
    a._local_participant_id = ""
    a._browser_alive = False
    a._speaker_snapshot_path = None
    a._respawn_attempts = deque(maxlen=16)
    a._respawn_disabled = False
    a.get_self_name = lambda: None  # type: ignore[method-assign]
    return a


def test_popen_oserror_is_graceful():
    with tempfile.TemporaryDirectory() as td:
        jsonl = Path(td) / "spike.jsonl"
        jsonl.touch()
        adapter = make_bare_adapter(jsonl)

        def raise_oserror(*args, **kwargs):
            raise OSError("[Errno 2] No such file or directory: 'bogus'")

        with patch(
            "_1_800_operator.connectors.attach_adapter.subprocess.Popen",
            side_effect=raise_oserror,
        ):
            adapter._spawn_audio_worker()
        assert adapter._audio_worker_proc is None, (
            "expected _audio_worker_proc = None after OSError fallback"
        )
    print("PASS: Popen OSError caught, _audio_worker_proc = None")


def test_circuit_breaker_caps_respawn_storm(caplog_messages: list[str]):
    with tempfile.TemporaryDirectory() as td:
        jsonl = Path(td) / "spike.jsonl"
        jsonl.touch()
        adapter = make_bare_adapter(jsonl)

        spawn_count = {"n": 0}
        original_popen = subprocess.Popen

        def crashing_popen(*args, **kwargs):
            """Replace the worker command with one that exits 7 instantly."""
            spawn_count["n"] += 1
            return original_popen(
                [sys.executable, "-c", "import sys; sys.exit(7)"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=kwargs.get("start_new_session", True),
            )

        with patch(
            "_1_800_operator.connectors.attach_adapter.subprocess.Popen",
            side_effect=crashing_popen,
        ):
            adapter._spawn_audio_worker()  # initial spawn — counts as 1
            time.sleep(0.3)
            assert adapter._audio_worker_proc.poll() is not None, (
                "worker should have exited"
            )
            # Drive 5 frame sends. Each detects dead proc → attempts respawn.
            # The breaker should trip after THRESHOLD attempts.
            for i in range(5):
                adapter._send_worker_frame(b"S", b"x" * 100)
                time.sleep(0.05)

        # 1 initial + at most THRESHOLD respawns before the breaker trips.
        # (Threshold is "more than N" so attempt count = THRESHOLD when last
        # respawn lands, then the (THRESHOLD+1)th caller trips the breaker
        # without spawning. Final spawn_count == 1 + THRESHOLD.)
        expected_max = 1 + _RESPAWN_BREAKER_THRESHOLD
        assert spawn_count["n"] <= expected_max, (
            f"breaker should cap at {expected_max} spawns; got {spawn_count['n']}"
        )
        assert adapter._respawn_disabled, "circuit breaker should be tripped"
        # Subsequent frames must NOT trigger another respawn.
        post_trip = spawn_count["n"]
        for _ in range(3):
            adapter._send_worker_frame(b"S", b"x" * 100)
        assert spawn_count["n"] == post_trip, (
            f"no respawns after breaker trips; got "
            f"{spawn_count['n'] - post_trip} additional"
        )
        # ERROR line must have been logged exactly once.
        storm_logs = [m for m in caplog_messages if "respawn storm" in m]
        assert len(storm_logs) == 1, (
            f"expected exactly 1 'respawn storm' log; got {len(storm_logs)}"
        )
    print(
        f"PASS: storm capped at {spawn_count['n']} spawns "
        f"(threshold={_RESPAWN_BREAKER_THRESHOLD}); breaker latched; "
        f"no respawns after trip; 1 ERROR logged"
    )


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(self.format(record))


def main():
    handler = _ListHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logging.basicConfig(level=logging.DEBUG, handlers=[handler])
    logging.getLogger().addHandler(handler)

    test_popen_oserror_is_graceful()
    test_circuit_breaker_caps_respawn_storm(handler.messages)
    print("all good")


if __name__ == "__main__":
    main()
