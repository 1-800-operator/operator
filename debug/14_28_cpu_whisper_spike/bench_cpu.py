"""
14.28 spike — CPU whisper backend benchmark vs MLX/Metal baseline.

Motivation: two crashes in a row from the MLX Metal-completion-handler
abort family (S227 + today's S233 HWK entry). Worth seeing if a CPU
backend is fast enough to drop the Metal dependency entirely.

Method: re-run S231's apples-to-apples benchmark using faster-whisper
(CTranslate2) on CPU instead of mlx-whisper on Metal. Same 12 ground-truth
utterances, same silence-padding, same WER metric — only the backend
changes.

Baseline from agent-context.md S231:
  Model: whisper-large-v3-turbo via mlx-whisper on Metal
  Latency: p50 650ms, worst-case 6.2s on 10s utterance (0.57x realtime)
  Accuracy: ~13% WER

What we measure here:
  - Cold-load time for faster-whisper-large-v3-turbo
  - Per-utterance latency (min/p50/p90/max/mean)
  - Realtime ratio on the longest utterance
  - WER vs ground truth
  - CPU usage during transcribe (rough — psutil)
  - Compute-type variant: int8 (production pick) and float32 (max accuracy)
"""
import glob
import os
import re
import string
import sys
import time
import wave

import numpy as np
import psutil

# Match S231 benchmark
SAMPLE_RATE = 16000
SILENCE_PAD_S = 0.5

# faster-whisper model. The HF repo with CT2 conversion of
# whisper-large-v3-turbo. faster-whisper auto-downloads on first use.
FW_MODEL = "deepdml/faster-whisper-large-v3-turbo-ct2"

SNAP = os.path.expanduser("~/.operator/debug_snapshots/gte-dmiw-spw-2026-05-14-pm")
WORKSHEET = f"{SNAP}/ground_truth_worksheet.md"


def parse_worksheet():
    items = []
    with open(WORKSHEET) as f:
        text = f.read()
    blocks = re.split(r"\n## #", text)[1:]
    for b in blocks:
        wav_m = re.search(r"afplay '([^']+)'", b)
        actual_m = re.search(r"actual:\s*(.*)", b)
        if not (wav_m and actual_m):
            continue
        items.append({"wav": wav_m.group(1), "actual": actual_m.group(1).strip()})
    return items


def load_wav(path):
    with wave.open(path, "rb") as w:
        frames = w.readframes(w.getnframes())
        sr = w.getframerate()
        n_channels = w.getnchannels()
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if n_channels == 2:
        audio = audio.reshape(-1, 2).mean(axis=1)
    if sr != SAMPLE_RATE:
        raise RuntimeError(f"unexpected sample rate {sr} for {path}")
    # Prepend silence pad (same as live pipeline does — without it whisper
    # drops the first word).
    pad = np.zeros(int(SAMPLE_RATE * SILENCE_PAD_S), dtype=np.float32)
    return np.concatenate([pad, audio])


def normalize(s):
    s = s.lower().translate(str.maketrans("", "", string.punctuation))
    s = re.sub(r"\s+", " ", s).strip()
    return s.split()


