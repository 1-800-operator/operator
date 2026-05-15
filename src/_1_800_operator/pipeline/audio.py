"""
AudioProcessor — utterance detection + Whisper STT for slip mode.

The connector (AttachAdapter) feeds raw Float32 16kHz mono PCM bytes from
the operator-audio-capture helper into feed_audio(); this module handles
silence-based utterance segmentation and transcription via mlx-whisper.

Ported from voice-preserved:pipeline/audio.py with the slip-only
simplifications spec'd in 14.20.4:
  - mlx-whisper only (no faster-whisper branch — slip is Mac-only because
    the Swift helper requires ScreenCaptureKit)
  - no is_speaking echo guard (slip is chat-only, the bot never speaks audio)
  - no debug WAV dump
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

# Whisper hallucinates these when fed near-silence. Match-and-drop after
# transcribe(); preserves real utterances that happen to be just "thanks".
# Lowercased + stripped before compare.
WHISPER_HALLUCINATIONS = {
    "you", "thank you", "thanks", "thanks a lot", "bye", "goodbye",
    "the end", "i'm sorry", "sorry",
}

# whisper-large-v3-turbo replaced whisper-base after the 14.23 STT
# benchmark — same 12 ground-truth utterances dropped from ~22% WER to
# ~13% WER (40% relative reduction) with no other pipeline changes.
# Wins were concentrated on the failure modes that hurt readability the
# most: proper-noun recovery ("Kyle", "Ariel"), acoustically-similar
# confusions ("review end of call sure" vs "refuel and of course sir"),
# and word-shape mismatches ("flagging" vs "fogging").
#
# Latency budget OK on M-series — p50 ~650ms, worst-case ~6s for a 10s
# utterance (~0.57x realtime). The live caption-to-write path stays
# inside the 5-10s ceiling.
#
# Cost: ~800MB on disk (vs ~140MB for base), ~1.6GB resident at runtime.
# An attendees-only initial_prompt was also tested and dropped — it
# helped marginally on whisper-base but actively hurt on -large-v3-turbo
# by biasing the decoder away from filler/short words; once the model
# is strong enough to read the audio unaided, the prompt becomes noise.
MLX_REPO = "mlx-community/whisper-large-v3-turbo"


class AudioProcessor:
    """Per-stream audio buffer + utterance loop + Whisper STT.

    Each meeting runs two of these — one fed by the helper's [S] frames
    (system audio = remote participants) and one fed by [M] frames (mic =
    local user). Each owns its own buffer and runs its own
    capture_next_utterance() loop on its own thread.
    """

    def __init__(self):
        import mlx_whisper
        self._mlx_whisper = mlx_whisper
        # Warm up: first call downloads + compiles the model (cached after).
        # Without this, the first real utterance pays a multi-second hit.
        mlx_whisper.transcribe(
            np.zeros(SAMPLE_RATE, dtype=np.float32),
            path_or_hf_repo=MLX_REPO,
            language="en",
        )
        log.info("AudioProcessor: mlx-whisper-base ready")
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

    def capture_next_utterance(self) -> str:
        """Block until a complete utterance is detected. Returns text or ''.

        Loops at UTTERANCE_CHECK_INTERVAL, accumulating PCM until either
        SILENCE_THRESHOLD consecutive silent ticks (utterance done) or
        MAX_DURATION elapsed (forced cut). Returns '' if self.capturing
        flipped False before any speech was detected.
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
            return ""

        self._write_debug_wav(utterance_audio)

        audio = np.frombuffer(utterance_audio, dtype=np.float32)
        text = self.transcribe(audio)
        log.info(f'AudioProcessor: whisper_done "{text}"')
        if not text:
            return ""
        if text.lower() in WHISPER_HALLUCINATIONS:
            log.info("AudioProcessor: dropped (silence hallucination)")
            return ""
        if self._is_repetition_hallucination(text):
            log.info("AudioProcessor: dropped (repetition hallucination)")
            return ""
        return text

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
        """Transcribe a Float32 mono 16kHz array via mlx-whisper.

        Prepends 0.5s of silence — without it whisper drops the first word
        of short utterances. Carried over from voice-preserved verbatim.
        """
        silence_pad = np.zeros(int(SAMPLE_RATE * 0.5), dtype=np.float32)
        audio = np.concatenate([silence_pad, audio])
        result = self._mlx_whisper.transcribe(
            audio, path_or_hf_repo=MLX_REPO, language="en",
        )
        return result["text"].strip()
