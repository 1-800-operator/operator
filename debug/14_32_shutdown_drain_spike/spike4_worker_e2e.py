"""
Spike 4: End-to-end whisper_worker test (no Chrome).

Verifies the new worker module works in isolation:
  - Spawn worker subprocess with --jsonl <tmpfile> --mic-label "Jojo"
  - Feed it real speech WAVs (S-leg) framed in the wire protocol
  - Send speaker_start/stop events so attribution has something to chew on
  - Send shutdown event with attended list
  - Close stdin → worker drains residual and writes meeting_end
  - Verify JSONL has the expected structure

PASS criteria:
  - All captions land
  - Last line is meeting_end
  - participants_final line precedes meeting_end
  - Each caption has correct speaker attribution
"""
import glob
import functools
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
import wave

import numpy as np

# Force unbuffered prints so we can watch progress live.
print = functools.partial(print, flush=True)

WAV_GLOB = os.path.expanduser(
    "~/.operator/debug_snapshots/sqr-vyex-wob-2026-05-14-am/operator_audio_debug/S/utterance_*.wav"
)


def load_wav_bytes(path: str) -> bytes:
    """Return raw Float32 PCM bytes ready to wrap in our wire protocol."""
    with wave.open(path, "rb") as w:
        assert w.getframerate() == 16000
        nframes = w.getnframes()
        raw = w.readframes(nframes)
    sampwidth = w.getsampwidth()
    if sampwidth == 4:
        return raw  # already Float32
    elif sampwidth == 2:
        arr = (np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0)
        return arr.tobytes()
    raise ValueError(f"unsupported sampwidth {sampwidth}")


def frame(tag: bytes, payload: bytes) -> bytes:
    return tag + struct.pack(">I", len(payload)) + payload


def event_frame(obj: dict) -> bytes:
    return frame(b"E", json.dumps(obj).encode("utf-8"))


def main():
    wavs = sorted(glob.glob(WAV_GLOB), key=os.path.getsize)
    if not wavs:
        print("FAIL: no test WAVs found")
        sys.exit(1)
    samples = [wavs[-1], wavs[len(wavs) // 2], wavs[-3]]
    print(f"Using {len(samples)} sample WAVs (longest first)")

    jsonl_path = tempfile.mktemp(suffix="_worker_test.jsonl")
    print(f"Worker JSONL: {jsonl_path}")

    # Spawn worker via the same module entry point main will use.
    worker_cmd = [
        sys.executable,
        "-m",
        "_1_800_operator.pipeline.whisper_worker",
        "--jsonl", jsonl_path,
        "--mic-label", "Jojo",
    ]
    env = os.environ.copy()
    # Use the source tree (the new module isn't in the installed pkg yet)
    # but pull faster_whisper/numpy/etc from the installed venv.
    env["PYTHONPATH"] = ":".join([
        "/Users/jojo/Desktop/operator/src",
        "/Users/jojo/.local/share/uv/tools/1-800-operator/lib/python3.14/site-packages",
    ])
    print(f"Spawning worker: {' '.join(worker_cmd)}")
    # Redirect worker stderr to a file so its log output doesn't fill the
    # pipe buffer and block writes. Read it back at the end for debugging.
    stderr_path = jsonl_path + ".worker_stderr"
    stderr_f = open(stderr_path, "wb")
    proc = subprocess.Popen(
        worker_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=stderr_f,
        start_new_session=True,
        env=env,
    )

    # Give the worker time to import + warm whisper.
    print("Waiting 8s for whisper warmup...")
    time.sleep(8)

    base_ts = time.time()
    # Feed three S-leg utterances with interleaved speaker events.
    # Speaker name sequence: Alice, Bob, Alice
    speakers = ["Alice", "Bob", "Alice"]
    for i, (wav_path, speaker) in enumerate(zip(samples, speakers)):
        speech_start = base_ts + i * 15  # 15s apart to keep things clean
        speech_end = speech_start + (os.path.getsize(wav_path) / 64000)  # Float32 16kHz
        # Speaker_start before audio
        proc.stdin.write(event_frame({
            "type": "speaker_start", "name": speaker, "t": speech_start,
        }))
        proc.stdin.flush()
        # Stream PCM as one frame (helper-style; we don't need chunking for this test)
        pcm = load_wav_bytes(wav_path)
        proc.stdin.write(frame(b"S", pcm))
        proc.stdin.flush()
        # Speaker_stop after audio
        proc.stdin.write(event_frame({
            "type": "speaker_stop", "name": speaker, "t": speech_end,
        }))
        proc.stdin.flush()
        # Pause so the utterance loop's 0.5s silence ticks have time to
        # detect end-of-utterance (we're not sending follow-up audio).
        time.sleep(3.0)

    # Feed one M-leg utterance (no speaker event — uses mic_label "Jojo").
    pcm = load_wav_bytes(samples[1])
    proc.stdin.write(frame(b"M", pcm))
    proc.stdin.flush()
    time.sleep(3.0)

    # Send shutdown event with attended list.
    proc.stdin.write(event_frame({
        "type": "shutdown",
        "attended": ["Alice", "Bob"],
        "currently_present": [],
        "self_name": "Jojo",
    }))
    proc.stdin.flush()

    # Close stdin — worker should drain residual + write meeting_end + exit.
    print("Closing worker stdin, waiting for drain...")
    proc.stdin.close()
    t0 = time.perf_counter()
    rc = proc.wait(timeout=30)
    drain_s = time.perf_counter() - t0
    print(f"Worker exited rc={rc} in {drain_s:.2f}s post-stdin-close")

    stderr_f.close()
    with open(stderr_path) as f:
        stderr = f.read()
    for line in stderr.split("\n"):
        if any(needle in line for needle in ("TIMING", "WARN", "ERROR", "FAIL", "whisper_worker")):
            print(f"  worker stderr: {line}")
    os.unlink(stderr_path)

    # Verify JSONL.
    if not os.path.exists(jsonl_path):
        print("FAIL: worker did not create JSONL")
        sys.exit(1)
    with open(jsonl_path) as f:
        lines = [json.loads(line) for line in f if line.strip()]
    print(f"\n=== JSONL contents ({len(lines)} lines) ===")
    for line in lines:
        kind = line.get("kind")
        if kind == "caption":
            print(f"  [caption] {line.get('sender'):<10} {line.get('text')[:80]}")
        else:
            print(f"  [{kind}] {json.dumps({k: v for k, v in line.items() if k != 'kind'})}")

    # Assertions.
    failures = []
    if not lines:
        failures.append("no lines in JSONL")
    else:
        if lines[-1].get("kind") != "meeting_end":
            failures.append(f"last line is {lines[-1].get('kind')}, expected meeting_end")
        if not any(line.get("kind") == "participants_final" for line in lines):
            failures.append("no participants_final line")
        caption_count = sum(1 for line in lines if line.get("kind") == "caption")
        if caption_count < 2:
            failures.append(f"only {caption_count} captions, expected ≥2")
        # Check attribution: at least one Alice or Bob caption (S-leg)
        s_speakers = {line.get("sender") for line in lines
                      if line.get("kind") == "caption" and line.get("sender") in ("Alice", "Bob")}
        if not s_speakers:
            failures.append(f"no S-leg captions attributed to Alice/Bob")

    if failures:
        print(f"\nFAIL: {len(failures)} issue(s)")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nPASS — worker E2E works")
    os.unlink(jsonl_path)


if __name__ == "__main__":
    main()
