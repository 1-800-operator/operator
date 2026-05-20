"""
AudioProcessor — utterance detection + Whisper STT for dial mode.

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

Other dial-only simplifications carried from voice-preserved:pipeline/audio.py:
  - mlx-whisper-only branch removed (this module is the new sole backend)
  - no is_speaking echo guard (dial is chat-only, the bot never speaks audio)
  - no is_prompt / no_speech_timeout (dial listens continuously)
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

# Lead silence prepended before every transcribe — without it whisper drops
# the first word of short utterances (mlx-whisper-era heritage). Also the
# offset subtracted from word_timestamps so per-word times are relative to
# the real (unpadded) audio.
_TRANSCRIBE_PAD_SECONDS = 0.5

# VAD constants. Hybrid asymmetric design (S249, replacing voice-preserved
# RMS-only): start utterances on (silero≥0.5 OR rms≥0.04); end utterances
# on silero-only silence after SILENCE_THRESHOLD ticks (1.5s). When
# silero-silence ends an utterance but rms still fires in the closing
# window, immediately restart at that window — catches unvoiced-onset
# words like "Three" that silero underweights. Tuning validated against
# four 10s recordings (built-in mic speech, AirPods HFP speech, ambient
# noise, single-speaker sentence with internal amplitude dips):
#   - silero=0.5: standard speech-vs-not threshold
#   - rms=0.04: above ambient noise floor (~0.025) but below partial-word
#     RMS (~0.046). The 0.02 floor from voice-preserved triggered spurious
#     utterances on AC/fan noise — too tight.
#   - silence_threshold=3 @ 0.5s tick = 1.5s of silence to end. 1.0s
#     (threshold=2) cut mid-sentence on natural pauses.
UTTERANCE_CHECK_INTERVAL = 0.5
UTTERANCE_SILENCE_THRESHOLD = 3
UTTERANCE_MAX_DURATION = 10
UTTERANCE_SILENCE_RMS = 0.04
SILERO_SPEECH_THRESHOLD = 0.5
# Silero v6 operates on 512-sample frames at 16kHz (32ms). One drain
# (0.5s = 8000 samples) contains 15 silero frames + 320 padding samples.
SILERO_FRAME_SAMPLES = 512

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

# Silero VAD singleton (S249). The model is reentrant for inference per
# upstream docs, but we serialize anyway since both legs may call into it
# concurrently and the ONNX session is shared. Bundled as part of
# faster-whisper at faster_whisper/assets/silero_vad_v6.onnx — we use the
# bundled SileroVADModel wrapper rather than the standalone silero-vad pkg.
_VAD = None
_VAD_LOAD_LOCK = threading.Lock()
_VAD_USE_LOCK = threading.Lock()


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


def _get_vad():
    """Return the shared SileroVADModel, loading on first call.

    ~2MB ONNX, ~50ms to load. Returns None if onnxruntime is unavailable
    or the bundled asset can't be located — caller falls back to RMS-only
    VAD in that case.
    """
    global _VAD
    if _VAD is not None:
        return _VAD
    with _VAD_LOAD_LOCK:
        if _VAD is not None:
            return _VAD
        try:
            import faster_whisper
            from faster_whisper.vad import SileroVADModel
            asset = os.path.join(
                os.path.dirname(faster_whisper.__file__),
                "assets", "silero_vad_v6.onnx",
            )
            _VAD = SileroVADModel(asset)
        except Exception as e:
            log.warning(f"_get_vad: Silero load failed ({e}) — falling back to RMS-only VAD")
            _VAD = False  # sentinel: tried + failed
        return _VAD if _VAD is not False else None


def _silero_is_speech(chunk: bytes) -> bool:
    """Run Silero on a 0.5s drain chunk. Return True if max frame prob
    across the chunk exceeds SILERO_SPEECH_THRESHOLD.

    Pads the chunk to a multiple of SILERO_FRAME_SAMPLES so the model
    accepts it. Returns False on any internal failure — caller still has
    the RMS branch as a safety net.
    """
    vad = _get_vad()
    if vad is None:
        return False
    try:
        audio = np.frombuffer(chunk, dtype=np.float32)
        pad = (-len(audio)) % SILERO_FRAME_SAMPLES
        # np.frombuffer returns a read-only view; Silero's ONNX session
        # writes into its input buffer in place. The pad>0 path's
        # concatenate already yields a fresh writeable array; the pad==0
        # path (chunk already a multiple of SILERO_FRAME_SAMPLES) would
        # otherwise hand the read-only view straight to ONNX → "assignment
        # destination is read-only", silently degrading the leg to RMS-only.
        if pad:
            audio = np.concatenate([audio, np.zeros(pad, dtype=np.float32)])
        else:
            audio = audio.copy()
        with _VAD_USE_LOCK:
            probs = vad(audio).reshape(-1)
        return bool(probs.max() >= SILERO_SPEECH_THRESHOLD)
    except Exception as e:
        log.warning(f"_silero_is_speech: inference failed ({e})")
        return False


class AudioProcessor:
    """Per-stream audio buffer + utterance loop + Whisper STT.

    Each meeting runs two of these — one fed by the helper's [S] frames
    (system audio = remote participants) and one fed by [M] frames (mic =
    local user). Each owns its own buffer and runs its own
    capture_next_utterance() loop on its own thread. The Whisper model
    itself is module-global and serialised under _MODEL_USE_LOCK.
    """

    def __init__(self, tag: str = ""):
        self.tag = tag
        self._tagp = f"[{tag}] " if tag else ""
        # Trigger lazy model loads on the construction thread. First-ever
        # call downloads whisper (~1.5GB); subsequent constructions are
        # cheap. Silero is bundled with faster-whisper (~2MB) and loads in
        # ~50ms.
        _get_model()
        _get_vad()
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
        log.info(f"AudioProcessor: {self._tagp}faster-whisper-large-v3-turbo ready")
        self._audio_buffer = b""
        self._audio_lock = threading.Lock()
        self.capturing = False
        # Set to a directory path to enable per-utterance WAV dumps (debug).
        self.debug_dir: str | None = None
        self._debug_seq = 0
        # Set to a file path to enable continuous raw-PCM capture for the
        # whole meeting (replay corpus — see whisper_worker OPERATOR_AUDIO_RAW_DUMP).
        # Format on disk: float32 LE, 16kHz mono, header-less. Lazy-opened on
        # the first feed_audio() call so we capture the wall-clock of the
        # first byte for downstream alignment against the speaker-snapshot
        # JSONL. Closed by close_raw_dump() at worker shutdown.
        self.raw_dump_path: str | None = None
        self._raw_dump_fh = None
        self._raw_dump_first_t: float | None = None
        self._raw_dump_byte_count: int = 0
        # Asymmetric-VAD pending state: when a silero-detected silence
        # closes an utterance but the closing chunk still has RMS-fire
        # (likely an unvoiced word onset like "Three" that silero
        # underweights), we hold that chunk over and make it the seed of
        # the NEXT utterance. capture_next_utterance both reads and writes
        # these — only the utterance thread touches them, so no lock needed.
        self._pending_prefix: bytes = b""
        self._pending_speech_detected: bool = False
        self._pending_start_time: float | None = None

    def feed_audio(self, chunk: bytes) -> None:
        """Append raw PCM bytes to the buffer. Called from the helper-reader thread."""
        with self._audio_lock:
            self._audio_buffer += chunk
        if self.raw_dump_path is not None:
            try:
                if self._raw_dump_fh is None:
                    os.makedirs(os.path.dirname(self.raw_dump_path), exist_ok=True)
                    self._raw_dump_fh = open(self.raw_dump_path, "ab")
                    self._raw_dump_first_t = time.time()
                self._raw_dump_fh.write(chunk)
                self._raw_dump_byte_count += len(chunk)
            except OSError as e:
                log.warning(f"AudioProcessor: {self._tagp}raw dump write failed: {e}")
                self.raw_dump_path = None  # disable further attempts

    def close_raw_dump(self) -> None:
        """Close the raw-PCM dump file if open. Idempotent."""
        if self._raw_dump_fh is not None:
            try:
                self._raw_dump_fh.close()
            except OSError as e:
                log.warning(f"AudioProcessor: {self._tagp}raw dump close failed: {e}")
            self._raw_dump_fh = None

    def drain_audio_buffer(self) -> bytes:
        with self._audio_lock:
            data = self._audio_buffer
            self._audio_buffer = b""
        return data

    def capture_next_utterance(self) -> tuple[str, float | None, list[dict] | None]:
        """Block until a complete utterance is detected.

        Returns (text, speech_start_time, words). speech_start_time is the
        wall-clock time.time() captured at the first non-silent frame of
        the utterance. text is '' (speech_start_time + words None) when the
        loop exits without detecting speech, or when transcription drops
        the result as a hallucination.

        words is populated only for the S (system / remote) leg: a list of
        {"word", "w0", "w1"} dicts with each word's wall-clock start/end,
        used downstream to attribute cross-talk per word (S250). The M leg
        is a single known speaker, so it returns words=None.

        speech_start_time is the load-bearing piece for downstream
        speaker attribution: the DOM speaking indicator clears the moment
        speech ends, but Whisper doesn't finalize until ~0.5-1s after,
        by which point the *next* speaker has typically grabbed the
        indicator. Attribution must look up "who was speaking at
        speech_start_time", not "who is speaking now."

        Loops at UTTERANCE_CHECK_INTERVAL. Hybrid asymmetric VAD (S249):
        utterance ONSET fires on (silero≥0.5 OR rms≥0.04); utterance END
        fires after SILENCE_THRESHOLD ticks of silero-only-silence.

        Asymmetric boundary-restart: if the chunk that pushes silence_count
        over threshold ALSO has rms-fire (silero silent + rms loud, the
        signature of an unvoiced word onset like "Three" that silero
        underweights), we pull that chunk out of the current utterance and
        hold it as the seed of the NEXT utterance. This recovers content
        that pure-Silero would lose at sentence boundaries.
        """
        # Seed from any pending state left by the previous call's
        # boundary-restart. If we were handed a prefix chunk, treat it
        # as the start of this utterance (speech already detected).
        speech_detected = self._pending_speech_detected
        speech_start_time = self._pending_start_time
        utterance_audio = self._pending_prefix
        silence_count = 0
        # Track whether Silero ever called any chunk speech during this
        # utterance. Used to silero-gate the hallucination filter — if
        # Silero never fired, we treat short whisper outputs that match
        # known hallucinations as artifacts and drop them. A real speaker
        # always trips Silero somewhere, so this is a safe gate.
        silero_ever_fired = False
        self._pending_prefix = b""
        self._pending_speech_detected = False
        self._pending_start_time = None

        while self.capturing:
            time.sleep(UTTERANCE_CHECK_INTERVAL)
            raw = self.drain_audio_buffer()
            if not raw:
                # An empty drain means the helper produced no frames this
                # tick (transient backpressure — TCC renegotiation, CPU
                # pressure, whisper inference feeding back to read
                # scheduling). NOT silence — leave silence_count untouched
                # so the countdown effectively pauses during starvation.
                # max-duration guard still bounds the wait.
                if speech_detected and speech_start_time is not None and time.time() - speech_start_time > UTTERANCE_MAX_DURATION:
                    log.info(f"AudioProcessor: {self._tagp}utterance_done (max_duration)")
                    break
                continue

            rms = float(np.sqrt(np.mean(np.frombuffer(raw, dtype=np.float32) ** 2)))
            silero_speech = _silero_is_speech(raw)
            rms_speech = rms >= UTTERANCE_SILENCE_RMS
            hybrid_speech = silero_speech or rms_speech

            if not speech_detected:
                # Onset: either VAD firing starts the utterance.
                if hybrid_speech:
                    speech_detected = True
                    speech_start_time = time.time()
                    if silero_speech:
                        silero_ever_fired = True
                    log.info(
                        f"AudioProcessor: {self._tagp}speech_first "
                        f"rms={rms:.4f} silero={'1' if silero_speech else '0'}"
                    )
                    utterance_audio += raw
                    silence_count = 0
                # If neither fires, drop the chunk (pre-utterance silence).
                continue

            # In utterance: append audio; only silero counts toward silence.
            utterance_audio += raw
            if silero_speech:
                silero_ever_fired = True
                silence_count = 0
            else:
                silence_count += 1
                if silence_count >= UTTERANCE_SILENCE_THRESHOLD:
                    if hybrid_speech:
                        # rms fired in the closing window but silero said
                        # silent — unvoiced onset of a new utterance.
                        # Pull this chunk out of current utt and stash it
                        # as the seed of the next.
                        utterance_audio = utterance_audio[:-len(raw)]
                        self._pending_prefix = raw
                        self._pending_speech_detected = True
                        self._pending_start_time = time.time()
                        log.info(
                            f"AudioProcessor: {self._tagp}utterance_done "
                            f"(silence + boundary-restart rms={rms:.4f})"
                        )
                    else:
                        log.info(f"AudioProcessor: {self._tagp}utterance_done (silence)")
                    break

            if speech_start_time is not None and time.time() - speech_start_time > UTTERANCE_MAX_DURATION:
                log.info(f"AudioProcessor: {self._tagp}utterance_done (max_duration)")
                break

        # S244: capturing=False (shutdown) exits the while loop without the
        # last 0.5s window of audio ever reaching utterance_audio. Drain one
        # more time so the trailing-utterance bytes that arrived between the
        # last tick and the flag flip get transcribed instead of dropped on
        # the floor. Belt to the worker drain's suspenders: the worker would
        # see EOF and exit cleanly, but without this drain the last word of
        # whatever was being said gets clipped.
        if not self.capturing:
            residual = self.drain_audio_buffer()
            if residual:
                rms = float(np.sqrt(np.mean(np.frombuffer(residual, dtype=np.float32) ** 2)))
                # Append if we were already mid-utterance, OR if the residual
                # itself contains speech (someone started talking right as
                # shutdown fired). Use the same hybrid speech check as the
                # main loop so the trailing word matches what the live VAD
                # would have caught.
                residual_speech = _silero_is_speech(residual) or rms >= UTTERANCE_SILENCE_RMS
                if speech_detected or residual_speech:
                    if not speech_detected:
                        speech_start_time = time.time()
                    utterance_audio += residual
                    log.info(f"AudioProcessor: {self._tagp}shutdown_drain captured {len(residual)}B (rms={rms:.4f})")

        if not utterance_audio:
            return "", None, None

        self._write_debug_wav(utterance_audio)

        audio = np.frombuffer(utterance_audio, dtype=np.float32)
        # S leg needs per-word timings for cross-talk attribution; M leg is a
        # single known speaker, so skip the (slightly costlier) word path.
        if self.tag == "S":
            text, rel_words = self.transcribe(audio, want_words=True)
        else:
            text, rel_words = self.transcribe(audio), None
        # SECURITY: never log the caption text itself. The whole meeting
        # transcript already lives at ~/.operator/history/<slug>.jsonl
        # (0o700/0o600); the root logger writes /tmp/operator.log with
        # default umask (0o644 → world-readable on multi-user macOS, plus
        # any sandboxed app on the box). Length counter only.
        log.info(f"AudioProcessor: {self._tagp}whisper_done ({len(text or '')} chars)")
        if not text:
            return "", None, None
        # Silero-gated hallucination filter: only drop matching short
        # outputs when Silero never said yes during the utterance. Real
        # participants saying "thank you" always trip Silero somewhere;
        # phantom whisper-on-noise outputs don't. Stripping trailing
        # punctuation lets us match against the bare-word entries in the
        # list (whisper adds periods to clean short outputs).
        normalized = text.lower().rstrip(".!?,;: ")
        if not silero_ever_fired and normalized in WHISPER_HALLUCINATIONS:
            log.info(f"AudioProcessor: {self._tagp}dropped (silence hallucination, silero=0)")
            return "", None, None
        if self._is_repetition_hallucination(text):
            log.info(f"AudioProcessor: {self._tagp}dropped (repetition hallucination)")
            return "", None, None
        # Map each word's audio-relative time onto wall-clock. utterance_audio
        # begins at speech_start_time, and rel times are already pad-subtracted
        # in transcribe(), so wall = speech_start_time + rel (clamped ≥ start).
        words = None
        if rel_words and speech_start_time is not None:
            words = [
                {"word": wtext,
                 "w0": speech_start_time + max(0.0, ws),
                 "w1": speech_start_time + max(0.0, we)}
                for (wtext, ws, we) in rel_words
            ]
        return text, speech_start_time, words

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
            log.info(f"AudioProcessor: {self._tagp}debug WAV → {path} ({len(data)} samples)")
            return path
        except Exception as e:
            log.warning(f"AudioProcessor: {self._tagp}debug WAV write failed: {e}")
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

    def transcribe(self, audio: np.ndarray, want_words: bool = False):
        """Transcribe a Float32 mono 16kHz array via faster-whisper.

        Prepends 0.5s of silence — without it whisper drops the first word
        of short utterances. Carried over from the mlx-whisper era verbatim.
        Serialised against the shared model under _MODEL_USE_LOCK; the
        CTranslate2 generator is not thread-safe and S/M legs both call here.

        Returns the joined text (str) by default. When want_words=True, also
        returns per-word (word, start, end) timings as the second element of
        a tuple — times are relative to the UNPADDED audio (the 0.5s lead pad
        is subtracted), so a caller can map word.start onto the utterance's
        wall-clock start. Used by the S-leg to attribute cross-talk per word
        instead of stamping the whole utterance with one speaker (S250).
        """
        silence_pad = np.zeros(int(SAMPLE_RATE * _TRANSCRIBE_PAD_SECONDS), dtype=np.float32)
        audio = np.concatenate([silence_pad, audio])
        words: list[tuple[str, float, float]] = []
        with _MODEL_USE_LOCK:
            segments, _info = _MODEL.transcribe(
                audio,
                language="en",
                beam_size=WHISPER_BEAM_SIZE,
                vad_filter=False,
                word_timestamps=want_words,
            )
            # Materialise inside the lock — faster-whisper does no compute
            # until iteration, so releasing early would let a second thread
            # enter transcribe() concurrently with this one's compute.
            seg_texts = []
            for seg in segments:
                seg_texts.append(seg.text.strip())
                if want_words:
                    for w in (seg.words or []):
                        words.append((w.word,
                                      w.start - _TRANSCRIBE_PAD_SECONDS,
                                      w.end - _TRANSCRIBE_PAD_SECONDS))
            text = " ".join(seg_texts).strip()
        if want_words:
            return text, words
        return text
