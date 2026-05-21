"""Benchmark smaller whisper models against the current large-v3-turbo.

Goal: find the smallest faster-whisper model we can ship with minimal
degradation from the current state. The current model
(deepdml/faster-whisper-large-v3-turbo-ct2, int8) is ~1.5GB on disk and is the
slow part of install — a cold first run downloads the whole thing from HF.

For each candidate model we transcribe the 6 hand-labeled cross-talk windows of
the sqr-vyex-wob_20260520 corpus (same .wav clips score_against_labels uses) and
report four things per model:

  size        on-disk size of the HF cache dir (the install cost)
  xscribe     wall-clock seconds to transcribe all 6 windows (150s of audio) +
              real-time factor (lower = faster)
  WER         word error rate vs the CURRENT turbo transcript (the reference the
              user picked) — pooled over all 6 windows. turbo-vs-turbo == 0.
  attrib      per-word speaker-attribution accuracy against the HAND-LABELED
              oracle, using the SHIPPED v0.1.50 attributor (raw max-overlap +
              0.10s halo-lag offset + 0.5s smoothing). This is the metric that
              actually matters downstream — does the smaller model's word timing
              still let us attribute speakers correctly. turbo's own words set
              the ~94% ceiling.

Transcription mirrors make_label_kit.py exactly (full-window transcribe, beam 5,
word_timestamps, language=en, NO lead-silence pad) so the turbo re-run reproduces
the cached words.json and every model is on equal footing.

Usage:
    venv/bin/python debug/14_34_audio_replay/model_bench.py [slug] \
        [--models tiny.en,base.en,small.en] [--no-turbo]
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from load_corpus import load  # noqa: E402
from word_level_attribution import intervals_from_timeline  # noqa: E402
from score_against_labels import parse_label, gt_per_word  # noqa: E402

# --- shipped v0.1.50 attribution config (mirror of whisper_worker.py) ---------
HALO_LAG_OFFSET_SECONDS = 0.10
WORD_GROUP_SMOOTH_SECONDS = 0.5

# --- transcription config (mirror of make_label_kit.py / audio.py) ------------
BEAM_SIZE = 5
COMPUTE_TYPE = "int8"

DEFAULT_SLUG = "sqr-vyex-wob_20260520"

# Candidate ladder, smallest first. English-only (.en) because production
# hardcodes language="en" — .en variants are smaller AND more accurate for
# English at low param counts. turbo is the current production model / reference.
TURBO_REPO = "deepdml/faster-whisper-large-v3-turbo-ct2"
LADDER = {
    "tiny.en":  "Systran/faster-whisper-tiny.en",
    "base.en":  "Systran/faster-whisper-base.en",
    "small.en": "Systran/faster-whisper-small.en",
    "medium.en": "Systran/faster-whisper-medium.en",
    "turbo":    TURBO_REPO,
}

_TOK = re.compile(r"[a-z0-9']+")


def norm_tokens(s: str) -> list[str]:
    return _TOK.findall(s.lower())


def _max_overlap_speaker(intervals, c0: float, c1: float, default: str) -> str:
    """Mirror of whisper_worker.WhisperWorker._max_overlap_speaker."""
    best_name, best_overlap = "", 0.0
    for t0, t1, name in intervals:
        overlap = max(0.0, min(t1, c1) - max(t0, c0))
        if overlap > best_overlap:
            best_overlap, best_name = overlap, name
    if best_name:
        return best_name
    candidates = [(t1, name) for (t0, t1, name) in intervals if t1 <= c0]
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]
    return default


def _group_words(words, smooth_gap):
    """Mirror of whisper_worker._group_words."""
    segs = []
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


def shipped_attribution(words, intervals, default):
    """Apply the v0.1.50 attributor and return one final speaker per word, in
    word order (smoothing reassigns absorbed runs to the flanking speaker)."""
    off = HALO_LAG_OFFSET_SECONDS
    for w in words:
        w["speaker"] = _max_overlap_speaker(intervals, w["w0"] + off, w["w1"] + off, default)
    groups = _group_words(words, WORD_GROUP_SMOOTH_SECONDS)
    pred = []
    for spk, run in groups:
        pred.extend(spk for _ in run)
    return pred


def wer(ref_tokens, hyp_tokens):
    """Word error rate via difflib opcodes: (S + D + I) / len(ref)."""
    sm = difflib.SequenceMatcher(a=ref_tokens, b=hyp_tokens, autojunk=False)
    s = d = i = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "replace":
            s += max(i2 - i1, j2 - j1)
        elif tag == "delete":
            d += i2 - i1
        elif tag == "insert":
            i += j2 - j1
    return (s + d + i) / max(1, len(ref_tokens)), (s, d, i, len(ref_tokens))


def cache_dir_for(repo):
    name = "models--" + repo.replace("/", "--")
    return Path.home() / ".cache" / "huggingface" / "hub" / name


def du_human(path: Path):
    if not path.exists():
        return "?"
    out = subprocess.run(["du", "-sh", str(path)], capture_output=True, text=True)
    return out.stdout.split("\t")[0].strip() if out.returncode == 0 else "?"


def read_wav(path: Path):
    with wave.open(str(path), "rb") as w:
        n = w.getnframes()
        raw = w.readframes(n)
        sr = w.getframerate()
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    return samples, sr


def window_rel_start(win: str) -> float:
    # "winA_128-153" -> 128.0
    return float(win.split("_")[1].split("-")[0])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("slug", nargs="?", default=DEFAULT_SLUG)
    ap.add_argument("--models", default="tiny.en,base.en,small.en",
                    help="comma list of ladder keys to test (turbo added unless --no-turbo)")
    ap.add_argument("--no-turbo", action="store_true",
                    help="skip the turbo baseline re-run (still uses cached words.json as WER ref)")
    args = ap.parse_args()

    kit = HERE / "labelkit" / args.slug
    windows = sorted(
        p.stem.replace("_label", "")
        for p in kit.glob("*_label.txt")
    )
    if not windows:
        print(f"no labeled windows in {kit}", file=sys.stderr)
        sys.exit(2)

    # corpus -> wall-clock anchor + halo timeline for attribution
    c = load(args.slug)
    leg = c.s
    a0 = leg.first_sample_wall_clock
    intervals = intervals_from_timeline(c.timeline)

    # Per-window: load wav, ground-truth tokens (from labels), turbo reference
    # tokens (cached words.json text), and the clip wall-clock anchor.
    win_data = {}
    for win in windows:
        samples, sr = read_wav(kit / f"{win}.wav")
        rs = window_rel_start(win)
        clip_t0 = leg.sample_t(leg.index_at(a0 + rs))
        turbo_words = json.loads((kit / f"{win}_words.json").read_text())
        ref_tokens = [t for w in turbo_words for t in norm_tokens(w["word"])]
        turns = parse_label(kit / f"{win}_label.txt")
        win_data[win] = dict(samples=samples, clip_t0=clip_t0,
                             turbo_words=turbo_words, ref_tokens=ref_tokens, turns=turns)

    model_keys = [m.strip() for m in args.models.split(",") if m.strip()]
    if not args.no_turbo and "turbo" not in model_keys:
        model_keys.append("turbo")

    rows = []
    from faster_whisper import WhisperModel

    for key in model_keys:
        repo = LADDER.get(key, key)
        print(f"\n>>> {key}  ({repo})", flush=True)
        t_load = time.perf_counter()
        model = WhisperModel(repo, device="cpu", compute_type=COMPUTE_TYPE, cpu_threads=0)
        load_s = time.perf_counter() - t_load

        all_ref, all_hyp = [], []          # pooled WER
        gt_all, pred_all, dur_all = [], [], []   # pooled attribution
        xscribe_s = 0.0
        audio_s = 0.0
        for win in windows:
            d = win_data[win]
            clip = d["samples"]
            audio_s += len(clip) / 16000.0
            t0 = time.perf_counter()
            segments, _ = model.transcribe(
                clip, beam_size=BEAM_SIZE, word_timestamps=True, language="en")
            words = []
            for seg in segments:
                for w in (seg.words or []):
                    words.append({"word": w.word,
                                  "w0": d["clip_t0"] + w.start,
                                  "w1": d["clip_t0"] + w.end})
            xscribe_s += time.perf_counter() - t0

            # WER vs turbo reference
            hyp_tokens = [t for w in words for t in norm_tokens(w["word"])]
            all_ref.extend(d["ref_tokens"])
            all_hyp.extend(hyp_tokens)

            # Attribution vs hand-labeled oracle (shipped attributor on THIS
            # model's word timings). gt_per_word aligns labels to this model's
            # word stream by text, so it's model-agnostic ground truth.
            if not words:
                continue
            gt = gt_per_word(words, d["turns"])
            pred = shipped_attribution(words, intervals, default="?")
            for g, p, w in zip(gt, pred, words):
                if g is None:           # unknown turn — exclude from scoring
                    continue
                gt_all.append(g)
                pred_all.append(p)
                dur_all.append(w["w1"] - w["w0"])

        werv, (s, dele, ins, n) = wer(all_ref, all_hyp)
        attr_word = sum(1 for g, p in zip(gt_all, pred_all) if g == p) / max(1, len(gt_all))
        good_t = sum(dt for g, p, dt in zip(gt_all, pred_all, dur_all) if g == p)
        attr_time = good_t / max(1e-9, sum(dur_all))
        size = du_human(cache_dir_for(repo))
        rtf = xscribe_s / max(1e-9, audio_s)
        rows.append(dict(key=key, size=size, load_s=load_s, xscribe_s=xscribe_s,
                         rtf=rtf, wer=werv, sdi=(s, dele, ins, n),
                         attr_word=attr_word, attr_time=attr_time, n_attr=len(gt_all)))
        del model

    # --- report ---
    print("\n" + "=" * 92)
    print(f"BENCH  slug={args.slug}  windows={len(windows)} ({audio_s:.0f}s audio)  "
          f"attrib over {rows[0]['n_attr'] if rows else 0} scored words")
    print("=" * 92)
    hdr = f"{'model':10s} {'size':>7s} {'load':>6s} {'xscribe':>8s} {'RTF':>6s} {'WER↓':>7s} {'attrib(word)↑':>14s} {'attrib(time)':>13s}"
    print(hdr)
    print("-" * 92)
    for r in rows:
        wer_str = "ref" if r["key"] == "turbo" else f"{r['wer']:.1%}"
        print(f"{r['key']:10s} {r['size']:>7s} {r['load_s']:>5.1f}s {r['xscribe_s']:>7.1f}s "
              f"{r['rtf']:>5.2f}x {wer_str:>7s} {r['attr_word']:>13.1%} {r['attr_time']:>12.1%}")
    print("-" * 92)
    print("WER = word error rate vs current turbo transcript (lower better; turbo is the reference).")
    print("attrib = per-word speaker accuracy vs hand-labeled oracle, shipped v0.1.50 attributor.")
    print("RTF = transcribe seconds per second of audio (lower = faster).")


if __name__ == "__main__":
    main()
