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

# Word-level attribution (S250): when grouping words into per-speaker caption
# runs, a lone speaker flip shorter than this AND flanked by the same speaker
# on both sides is re-absorbed into that speaker. Removes fragmentation from a
# single mis-timed word or a sub-half-second halo blip during cross-talk.
# Validated at 0.5s on the S250 replay corpus (debug/14_34_audio_replay), and
# re-confirmed S253 against 235 hand-labeled words: 0.3-0.8s all tie at the
# optimum (+0.5pt over no smoothing); 1.2s over-smooths (absorbs real turns).
WORD_GROUP_SMOOTH_SECONDS = 0.5

# Meet's speaking-ring (BlxGDf) lights up ~100ms AFTER speech actually starts
# (UI render + observer drain latency), so whisper word timestamps LEAD the halo
# timeline. At a speaker handoff the trailing word of one turn lands in the
# moment the ring has already flipped, and gets sliced onto the wrong speaker —
# the source of both fragmentation and cross-talk mis-attribution. Nudge word
# times forward by this much before overlap attribution to realign them.
# S253: validated against 235 hand-labeled words across 6 cross-talk windows —
# lifts per-word accuracy 91.9% -> 94.0% pooled; plateau +50..+150ms; nothing in
# +50..+300ms is ever worse than 0. (debug/14_34_audio_replay/offset_sweep.py)
HALO_LAG_OFFSET_SECONDS = 0.10


