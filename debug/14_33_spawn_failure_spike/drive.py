"""Drive the whisper_worker spawn-failure paths in isolation.

Tests two failure modes against the real AttachAdapter spawn machinery:
  A. subprocess.Popen raises OSError (e.g. ENOENT)
  B. Popen succeeds, but the child crashes immediately on startup

For B we want to know whether _send_worker_frame's dead-proc detection
causes a respawn storm when the worker keeps crashing on every spawn.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest.mock import patch

# Configure logging so the warnings from attach_adapter land on stderr.
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

from _1_800_operator.connectors.attach_adapter import AttachAdapter

# Build a bare AttachAdapter without running __init__'s side effects
# (browser thread, audio helper spawn, etc). We only need the spawn-worker
# bound methods + a handful of attributes they read.
def make_bare_adapter(jsonl: Path) -> AttachAdapter:
    a = AttachAdapter.__new__(AttachAdapter)
    a._jsonl_path = jsonl
    a._audio_worker_proc = None
    a._audio_worker_pid = None
    a._audio_worker_shutdown_sent = False
    import threading
    a._audio_worker_respawn_lock = threading.Lock()
    a._audio_worker_lock = threading.Lock()
    a._speaking_lock = threading.Lock()
    a._speaking_history = []
    a._mic_label = None
    a._local_participant_id = ""
    a._browser_alive = False
    a._speaker_snapshot_path = None
    # Bypass get_self_name (would require Playwright state).
    a.get_self_name = lambda: None  # type: ignore[method-assign]
    return a


def scenario_a_popen_oserror():
    print("\n=== Scenario A: subprocess.Popen raises OSError ===")
    with tempfile.TemporaryDirectory() as td:
        jsonl = Path(td) / "spike.jsonl"
        jsonl.touch()
        adapter = make_bare_adapter(jsonl)

        def raise_oserror(*args, **kwargs):
            raise OSError("[Errno 2] No such file or directory: 'bogus'")

        with patch("_1_800_operator.connectors.attach_adapter.subprocess.Popen", side_effect=raise_oserror):
            adapter._spawn_audio_worker()
        print(f"RESULT: _audio_worker_proc={adapter._audio_worker_proc}")
        assert adapter._audio_worker_proc is None, "expected None after OSError fallback"
        print("PASS: graceful — proc is None, no exception leaked")


def scenario_b_immediate_crash():
    print("\n=== Scenario B: Popen succeeds, worker crashes immediately ===")
    with tempfile.TemporaryDirectory() as td:
        jsonl = Path(td) / "spike.jsonl"
        jsonl.touch()
        adapter = make_bare_adapter(jsonl)

        spawn_count = {"n": 0}
        original_popen = subprocess.Popen

        def crashing_popen(*args, **kwargs):
            # Replace the command with one that exits 1 instantly.
            spawn_count["n"] += 1
            new_args = ([sys.executable, "-c", "import sys; sys.exit(7)"],)
            kwargs.pop("stdin", None)
            kwargs.pop("stdout", None)
            kwargs.pop("stderr", None)
            return original_popen(
                *new_args,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=kwargs.get("start_new_session", True),
            )

        with patch("_1_800_operator.connectors.attach_adapter.subprocess.Popen", side_effect=crashing_popen):
            adapter._spawn_audio_worker()
            print(f"  initial spawn returned proc pid={adapter._audio_worker_proc.pid if adapter._audio_worker_proc else None}")
            # Wait for child to die.
            time.sleep(0.5)
            assert adapter._audio_worker_proc.poll() is not None, "worker should have exited"
            print(f"  worker exited with code={adapter._audio_worker_proc.returncode}")

            # Drive 5 frame sends and watch for respawn storm.
            print("  sending 5 frames; counting respawn attempts...")
            for i in range(5):
                ok = adapter._send_worker_frame(b"S", b"x" * 100)
                print(f"    frame {i}: ok={ok}, total_spawns={spawn_count['n']}")
                time.sleep(0.2)

        print(f"RESULT: spawn_count={spawn_count['n']} after 1 initial + 5 frames")
        print("(Without backoff, expect spawn_count ≥ 6 — one per frame that detected dead proc.)")


if __name__ == "__main__":
    scenario_a_popen_oserror()
    scenario_b_immediate_crash()
    print("\n=== spike done ===")
