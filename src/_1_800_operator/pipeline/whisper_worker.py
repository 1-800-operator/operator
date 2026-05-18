"""
whisper_worker — subprocess that owns whisper STT, drains independently of
the main operator process.

Architecture (S244):
  Main process (attach_adapter) owns Chrome, the audio helper, and AEC3.
  Frames flow: helper → main's reader thread → AEC3 → main forwards clean
  PCM bytes to this worker's stdin. This worker:
    - reads framed input (1-byte tag + 4-byte BE length + payload)
    - owns AudioProcessor (S + M legs) with their own whisper model
    - replicates the speaker timeline from control events sent by main
    - attributes each finalized caption, applies S↔M bleed dedupe
    - appends caption lines directly to the meeting JSONL (O_APPEND)
  On stdin EOF the worker drains residual utterances, writes
  participants_final + meeting_end (using the attended/self_name from the
  shutdown control msg that preceded EOF), and exits.

Why a separate process: whisper transcribe of a worst-case 10s utterance
takes 3-4s wall-clock; two legs serialized through the model lock = 7s.
Embedding that in main's shutdown either (a) blocks meeting-to-meeting
iteration or (b) gets killed by the shutdown reaper and drops the trailing
caption — which is the actual bug this module exists to fix. Splitting it
out lets main exit in ~0.6s and the worker run to completion in its own
session group (start_new_session=True; safety-net reaper uses pgrep -P and
doesn't see us).

Frame protocol (stdin):
  byte 0:     tag — b"S" (system), b"M" (mic), b"E" (event)
  bytes 1-4:  big-endian uint32 length of payload
  bytes 5..:  payload — Float32 PCM for S/M, UTF-8 JSON for E

Control events (E-tag JSON):
  {"type": "speaker_start", "name": str, "t": float}
  {"type": "speaker_stop",  "name": str, "t": float}
  {"type": "mic_label", "name": str}   — update mic-leg default label
  {"type": "shutdown",
   "attended": [str], "currently_present": [str], "self_name": str}
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import struct
import sys
import threading
import time
from collections import deque
from difflib import SequenceMatcher
from pathlib import Path

from _1_800_operator.pipeline.audio import AudioProcessor

log = logging.getLogger("whisper_worker")

_FRAME_TAG_SYSTEM = b"S"
_FRAME_TAG_MIC = b"M"
_FRAME_TAG_EVENT = b"E"
_FRAME_HEADER_LEN = 5
MAX_FRAME_BYTES = 1 << 20

_SPEAKER_OTHER = "other"

# Bleed dedupe parameters — match defaults in config.py so behaviour
# matches the prior in-process path exactly.
BLEED_DEDUPE_WINDOW_SECONDS = 8.0
BLEED_DEDUPE_SIMILARITY = 0.85


def _normalize_for_dedupe(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", text.lower())).strip()


class WhisperWorker:
    def __init__(self, jsonl_path: Path, mic_label: str):
        self.jsonl_path = jsonl_path
        self.mic_label = mic_label or "user"

        # Speaker timeline — populated from main via [E] events. Same
        # semantics as AttachAdapter._speaking_history: a list of
        # (timestamp, name, "start"|"stop") tuples.
        self._timeline: deque[tuple[float, str, str]] = deque(maxlen=512)
        self._timeline_lock = threading.Lock()

        # S-leg bleed dedupe rolling buffer (timestamp, normalized_text).
        self._recent_s_captions: deque[tuple[float, str]] = deque(maxlen=64)
        self._recent_s_lock = threading.Lock()

        # AudioProcessors — whisper model loads inside the first call.
        self.s_proc = AudioProcessor()
        self.m_proc = AudioProcessor()
        # Both legs need capturing=True before their utterance threads start
        # or they exit on the first iteration.
        self.s_proc.capturing = True
        self.m_proc.capturing = True

        # Shutdown plumbing — populated when main sends shutdown event or
        # we hit stdin EOF.
        self._shutdown_payload: dict | None = None

        # Worker threads.
        self._utterance_threads: list[threading.Thread] = []

    def start(self) -> None:
        """Launch the two utterance loops."""
        for tag, label, proc in (
            (_FRAME_TAG_SYSTEM, _SPEAKER_OTHER, self.s_proc),
            (_FRAME_TAG_MIC, self.mic_label, self.m_proc),
        ):
            t = threading.Thread(
                target=self._utterance_loop,
                args=(tag, label, proc),
                name=f"whisper_worker-utt-{tag.decode()}",
                daemon=False,  # non-daemon — we WANT to wait for them on exit
            )
            t.start()
            self._utterance_threads.append(t)
        log.info("whisper_worker: utterance loops up")

    def run_stdin_loop(self) -> None:
        """Read framed input from stdin, dispatch to processors / event handler.

        Exits on EOF — caller proceeds to drain + finalize.
        """
        stdin = sys.stdin.buffer
        try:
            while True:
                header = stdin.read(_FRAME_HEADER_LEN)
                if len(header) < _FRAME_HEADER_LEN:
                    log.info("whisper_worker: stdin EOF")
                    return
                tag = header[0:1]
                (length,) = struct.unpack(">I", header[1:5])
                if length == 0 or length > MAX_FRAME_BYTES:
                    log.warning(f"whisper_worker: bogus frame length {length} — abandoning stream")
                    return
                payload = stdin.read(length)
                if len(payload) < length:
                    log.info("whisper_worker: truncated read — main exited mid-frame")
                    return
                if tag == _FRAME_TAG_SYSTEM:
                    self.s_proc.feed_audio(payload)
                elif tag == _FRAME_TAG_MIC:
                    self.m_proc.feed_audio(payload)
                elif tag == _FRAME_TAG_EVENT:
                    self._handle_event(payload)
                else:
                    log.warning(f"whisper_worker: unknown tag {tag!r} — dropping {length}B")
        except Exception as e:
            log.warning(f"whisper_worker: stdin loop crashed: {e}")

    def _handle_event(self, payload: bytes) -> None:
        try:
            msg = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            log.warning(f"whisper_worker: bad event payload ({e})")
            return
        mtype = msg.get("type")
        if mtype == "speaker_start" or mtype == "speaker_stop":
            t = msg.get("t")
            name = msg.get("name")
            if isinstance(t, (int, float)) and isinstance(name, str) and name:
                kind = "start" if mtype == "speaker_start" else "stop"
                with self._timeline_lock:
                    self._timeline.append((float(t), name, kind))
        elif mtype == "mic_label":
            name = msg.get("name")
            if isinstance(name, str) and name:
                self.mic_label = name
                log.info(f"whisper_worker: mic_label updated → {name!r}")
        elif mtype == "shutdown":
            log.info("whisper_worker: shutdown event received")
            self._shutdown_payload = msg
        else:
            log.warning(f"whisper_worker: unknown event type {mtype!r}")

    def _utterance_loop(self, tag: bytes, default_label: str, proc: AudioProcessor) -> None:
        """Drain finalized utterances from one processor, write captions."""
        leg = tag.decode()
        while proc.capturing:
            try:
                text, speech_start = proc.capture_next_utterance()
            except Exception as e:
                log.warning(f"whisper_worker[{leg}]: utterance raised: {e}")
                continue
            if not text:
                continue
            # S-leg attribution via timeline; M-leg uses the (possibly
            # updated) mic_label resolved from main's get_self_name().
            if tag == _FRAME_TAG_SYSTEM and speech_start is not None:
                speaker = self._attribute_speaker(
                    chunk_start=speech_start,
                    chunk_end=time.time(),
                    default=default_label,
                )
            else:
                speaker = self.mic_label if tag == _FRAME_TAG_MIC else default_label
            # Bleed dedupe: drop M-leg captions that just appeared on S-leg.
            if tag == _FRAME_TAG_MIC and self._is_recent_s_caption(text):
                log.info(f"whisper_worker[{leg}]: dropped (S-leg dedupe) {text!r}")
                continue
            self._write_caption(speaker, text, time.time())
            if tag == _FRAME_TAG_SYSTEM:
                self._record_s_caption(text)
        log.info(f"whisper_worker[{leg}]: utterance loop exited")

    def _attribute_speaker(self, chunk_start: float, chunk_end: float, default: str) -> str:
        """Same overlap-max logic as AttachAdapter._attribute_s_leg."""
        with self._timeline_lock:
            events = list(self._timeline)
        open_starts: dict[str, float] = {}
        intervals: list[tuple[float, float, str]] = []
        for t, name, kind in events:
            if kind == "start":
                if name in open_starts:
                    intervals.append((open_starts[name], t, name))
                open_starts[name] = t
            else:
                t0 = open_starts.pop(name, None)
                if t0 is not None:
                    intervals.append((t0, t, name))
        for name, t0 in open_starts.items():
            intervals.append((t0, float("inf"), name))
        best_name = ""
        best_overlap = 0.0
        for t0, t1, name in intervals:
            overlap = max(0.0, min(t1, chunk_end) - max(t0, chunk_start))
            if overlap > best_overlap:
                best_overlap = overlap
                best_name = name
        if best_name:
            return best_name
        candidates = [(t1, name) for (t0, t1, name) in intervals if t1 <= chunk_start]
        if candidates:
            candidates.sort(reverse=True)
            return candidates[0][1]
        return default

    def _is_recent_s_caption(self, text: str) -> bool:
        needle = _normalize_for_dedupe(text)
        if not needle:
            return False
        now = time.time()
        with self._recent_s_lock:
            while self._recent_s_captions and now - self._recent_s_captions[0][0] > BLEED_DEDUPE_WINDOW_SECONDS:
                self._recent_s_captions.popleft()
            candidates = [n for _, n in self._recent_s_captions]
        return any(SequenceMatcher(None, needle, c).ratio() >= BLEED_DEDUPE_SIMILARITY for c in candidates)

    def _record_s_caption(self, text: str) -> None:
        normalized = _normalize_for_dedupe(text)
        if not normalized:
            return
        with self._recent_s_lock:
            self._recent_s_captions.append((time.time(), normalized))

    def _write_caption(self, speaker: str, text: str, ts: float) -> None:
        entry = {
            "timestamp": ts,
            "sender": speaker,
            "text": text,
            "kind": "caption",
        }
        try:
            line = json.dumps(entry, ensure_ascii=False)
            # O_APPEND is per-write atomic on APFS for our payload sizes
            # (verified in debug/14_32_shutdown_drain_spike/spike2_*). Open
            # per write is fine — captions arrive ~once/second, file system
            # caching makes this cheap.
            with self.jsonl_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            log.warning(f"whisper_worker: caption append failed: {e}")

    def drain_and_finalize(self) -> None:
        """Flip capturing=False so utterance loops finish their current
        iteration (drains buffer + transcribes residual), wait for both
        threads to exit, then write participants_final + meeting_end."""
        log.info("whisper_worker: draining residual utterances...")
        t0 = time.perf_counter()
        self.s_proc.capturing = False
        self.m_proc.capturing = False
        for t in self._utterance_threads:
            t.join()  # No timeout — drain MUST complete (this is the whole point).
        drain_s = time.perf_counter() - t0
        log.info(f"TIMING whisper_worker_drain elapsed_s={drain_s:.3f}")

        # Write the seal lines. Use the shutdown payload if main sent one;
        # fall back to empty lists if main exited without sending shutdown
        # (crash case). Either way meeting_end lands so post-meeting tools
        # know the file is complete.
        payload = self._shutdown_payload or {}
        attended = payload.get("attended") or []
        currently_present = payload.get("currently_present") or []
        self_name = payload.get("self_name") or ""
        now = time.time()
        seal_entries: list[dict] = []
        if attended:
            seal_entries.append({
                "kind": "participants_final",
                "timestamp": now,
                "currently_present": list(currently_present),
                "attended": list(attended),
                "self_name": self_name,
            })
        seal_entries.append({"kind": "meeting_end", "timestamp": now})
        try:
            with self.jsonl_path.open("a", encoding="utf-8") as f:
                for entry in seal_entries:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            log.info(
                f"whisper_worker: sealed (participants_final={'yes' if attended else 'no'}, "
                f"meeting_end written)"
            )
        except OSError as e:
            log.warning(f"whisper_worker: seal write failed: {e}")


def _configure_logging() -> None:
    # stderr is redirected by main to /tmp/operator.log (same as audio
    # helper). Match the rest of operator's log format.
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s — %(message)s"
    ))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def main() -> int:
    parser = argparse.ArgumentParser(description="operator whisper drain worker")
    parser.add_argument("--jsonl", required=True, type=Path,
                        help="Path to meeting JSONL to append captions to")
    parser.add_argument("--mic-label", default="user",
                        help="Initial speaker label for mic-leg captions (can be updated via event)")
    args = parser.parse_args()

    _configure_logging()
    log.info(f"whisper_worker starting (jsonl={args.jsonl}, pid={os.getpid()})")

    worker = WhisperWorker(jsonl_path=args.jsonl, mic_label=args.mic_label)
    worker.start()

    # Block reading stdin until EOF. Spike 3 confirmed this process
    # survives parent SIGKILL on macOS via start_new_session=True.
    worker.run_stdin_loop()

    # Drain + write meeting_end. No timeout — drain bound is ~7s worst case
    # per spike 1, ~3-4s typical.
    worker.drain_and_finalize()

    log.info("whisper_worker exiting cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