def _normalize_for_dedupe(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", text.lower())).strip()


def _group_words(words: list[dict], smooth_gap: float) -> list[list]:
    """Group consecutive same-speaker words into [speaker, [words...]] runs.

    smooth_gap>0: a run shorter than smooth_gap flanked by the SAME speaker on
    both sides is absorbed back into that speaker (iteratively, until stable).
    Each word dict carries "speaker", "word", "w0", "w1" (wall-clock)."""
    segs: list[list] = []
    for w in words:
        if segs and segs[-1][0] == w["speaker"]:
            segs[-1][1].append(w)
        else:
            segs.append([w["speaker"], [w]])
    if smooth_gap <= 0:
        return segs
    changed = True
    while changed and len(segs) >= 3:
        changed = False
        for i in range(1, len(segs) - 1):
            run = segs[i][1]
            dur = run[-1]["w1"] - run[0]["w0"]
            if segs[i - 1][0] == segs[i + 1][0] and dur < smooth_gap:
                segs[i - 1][1].extend(run)
                segs[i - 1][1].extend(segs[i + 1][1])
                del segs[i:i + 2]
                changed = True
                break
    return segs


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
        self.s_proc = AudioProcessor(tag="S")
        self.m_proc = AudioProcessor(tag="M")
        # Debug: dump every utterance to ~/.operator/debug/audio_<ts>/{S,M}/
        # so we can listen to what each leg actually captured. Off by default;
        # set OPERATOR_AUDIO_DEBUG=1 to enable.
        if os.environ.get("OPERATOR_AUDIO_DEBUG") == "1":
            import time as _t
            base = os.path.expanduser(f"~/.operator/debug/audio_{int(_t.time())}")
            self.s_proc.debug_dir = os.path.join(base, "S")
            self.m_proc.debug_dir = os.path.join(base, "M")
            log.info(f"whisper_worker: audio debug dumps enabled → {base}")
        # Debug: dump continuous raw PCM for both legs (replay corpus —
        # lets us iterate on VAD / attribution offline without needing
        # another meeting). Off by default; OPERATOR_AUDIO_RAW_DUMP=1 to
        # enable. Files land alongside the speaker-snapshot JSONL under
        # ~/.operator/debug/raw_<slug>/{S,M}.f32 plus a meta.json sidecar
        # with the wall-clock anchor needed to align audio against the
        # snapshot timeline.
        self._raw_dump_base: str | None = None
        if os.environ.get("OPERATOR_AUDIO_RAW_DUMP") == "1":
            self._raw_dump_base = os.path.expanduser(
                f"~/.operator/debug/raw_{self.jsonl_path.stem}"
            )
            self.s_proc.raw_dump_path = os.path.join(self._raw_dump_base, "S.f32")
            self.m_proc.raw_dump_path = os.path.join(self._raw_dump_base, "M.f32")
            log.info(f"whisper_worker: raw audio dump enabled → {self._raw_dump_base}")
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
                text, speech_start, words = proc.capture_next_utterance()
            except Exception as e:
                log.warning(f"whisper_worker[{leg}]: utterance raised: {e}")
                continue
            if not text:
                continue
            # S-leg attribution via timeline; M-leg uses the (possibly
            # updated) mic_label resolved from main's get_self_name().
            if tag == _FRAME_TAG_SYSTEM and speech_start is not None:
                # Word-level split (S250): attribute each word to its DOM
                # speaker and emit one caption per consecutive-speaker run, so
                # cross-talk isn't flattened onto a single name. Falls back to
                # single-winner attribution when word timings are unavailable.
                if words:
                    self._write_word_attributed(words, text, default_label)
                    continue
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

    def _build_intervals(self) -> list[tuple[float, float, str]]:
        """Snapshot the speaking timeline as [(t0, t1, name)] intervals."""
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
        return intervals

    @staticmethod
    def _max_overlap_speaker(intervals, c0: float, c1: float, default: str) -> str:
        """Speaker with the most overlap against [c0, c1]; fall back to the
        most-recently-finished speaker before c0, else `default`."""
        best_name = ""
        best_overlap = 0.0
        for t0, t1, name in intervals:
            overlap = max(0.0, min(t1, c1) - max(t0, c0))
            if overlap > best_overlap:
                best_overlap = overlap
                best_name = name
        if best_name:
            return best_name
        candidates = [(t1, name) for (t0, t1, name) in intervals if t1 <= c0]
        if candidates:
            candidates.sort(reverse=True)
            return candidates[0][1]
        return default

    def _attribute_speaker(self, chunk_start: float, chunk_end: float, default: str) -> str:
        """Single-winner attribution for a whole utterance window — the
        fallback path when per-word timings aren't available."""
        return self._max_overlap_speaker(
            self._build_intervals(), chunk_start, chunk_end, default
        )

    def _write_word_attributed(self, words: list[dict], full_text: str, default: str) -> None:
        """Attribute each word to its DOM speaker, group consecutive-speaker
        runs, and write one caption per group.

        Parity: a single group (no cross-talk — the overwhelming common case)
        writes the original full text stamped with time.time(), identical to
        the pre-S250 single-caption path. Only genuine multi-speaker blobs
        split into multiple captions, each stamped with its first word's
        wall-clock (which also drops the post-transcribe chunk_end bias)."""
        intervals = self._build_intervals()
        off = HALO_LAG_OFFSET_SECONDS
        for w in words:
            w["speaker"] = self._max_overlap_speaker(intervals, w["w0"] + off, w["w1"] + off, default)
        groups = _group_words(words, WORD_GROUP_SMOOTH_SECONDS)
        if len(groups) <= 1:
            speaker = groups[0][0] if groups else default
            self._write_caption(speaker, full_text, time.time())
            self._record_s_caption(full_text)
            return
        for speaker, run in groups:
            seg_text = "".join(x["word"] for x in run).strip()
            if not seg_text:
                continue
            self._write_caption(speaker, seg_text, run[0]["w0"])
            self._record_s_caption(seg_text)

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
            # Captions key the speaker as "speaker" (S250). Chat messages use
            # "sender". Readers resolve via record_server._speaker_of, which
            # falls back to "sender" for pre-S250 caption files.
            "speaker": speaker,
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

        # Finalize raw audio dump (OPERATOR_AUDIO_RAW_DUMP=1). Close both
        # leg files + write a meta.json sidecar with the wall-clock anchor
        # and byte counts needed for offline replay.
        if self._raw_dump_base is not None:
            self.s_proc.close_raw_dump()
            self.m_proc.close_raw_dump()
            meta = {
                "version": 1,
                "slug": self.jsonl_path.stem,
                "meeting_jsonl_path": str(self.jsonl_path),
                "sample_rate": 16000,
                "dtype": "float32",
                "channels": 1,
                "byte_order": "little",
                "S": {
                    "path": "S.f32",
                    "byte_count": self.s_proc._raw_dump_byte_count,
                    "first_byte_wall_clock": self.s_proc._raw_dump_first_t,
                },
                "M": {
                    "path": "M.f32",
                    "byte_count": self.m_proc._raw_dump_byte_count,
                    "first_byte_wall_clock": self.m_proc._raw_dump_first_t,
                },
            }
            # Ensure base dir exists — feed_audio() creates it lazily on the
            # first PCM chunk, so an empty-meeting path (no audio ever flowed)
            # would leave us writing meta.json into nowhere.
            try:
                os.makedirs(self._raw_dump_base, exist_ok=True)
                meta_path = os.path.join(self._raw_dump_base, "meta.json")
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(meta, f, indent=2)
                log.info(
                    f"whisper_worker: raw dump finalized "
                    f"(S={self.s_proc._raw_dump_byte_count}B, "
                    f"M={self.m_proc._raw_dump_byte_count}B) → {meta_path}"
                )
            except OSError as e:
                log.warning(f"whisper_worker: raw dump meta write failed: {e}")

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
