"""
S240 follow-up — beam_size sweep for production faster-whisper config.

The original 14.28 spike used `beam_size=5` (faster-whisper's library
default) as the apples-to-apples MLX comparison baseline. The spike's
own follow-up section flagged this as untested:

  > "beam_size tuning — faster-whisper default is 5, can lower to 1
  >  (greedy) for ~20-30% latency improvement at a small WER cost."

This script answers the question the spike teed up. Runs the same 12
ground-truth utterances at beam_size 1, 3, 5 with one model load.
Same compute_type=int8 (production pick), same VAD-off, same silence
pad — only beam_size changes.
"""
import os
import re
import string
import time
import wave

import numpy as np

SAMPLE_RATE = 16000
SILENCE_PAD_S = 0.5
FW_MODEL = "deepdml/faster-whisper-large-v3-turbo-ct2"
SNAP = os.path.expanduser("~/.operator/debug_snapshots/gte-dmiw-spw-2026-05-14-pm")
WORKSHEET = f"{SNAP}/ground_truth_worksheet.md"

BEAM_SIZES = [1, 3, 5]


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
    pad = np.zeros(int(SAMPLE_RATE * SILENCE_PAD_S), dtype=np.float32)
    return np.concatenate([pad, audio])


def normalize(s):
    s = s.lower().translate(str.maketrans("", "", string.punctuation))
    s = re.sub(r"\s+", " ", s).strip()
    return s.split()


def wer(ref_words, hyp_words):
    if not ref_words:
        return None, 0
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


def transcribe(model, audio, beam_size):
    segments, _info = model.transcribe(
        audio,
        language="en",
        beam_size=beam_size,
        vad_filter=False,
    )
    return " ".join(seg.text.strip() for seg in segments).strip()


def run_pass(model, items, beam_size):
    # Warmup at this beam_size — first transcribe at a new beam_size
    # pays a small JIT-ish cost on CTranslate2.
    _ = transcribe(model, np.zeros(SAMPLE_RATE, dtype=np.float32), beam_size)

    rows = []
    total_errs = 0
    total_ref_words = 0
    for i, it in enumerate(items, 1):
        audio = load_wav(it["wav"])
        dur_s = audio.shape[0] / SAMPLE_RATE - SILENCE_PAD_S
        t0 = time.time()
        hyp = transcribe(model, audio, beam_size)
        elapsed_ms = (time.time() - t0) * 1000
        ref = normalize(it["actual"])
        wer_val, errs = wer(ref, normalize(hyp))
        if ref:
            total_errs += errs
            total_ref_words += len(ref)
        rows.append({
            "i": i, "actual": it["actual"], "hyp": hyp,
            "wer": wer_val, "errs": errs, "ms": elapsed_ms, "dur_s": dur_s,
        })

    times = sorted(r["ms"] for r in rows)
    return {
        "beam_size": beam_size,
        "rows": rows,
        "p50_ms": times[len(times)//2],
        "p90_ms": times[int(len(times)*0.9)],
        "max_ms": times[-1],
        "mean_ms": sum(times)/len(times),
        "wer_pct": total_errs/total_ref_words*100,
        "total_errs": total_errs,
        "total_ref_words": total_ref_words,
    }


def main():
    from faster_whisper import WhisperModel
    print(f"Loading faster-whisper-large-v3-turbo (int8, cpu)…")
    t0 = time.time()
    model = WhisperModel(FW_MODEL, device="cpu", compute_type="int8", cpu_threads=0)
    print(f"  loaded in {time.time()-t0:.1f}s")

    items = parse_worksheet()
    print(f"Loaded {len(items)} ground-truth utterances\n")

    results = []
    for bs in BEAM_SIZES:
        print(f"=== beam_size={bs} ===")
        r = run_pass(model, items, bs)
        results.append(r)
        for row in r["rows"]:
            wer_str = f"{row['wer']*100:>4.0f}%" if row['wer'] is not None else "  n/a"
            print(f"  #{row['i']:02d}  dur={row['dur_s']:>4.1f}s  compute={row['ms']:>5.0f}ms  WER={wer_str}")
        print(f"  --  p50={r['p50_ms']:.0f}ms  p90={r['p90_ms']:.0f}ms  max={r['max_ms']:.0f}ms  WER={r['wer_pct']:.1f}%")
        print()

    print(f"{'='*70}")
    print(f"SUMMARY")
    print(f"{'='*70}\n")
    fmt = "{:<10} {:>10} {:>10} {:>10} {:>10} {:>12}"
    print(fmt.format("beam_size", "p50 ms", "p90 ms", "max ms", "mean ms", "WER %"))
    print(fmt.format("-"*10, "-"*10, "-"*10, "-"*10, "-"*10, "-"*12))
    base = results[-1]  # beam_size=5 is the baseline (current production)
    for r in results:
        wer_delta = r["wer_pct"] - base["wer_pct"]
        p50_delta = (r["p50_ms"] - base["p50_ms"]) / base["p50_ms"] * 100
        wer_str = f"{r['wer_pct']:.1f} ({wer_delta:+.1f})"
        p50_str = f"{r['p50_ms']:.0f}"
        if r["beam_size"] != base["beam_size"]:
            p50_str += f" ({p50_delta:+.0f}%)"
        print(fmt.format(
            str(r["beam_size"]),
            p50_str,
            f"{r['p90_ms']:.0f}",
            f"{r['max_ms']:.0f}",
            f"{r['mean_ms']:.0f}",
            wer_str,
        ))

    print(f"\nBaseline: beam_size=5 (current production)")
    print(f"WER deltas vs baseline shown in parens; p50 delta as %")


if __name__ == "__main__":
    main()
