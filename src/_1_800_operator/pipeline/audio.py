"""
AudioProcessor — utterance detection + Whisper STT for slip mode.

The connector (AttachAdapter) feeds raw Float32 16kHz mono PCM bytes from
the Operator audio helper into feed_audio(); this module handles
silence-based utterance segmentation and transcription via faster-whisper.

Backend: `faster-whisper` (CTranslate2) on CPU. S233 swapped from
mlx-whisper after two production crashes from MLX's async Metal
command-buffer abort path (see docs/agent-context.md HWK S227 + S233 and
the bench in debug/14_28_cpu_whisper_spike/). CPU is materially slower at
p50 (~3.5s vs ~650ms) but identical WER, lower worst-case latency, and
kills the entire MLX/Metal crash family. Captions don't need to be
real-time — they're queried via the transcript MCP, not consumed live.

The CTranslate2 generator returned by `model.transcribe()` is not
thread-safe. Operator runs two AudioProcessors (S leg = system audio,
M leg = mic) on separate threads. We share a single module-level
WhisperModel and serialise transcribe calls under a lock. 1.5GB model
shared across both legs (vs ~3GB if each leg held its own).

Other slip-only simplifications carried from voice-preserved:pipeline/audio.py:
  - mlx-whisper-only branch removed (this module is the new sole backend)
  - no is_speaking echo guard (slip is chat-only, the bot never speaks audio)
  - no is_prompt / no_speech_timeout (slip listens continuously)
"""
from __future__ import annotations

import logging
import os
import threading
import time
import wave
from collections import Counter

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000

# VAD constants — carried verbatim from voice-preserved. Tuned against real
# meeting audio; don't loosen without re-tuning. RMS=0.02 is the floor that
# rejects HVAC / fan noise but catches normal speech; SILENCE_THRESHOLD=2
# checks @ 0.5s = ~1s of trailing silence to call an utterance done;
# MAX_DURATION=10s caps runaway utterances (long speakers get chunked).
UTTERANCE_CHECK_INTERVAL = 0.5
UTTERANCE_SILENCE_THRESHOLD = 2
UTTERANCE_MAX_DURATION = 10
UTTERANCE_SILENCE_RMS = 0.02

# faster-whisper decoder beam. Benched at S240 against beam_size 1/3/5 on
# the 12-utterance ground-truth set (debug/14_28_cpu_whisper_spike/
# bench_beam_size.py): no p50 latency win at any lower value (turbo's
# 4-layer decoder is fast enough that the encoder dominates wall-clock on
# CPU int8), and 5 has the lowest WER. Don't lower without re-benching.
WHISPER_BEAM_SIZE = 5

# Whisper hallucinates these when fed near-silence. Match-and-drop after
# transcribe(); preserves real utterances that happen to be just "thanks".
# Lowercased + stripped before compare.
WHISPER_HALLUCINATIONS = {
    "you", "thank you", "thanks", "thanks a lot", "bye", "goodbye",
    "the end", "i'm sorry", "sorry",
}

# faster-whisper-large-v3-turbo via CTranslate2 — same underlying model as
# the prior mlx-whisper-large-v3-turbo (S231), different inference engine.
# Bench: 13.7% WER (float32) / 14.4% WER (int8) vs mlx's 13.0% on the same
# 12-utterance set (within noise). Production pick is int8 — slightly
# faster wall-clock, +0.7 WER vs float32 (sub-noise), ~4 cores during
# transcribe on M-series. See debug/14_28_cpu_whisper_spike/.
_FW_MODEL_REPO = "deepdml/faster-whisper-large-v3-turbo-ct2"
_FW_COMPUTE_TYPE = "int8"

# Module-level singleton + locks. _MODEL_LOAD_LOCK guards lazy instantiation
# in _get_model(); _MODEL_USE_LOCK serialises concurrent transcribe calls
# from the S-leg and M-leg threads (faster-whisper's segment generator is
# not thread-safe). Splitting load vs use keeps load contention out of the
# hot transcribe path.
_MODEL = None
_MODEL_LOAD_LOCK = threading.Lock()
_MODEL_USE_LOCK = threading.Lock()


