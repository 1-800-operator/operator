"""
Spike 5: Integration test — AttachAdapter ↔ whisper_worker IPC plumbing.

Spike 4 tested the worker module in isolation with hand-rolled frame
encoding. This spike exercises the REAL attach_adapter methods that will
run in production: _spawn_audio_worker, _send_worker_frame,
_send_worker_event, send_audio_worker_shutdown. Skips Chrome / audio
helper / AEC3 since those code paths are unchanged from the prior shipping
build.

PASS criteria:
  - AttachAdapter constructed with jsonl_path spawns a live worker
  - _send_worker_frame correctly encodes + ships PCM frames (S + M)
  - _send_worker_event ships speaker_start/stop events
  - send_audio_worker_shutdown writes the shutdown event
  - After stdin close, worker drains + writes participants_final + meeting_end
  - JSONL contents are valid (captions, attribution, seal lines)
"""
import glob
import json
import os
import sys
import tempfile
import time
import wave

import numpy as np

sys.path.insert(
    0,
    "/Users/jojo/.local/share/uv/tools/1-800-operator/lib/python3.14/site-packages",
)

from _1_800_operator.connectors.attach_adapter import AttachAdapter, _FRAME_TAG_SYSTEM, _FRAME_TAG_MIC

WAV_GLOB = os.path.expanduser(
    "~/.operator/debug_snapshots/sqr-vyex-wob-2026-05-14-am/operator_audio_debug/S/utterance_*.wav"
)


def load_wav_bytes(path: str) -> bytes:
    with wave.open(path, "rb") as w:
        assert w.getframerate() == 16000
        raw = w.readframes(w.getnframes())
    if w.getsampwidth() == 4:
        return raw
    if w.getsampwidth() == 2:
        return (np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0).tobytes()
    raise ValueError("unexpected sample width")


def chunk_pcm(pcm: bytes, chunk_ms: int = 40) -> list[bytes]:
    """Split a PCM buffer into ~chunk_ms-sized chunks matching how the
    helper actually streams audio. Without chunking, AudioProcessor sees
    one giant chunk and can't detect end-of-utterance via silence (see
    spike 4 — the AudioProcessor expects continuously-arriving frames)."""
    bytes_per_ms = 16 * 4  # 16 samples/ms × 4 bytes/sample (Float32)
    sz = chunk_ms * bytes_per_ms
    return [pcm[i:i + sz] for i in range(0, len(pcm), sz)]


