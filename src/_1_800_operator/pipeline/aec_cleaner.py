"""
AecCleaner — subprocess manager for the long-running aec3 binary.

The binary (`aec3_spike --stream`, built from debug/14_23_aec_spike/aec3) is a
single-process AEC3 wrapper that reads the audio helper's framing protocol on
stdin and writes cleaned mic frames on stdout using the same framing. This
module owns that subprocess: spawning it, forwarding render ('S') and capture
('M') frames in, parsing cleaned-mic frames out, and shutting cleanly on stop.

Protocol (input and output — same layout, mic-only on output):
    [1-byte tag: 'S' (0x53) = system / render, 'M' (0x4D) = mic / capture]
    [4-byte big-endian uint32: payload length in bytes]
    [N bytes: Float32 PCM, little-endian, 16 kHz mono]

Wiring (step 3 will hook this in; step 2 just builds the manager):

    helper stdout
      ├─ 'S' frames ─► s_proc.feed_audio (whisper, unchanged)
      │              └─► aec.feed_render
      └─ 'M' frames ─► aec.feed_capture
                       └─► binary cleans → on_clean_mic(pcm) → m_proc.feed_audio
"""
from __future__ import annotations

import logging
import struct
import subprocess
import threading
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# Match the helper / binary protocol exactly. Source of truth:
# src/_1_800_operator/swift/operator-audio-capture.swift and
# debug/14_23_aec_spike/aec3/src/main.rs.
_TAG_RENDER = b"S"
_TAG_CAPTURE = b"M"
_HEADER_LEN = 5
_MAX_FRAME_BYTES = 1 << 20  # matches the binary's own cap

OnCleanMic = Callable[[bytes], None]


class AecCleaner:
    """Owns the aec3 streaming subprocess.

    Single-writer assumption: feed_render and feed_capture should be called
    from one thread (the helper-stdout reader in attach_adapter). A lock
    guards stdin writes anyway so that a misuse degrades gracefully instead
    of corrupting the framing.
    """

    def __init__(
        self,
        binary_path: Path,
        on_clean_mic: OnCleanMic,
    ) -> None:
        self._binary_path = Path(binary_path)
        self._on_clean_mic = on_clean_mic

        self._proc: subprocess.Popen | None = None
        self._stdin_lock = threading.Lock()
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # Set when the stdout reader exits — used by stop() to wait for the
        # binary to drain after we close stdin.
        self._stdout_done = threading.Event()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        """Spawn the binary in streaming mode. Idempotent: no-op if alive."""
        if self.alive:
            return
        if not self._binary_path.exists():
            raise FileNotFoundError(f"aec3 binary not found: {self._binary_path}")

        log.info(f"AecCleaner: spawning {self._binary_path}")
        self._stop_event.clear()
        self._stdout_done.clear()
        self._proc = subprocess.Popen(
            [str(self._binary_path), "--stream"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

        self._stdout_thread = threading.Thread(
            target=self._stdout_loop, name="aec-cleaner-stdout", daemon=True
        )
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop, name="aec-cleaner-stderr", daemon=True
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    def feed_render(self, pcm: bytes) -> None:
        """Send an 'S' frame to the binary. No-op if the subprocess is gone."""
        self._write_frame(_TAG_RENDER, pcm)

    def feed_capture(self, pcm: bytes) -> None:
        """Send an 'M' frame to the binary. No-op if the subprocess is gone."""
        self._write_frame(_TAG_CAPTURE, pcm)

    def _write_frame(self, tag: bytes, pcm: bytes) -> None:
        if not pcm:
            return
        if len(pcm) > _MAX_FRAME_BYTES:
            log.warning(
                f"AecCleaner: oversize frame {len(pcm)}B (tag {tag!r}) — dropping"
            )
            return
        # Pre-build the full frame so the write is one syscall and we don't
        # half-emit a header if stdin fails mid-write.
        frame = tag + struct.pack(">I", len(pcm)) + pcm
        with self._stdin_lock:
            proc = self._proc
            if proc is None or proc.stdin is None or proc.poll() is not None:
                return
            try:
                proc.stdin.write(frame)
            except (BrokenPipeError, OSError) as e:
                log.warning(f"AecCleaner: stdin write failed ({e}) — binary gone")

    def stop(self, timeout: float = 2.0) -> None:
        """Close stdin (clean EOF), wait for the binary to drain and exit."""
        self._stop_event.set()
        proc = self._proc
        if proc is None:
            return
        with self._stdin_lock:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except Exception:
                pass
        # Give the binary a moment to flush remaining cleaned frames + exit.
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log.warning("AecCleaner: binary did not exit within timeout — killing")
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=1.0)
            except Exception:
                pass
        # Join readers so the caller knows there are no more callback firings.
        for t in (self._stdout_thread, self._stderr_thread):
            if t is not None and t.is_alive():
                t.join(timeout=1.0)
        log.info(f"AecCleaner: stopped (exit code {proc.returncode})")
        self._proc = None

    # ── reader loops ──────────────────────────────────────────────────────

    def _stdout_loop(self) -> None:
        """Parse framed cleaned-mic frames; fire callback per frame."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            self._stdout_done.set()
            return
        try:
            while not self._stop_event.is_set():
                header = self._read_exact(proc.stdout, _HEADER_LEN)
                if header is None:
                    log.info("AecCleaner: stdout EOF — binary exited")
                    break
                tag = header[0:1]
                (length,) = struct.unpack(">I", header[1:5])
                if length == 0 or length > _MAX_FRAME_BYTES:
                    log.warning(
                        f"AecCleaner: bogus frame length {length} on stdout — "
                        "abandoning reader"
                    )
                    break
                payload = self._read_exact(proc.stdout, length)
                if payload is None:
                    log.info("AecCleaner: stdout truncated mid-frame")
                    break
                if tag != _TAG_CAPTURE:
                    log.warning(
                        f"AecCleaner: unexpected stdout tag {tag!r} — dropping"
                    )
                    continue
                try:
                    self._on_clean_mic(payload)
                except Exception as e:
                    # A callback bug must not kill the reader — log and keep
                    # parsing frames. Backpressure is the caller's job.
                    log.warning(f"AecCleaner: on_clean_mic raised: {e}")
        except Exception as e:
            log.warning(f"AecCleaner: stdout reader crashed: {e}")
        finally:
            self._stdout_done.set()

    def _stderr_loop(self) -> None:
        """Pipe the binary's stderr into our logger, line by line."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.rstrip(b"\n").decode(errors="replace")
                if line:
                    log.info(f"aec3: {line}")
        except Exception as e:
            log.warning(f"AecCleaner: stderr reader crashed: {e}")

    @staticmethod
    def _read_exact(stream, n: int) -> bytes | None:
        """Read exactly n bytes or return None on clean EOF / truncation."""
        buf = bytearray()
        while len(buf) < n:
            chunk = stream.read(n - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)