def _get_model():
    """Return the shared WhisperModel, instantiating on first call.

    Cold-cache first call downloads ~1.5GB to ~/.cache/huggingface/ — can
    take 100s+ on a slow network. Warm-cache load is ~1-2s. Caller pays
    this cost; AudioProcessor.__init__ triggers it so it happens on the
    warming thread, not mid-meeting on the audio thread.
    """
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    with _MODEL_LOAD_LOCK:
        if _MODEL is not None:
            return _MODEL
        from faster_whisper import WhisperModel
        _MODEL = WhisperModel(
            _FW_MODEL_REPO,
            device="cpu",
            compute_type=_FW_COMPUTE_TYPE,
            cpu_threads=0,  # 0 = use all available cores
        )
        return _MODEL


class AudioProcessor:
    """Per-stream audio buffer + utterance loop + Whisper STT.

    Each meeting runs two of these — one fed by the helper's [S] frames
    (system audio = remote participants) and one fed by [M] frames (mic =
    local user). Each owns its own buffer and runs its own
    capture_next_utterance() loop on its own thread. The Whisper model
    itself is module-global and serialised under _MODEL_USE_LOCK.
    """

    def __init__(self):
        # Trigger lazy model load on the construction thread. First-ever
        # call downloads the model; subsequent constructions are cheap.
        _get_model()
        # Warmup pass — one transcribe on a second of silence to JIT any
        # compute-type-specific kernels. Without this the first real
        # utterance pays the JIT cost.
        with _MODEL_USE_LOCK:
            segments, _info = _MODEL.transcribe(
                np.zeros(SAMPLE_RATE, dtype=np.float32),
                language="en",
                beam_size=WHISPER_BEAM_SIZE,
                vad_filter=False,
            )
            # Materialise the generator — faster-whisper does no compute
            # until you iterate.
            for _ in segments:
                pass
        log.info("AudioProcessor: faster-whisper-large-v3-turbo ready")
        self._audio_buffer = b""
        self._audio_lock = threading.Lock()
        self.capturing = False
        # Set to a directory path to enable per-utterance WAV dumps (debug).
        self.debug_dir: str | None = None
        self._debug_seq = 0

    def feed_audio(self, chunk: bytes) -> None:
        """Append raw PCM bytes to the buffer. Called from the helper-reader thread."""
        with self._audio_lock:
            self._audio_buffer += chunk

    def drain_audio_buffer(self) -> bytes:
        with self._audio_lock:
            data = self._audio_buffer
            self._audio_buffer = b""
        return data

    def capture_next_utterance(self) -> tuple[str, float | None]:
        """Block until a complete utterance is detected.

        Returns (text, speech_start_time) where speech_start_time is the
        wall-clock time.time() captured at the first non-silent frame of
        the utterance. text is '' (and speech_start_time None) when the
        loop exits without detecting speech, or when transcription drops
        the result as a hallucination.

        speech_start_time is the load-bearing piece for downstream
        speaker attribution: the DOM speaking indicator clears the moment
        speech ends, but Whisper doesn't finalize until ~0.5-1s after,
        by which point the *next* speaker has typically grabbed the
        indicator. Attribution must look up "who was speaking at
        speech_start_time", not "who is speaking now."

        Loops at UTTERANCE_CHECK_INTERVAL, accumulating PCM until either
        SILENCE_THRESHOLD consecutive silent ticks (utterance done) or
        MAX_DURATION elapsed (forced cut).
        """
        speech_detected = False
        silence_count = 0
        utterance_audio = b""
        speech_start_time: float | None = None
        silence_start_time: float | None = None

        while self.capturing:
            time.sleep(UTTERANCE_CHECK_INTERVAL)
            raw = self.drain_audio_buffer()
            if raw:
                rms = float(np.sqrt(np.mean(np.frombuffer(raw, dtype=np.float32) ** 2)))
                if rms >= UTTERANCE_SILENCE_RMS:
                    if not speech_detected:
                        speech_start_time = time.time()
                        log.info(f"AudioProcessor: speech_first rms={rms:.4f}")
                    speech_detected = True
                    silence_count = 0
                    silence_start_time = None
                    utterance_audio += raw
                else:
                    if speech_detected:
                        utterance_audio += raw
                        silence_count += 1
                        if silence_count == 1:
                            silence_start_time = time.time()
            else:
                if speech_detected:
                    silence_count += 1
                    if silence_count == 1:
                        silence_start_time = time.time()

            if speech_detected:
                if silence_count >= UTTERANCE_SILENCE_THRESHOLD:
                    log.info("AudioProcessor: utterance_done (silence)")
                    break
                if speech_start_time is not None and time.time() - speech_start_time > UTTERANCE_MAX_DURATION:
                    log.info("AudioProcessor: utterance_done (max_duration)")
                    break

        if not utterance_audio:
            return "", None

        self._write_debug_wav(utterance_audio)

        audio = np.frombuffer(utterance_audio, dtype=np.float32)
        text = self.transcribe(audio)
        # SECURITY: never log the caption text itself. The whole meeting
        # transcript already lives at ~/.operator/history/<slug>.jsonl
        # (0o700/0o600); the root logger writes /tmp/operator.log with
        # default umask (0o644 → world-readable on multi-user macOS, plus
        # any sandboxed app on the box). Length counter only.
        log.info("AudioProcessor: whisper_done (%d chars)", len(text or ""))
        if not text:
            return "", None
        if text.lower() in WHISPER_HALLUCINATIONS:
            log.info("AudioProcessor: dropped (silence hallucination)")
            return "", None
        if self._is_repetition_hallucination(text):
            log.info("AudioProcessor: dropped (repetition hallucination)")
            return "", None
        return text, speech_start_time

    def _write_debug_wav(self, pcm_bytes: bytes) -> str | None:
        """Write raw PCM to a WAV file under debug_dir. Returns path or None."""
        if not self.debug_dir:
            return None
        self._debug_seq += 1
        path = os.path.join(
            self.debug_dir,
            f"utterance_{int(time.time())}_{self._debug_seq:04d}.wav",
        )
        try:
            os.makedirs(self.debug_dir, exist_ok=True)
            data = np.frombuffer(pcm_bytes, dtype=np.float32)
            data_int16 = (np.clip(data, -1.0, 1.0) * 32767).astype(np.int16)
            with wave.open(path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(data_int16.tobytes())
            log.info(f"AudioProcessor: debug WAV → {path} ({len(data)} samples)")
            return path
        except Exception as e:
            log.warning(f"AudioProcessor: debug WAV write failed: {e}")
            return None

    @staticmethod
    def _is_repetition_hallucination(text: str) -> bool:
        words = text.lower().split()
        if len(words) <= 10:
            return False
        counts = Counter(words)
        if counts.most_common(1)[0][1] / len(words) > 0.5:
            return True
        bigrams = [f"{words[i]} {words[i+1]}" for i in range(len(words) - 1)]
        if bigrams:
            bcounts = Counter(bigrams)
            if bcounts.most_common(1)[0][1] / len(bigrams) > 0.5:
                return True
        return False

    def transcribe(self, audio: np.ndarray) -> str:
        """Transcribe a Float32 mono 16kHz array via faster-whisper.

        Prepends 0.5s of silence — without it whisper drops the first word
        of short utterances. Carried over from the mlx-whisper era verbatim.
        Serialised against the shared model under _MODEL_USE_LOCK; the
        CTranslate2 generator is not thread-safe and S/M legs both call here.
        """
        silence_pad = np.zeros(int(SAMPLE_RATE * 0.5), dtype=np.float32)
        audio = np.concatenate([silence_pad, audio])
        with _MODEL_USE_LOCK:
            segments, _info = _MODEL.transcribe(
                audio,
                language="en",
                beam_size=WHISPER_BEAM_SIZE,
                vad_filter=False,
            )
            # Materialise inside the lock — faster-whisper does no compute
            # until iteration, so releasing early would let a second thread
            # enter transcribe() concurrently with this one's compute.
            text = " ".join(seg.text.strip() for seg in segments).strip()
        return text