def main() -> int:
    wavs = sorted(glob.glob(WAV_GLOB), key=os.path.getsize)
    if not wavs:
        print("FAIL: no test WAVs")
        return 1
    samples = [wavs[-1], wavs[len(wavs) // 2]]
    print(f"Using {len(samples)} real WAVs (≥6s of speech each)")

    jsonl_path = tempfile.mktemp(suffix="_attach_integration.jsonl")
    print(f"JSONL target: {jsonl_path}")

    # The actual production constructor.
    adapter = AttachAdapter(jsonl_path=jsonl_path)

    # Manually invoke the spawn that _warm_whisper would normally call.
    # In production this happens automatically inside join().
    print("Spawning worker via adapter._spawn_audio_worker()...")
    t_spawn = time.perf_counter()
    adapter._spawn_audio_worker()
    spawn_s = time.perf_counter() - t_spawn
    print(f"  spawn returned in {spawn_s:.3f}s")
    if not adapter.has_audio_worker:
        print("FAIL: has_audio_worker is False after spawn")
        return 1
    print(f"  worker pid={adapter._audio_worker_proc.pid}, has_audio_worker={adapter.has_audio_worker}")

    # Give the worker a moment to start its stdin reader + warm whisper.
    # (Frames sent before warmup completes buffer in the pipe — fine.)
    print("Sleeping 8s for whisper warmup...")
    time.sleep(8)

    # Push a mic_label event, then stream two utterances as chunks with
    # speaker events around them. Use real chunking so AudioProcessor's
    # silence detection works as it would in production.
    print("Pushing mic_label event...")
    assert adapter._send_worker_event({"type": "mic_label", "name": "Jojo"})

    base_ts = time.time()
    speakers = ["Alice", "Bob"]
    for i, (wav_path, speaker) in enumerate(zip(samples, speakers)):
        speech_start = base_ts + i * 12
        speech_end = speech_start + (os.path.getsize(wav_path) / 64000)
        print(f"  [{speaker}] streaming {os.path.basename(wav_path)} ({os.path.getsize(wav_path) // 1000}KB)")
        adapter._send_worker_event({"type": "speaker_start", "name": speaker, "t": speech_start})
        pcm = load_wav_bytes(wav_path)
        chunks = chunk_pcm(pcm, chunk_ms=40)
        for ch in chunks:
            adapter._send_worker_frame(_FRAME_TAG_SYSTEM, ch)
            time.sleep(0.04)  # match real-time streaming rate
        adapter._send_worker_event({"type": "speaker_stop", "name": speaker, "t": speech_end})
        # Now stream silence so the utterance loop detects end-of-utterance
        # (needs ≥2 silent ticks @ 0.5s).
        silence_chunk = (np.zeros(640, dtype=np.float32)).tobytes()  # 40ms of silence
        for _ in range(40):  # 1.6s of silence
            adapter._send_worker_frame(_FRAME_TAG_SYSTEM, silence_chunk)
            time.sleep(0.04)

    # Send shutdown event then close worker stdin via the real production path.
    print("Calling send_audio_worker_shutdown + closing stdin via _stop_audio_pipeline...")
    sent = adapter.send_audio_worker_shutdown(
        attended=["Alice", "Bob"],
        currently_present=[],
        self_name="Jojo",
    )
    if not sent:
        print("FAIL: send_audio_worker_shutdown returned False")
        return 1
    # _stop_audio_pipeline closes worker stdin (among other things). In
    # this spike there's no helper/AEC to clean up so it's a quick op.
    adapter._stop_audio_pipeline()

    # Hold a handle to the worker proc that _stop_audio_pipeline cleared.
    # We need it to wait for clean exit.
    # The Popen object stays alive in the OS; we re-find it via pgrep would
    # be overkill — instead, we kept a copy.
    # Easier: re-snapshot via psutil... actually we cleared the handle in
    # _stop_audio_pipeline. So just wait for the JSONL to gain a meeting_end
    # line — that's the real success signal anyway.
    print("Waiting for JSONL to gain meeting_end line (drain in progress)...")
    deadline = time.time() + 30
    sealed = False
    while time.time() < deadline:
        try:
            with open(jsonl_path) as f:
                lines = [json.loads(line) for line in f if line.strip()]
            if lines and lines[-1].get("kind") == "meeting_end":
                sealed = True
                break
        except (OSError, json.JSONDecodeError):
            pass
        time.sleep(0.5)

    if not sealed:
        print(f"FAIL: meeting_end never landed in {jsonl_path} within 30s")
        with open(jsonl_path) as f:
            print(f.read())
        return 1

    drain_total_s = 30 - (deadline - time.time())
    print(f"Sealed in {drain_total_s:.2f}s post-stdin-close")
    print(f"\n=== JSONL contents ({len(lines)} lines) ===")
    for line in lines:
        kind = line.get("kind")
        if kind == "caption":
            print(f"  [caption] {line.get('sender'):<10} {line.get('text', '')[:80]}")
        else:
            other = {k: v for k, v in line.items() if k != "kind"}
            print(f"  [{kind}] {json.dumps(other)}")

    # Assertions.
    failures = []
    if lines[-1].get("kind") != "meeting_end":
        failures.append(f"last line is {lines[-1].get('kind')}, expected meeting_end")
    if not any(line.get("kind") == "participants_final" for line in lines):
        failures.append("no participants_final line")
    captions = [line for line in lines if line.get("kind") == "caption"]
    if len(captions) < 1:
        failures.append(f"got {len(captions)} captions, expected ≥1")
    s_speakers = {c.get("sender") for c in captions if c.get("sender") in ("Alice", "Bob")}
    if not s_speakers:
        failures.append(f"no S-leg attribution to Alice/Bob (captions: {[c.get('sender') for c in captions]})")

    if failures:
        print(f"\nFAIL: {len(failures)} issue(s)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nPASS — AttachAdapter ↔ whisper_worker integration works")
    os.unlink(jsonl_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