def wer(ref_words, hyp_words):
    if not ref_words:
        return None, 0
    # Standard edit-distance WER (substitutions + insertions + deletions)
    n, m = len(ref_words), len(hyp_words)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = i
    for j in range(m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost = 0 if ref_words[i - 1] == hyp_words[j - 1] else 1
            dp[i][j] = min(dp[i - 1][j] + 1, dp[i][j - 1] + 1, dp[i - 1][j - 1] + cost)
    errs = dp[n][m]
    return errs / n, errs


def transcribe_fw(model, audio):
    """Call faster-whisper transcribe, return the concatenated text."""
    segments, _info = model.transcribe(
        audio,
        language="en",
        beam_size=5,           # faster-whisper default
        vad_filter=False,      # match live pipeline (no VAD on already-segmented utterances)
    )
    # segments is a generator; materialize it to force the transcribe to run
    return " ".join(seg.text.strip() for seg in segments).strip()


def bench(compute_type):
    from faster_whisper import WhisperModel
    print(f"\n{'='*70}")
    print(f"BENCHMARK: faster-whisper-large-v3-turbo on CPU, compute_type={compute_type}")
    print(f"{'='*70}\n")

    print(f"Loading model (cold-cache may download ~1.5GB)…")
    t0 = time.time()
    model = WhisperModel(
        FW_MODEL,
        device="cpu",
        compute_type=compute_type,
        cpu_threads=0,  # 0 = use all available
    )
    load_s = time.time() - t0
    print(f"  cold-load: {load_s:.1f}s")

    # Warmup pass — first transcribe pays a JIT cost.
    print(f"\nWarmup transcribe (1s silence)…")
    t0 = time.time()
    _ = transcribe_fw(model, np.zeros(SAMPLE_RATE, dtype=np.float32))
    warmup_ms = (time.time() - t0) * 1000
    print(f"  warmup: {warmup_ms:.0f}ms")

    items = parse_worksheet()
    print(f"\nLoaded {len(items)} utterances from worksheet")

    rows = []
    total_errs = 0
    total_ref_words = 0
    proc = psutil.Process()

    for i, it in enumerate(items, 1):
        audio = load_wav(it["wav"])
        dur_s = audio.shape[0] / SAMPLE_RATE - SILENCE_PAD_S

        # CPU usage sample
        proc.cpu_percent(None)  # prime
        t0 = time.time()
        hyp = transcribe_fw(model, audio)
        elapsed_ms = (time.time() - t0) * 1000
        cpu_pct = proc.cpu_percent(None)

        ref = normalize(it["actual"])
        wer_val, errs = wer(ref, normalize(hyp))

        if ref:
            total_errs += errs
            total_ref_words += len(ref)

        rows.append({
            "i": i, "actual": it["actual"], "hyp": hyp,
            "wer": wer_val, "errs": errs, "ms": elapsed_ms,
            "dur_s": dur_s, "cpu_pct": cpu_pct,
        })

    print(f"\n--- Per-utterance ---\n")
    for r in rows:
        wer_str = f"{r['wer']*100:.0f}%" if r['wer'] is not None else "n/a"
        rt_ratio = (r['ms']/1000) / r['dur_s'] if r['dur_s'] > 0 else 0
        print(f"#{r['i']:02d}  dur={r['dur_s']:.1f}s  compute={r['ms']:>5.0f}ms  "
              f"({rt_ratio:.2f}x rt)  cpu={r['cpu_pct']:.0f}%  WER={wer_str}")
        print(f"     actual: {r['actual']}")
        print(f"     hyp:    {r['hyp']}")

    print(f"\n--- Aggregate ---\n")
    times = sorted(r["ms"] for r in rows)
    durations = [r["dur_s"] for r in rows]
    cpu_pcts = [r["cpu_pct"] for r in rows if r["cpu_pct"] > 0]

    print(f"  total audio:   {sum(durations):.1f}s")
    print(f"  total compute: {sum(times)/1000:.1f}s  ({sum(times)/1000/sum(durations):.2f}x realtime)")
    print(f"")
    print(f"  latency  min: {times[0]:>6.0f} ms")
    print(f"           p50: {times[len(times)//2]:>6.0f} ms")
    print(f"           p90: {times[int(len(times)*0.9)]:>6.0f} ms")
    print(f"           max: {times[-1]:>6.0f} ms")
    print(f"           mean:{sum(times)/len(times):>6.0f} ms")
    print(f"")
    # Worst-case rt ratio (longest utterance)
    longest = max(rows, key=lambda r: r["dur_s"])
    print(f"  longest: {longest['dur_s']:.1f}s audio → {longest['ms']:.0f}ms compute "
          f"({(longest['ms']/1000)/longest['dur_s']:.2f}x rt)")
    print(f"")
    print(f"  WER total: {total_errs}/{total_ref_words} = {total_errs/total_ref_words*100:.1f}%")
    print(f"  cpu mean:  {sum(cpu_pcts)/len(cpu_pcts):.0f}% (per-transcribe, across all cores)")

    return {
        "compute_type": compute_type,
        "load_s": load_s,
        "warmup_ms": warmup_ms,
        "p50_ms": times[len(times)//2],
        "p90_ms": times[int(len(times)*0.9)],
        "max_ms": times[-1],
        "longest_rt": (longest['ms']/1000)/longest['dur_s'],
        "wer_pct": total_errs/total_ref_words*100,
        "cpu_pct": sum(cpu_pcts)/len(cpu_pcts),
    }


def main():
    results = []
    # int8 first — production CPU pick. Fast, slight accuracy hit.
    results.append(bench("int8"))
    # float32 — max accuracy. Slow but is the floor on quality.
    results.append(bench("float32"))

    # Baseline numbers from agent-context.md S231 (Metal/MLX)
    mlx_baseline = {
        "compute_type": "mlx/metal (S231 baseline)",
        "load_s": None,
        "warmup_ms": None,
        "p50_ms": 650,
        "p90_ms": None,
        "max_ms": 6200,
        "longest_rt": 0.57,
        "wer_pct": 13.0,
        "cpu_pct": None,
    }

    print(f"\n{'='*70}")
    print(f"SUMMARY vs MLX/Metal baseline")
    print(f"{'='*70}\n")
    fmt = "{:<25} {:>10} {:>10} {:>10} {:>10} {:>10}"
    print(fmt.format("backend", "p50 ms", "max ms", "rt (max)", "WER %", "cpu %"))
    print(fmt.format("-"*25, "-"*10, "-"*10, "-"*10, "-"*10, "-"*10))
    for r in [mlx_baseline] + results:
        print(fmt.format(
            r["compute_type"][:25],
            f"{r['p50_ms']}" if r["p50_ms"] else "?",
            f"{r['max_ms']:.0f}" if r["max_ms"] else "?",
            f"{r['longest_rt']:.2f}x",
            f"{r['wer_pct']:.1f}",
            f"{r['cpu_pct']:.0f}" if r["cpu_pct"] else "?",
        ))


if __name__ == "__main__":
    main()
