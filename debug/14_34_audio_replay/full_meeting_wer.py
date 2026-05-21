"""Whole-meeting WER-vs-turbo, for a representative (not worst-case) read.

model_bench.py scores the 6 hand-labeled CROSS-TALK windows — deliberately the
hardest slices. This script transcribes the ENTIRE S-leg of the corpus with each
candidate and computes WER against the current turbo transcript over the whole
meeting, so we see typical caption degradation, not just the cross-talk corner.

No labels needed (WER is candidate-vs-turbo). Turbo is transcribed once as the
reference. faster-whisper windows long audio internally, so we hand it the whole
array.

Usage:
    venv/bin/python debug/14_34_audio_replay/full_meeting_wer.py [slug] \
        [--models tiny.en,base.en,small.en]
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from load_corpus import load  # noqa: E402
from model_bench import LADDER, TURBO_REPO, COMPUTE_TYPE, BEAM_SIZE, wer  # noqa: E402

_TOK = re.compile(r"[a-z0-9']+")


def toks(s):
    return _TOK.findall(s.lower())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("slug", nargs="?", default="sqr-vyex-wob_20260520")
    ap.add_argument("--models", default="tiny.en,base.en,small.en")
    args = ap.parse_args()

    c = load(args.slug)
    audio = c.s.samples
    audio_s = len(audio) / c.s.sample_rate
    print(f"S-leg: {audio_s:.0f}s of audio", flush=True)

    from faster_whisper import WhisperModel

    def transcribe_all(repo):
        t_load = time.perf_counter()
        m = WhisperModel(repo, device="cpu", compute_type=COMPUTE_TYPE, cpu_threads=0)
        load_s = time.perf_counter() - t_load
        t0 = time.perf_counter()
        segs, _ = m.transcribe(audio, beam_size=BEAM_SIZE, language="en")
        text = " ".join(s.text.strip() for s in segs).strip()
        x_s = time.perf_counter() - t0
        del m
        return text, load_s, x_s

    print(">>> turbo (reference)", flush=True)
    ref_text, _, ref_x = transcribe_all(TURBO_REPO)
    ref_tok = toks(ref_text)
    print(f"    turbo: {len(ref_tok)} words, {ref_x:.0f}s", flush=True)

    rows = []
    for key in [m.strip() for m in args.models.split(",") if m.strip()]:
        repo = LADDER.get(key, key)
        print(f">>> {key}", flush=True)
        hyp_text, load_s, x_s = transcribe_all(repo)
        w, (s, d, i, n) = wer(ref_tok, toks(hyp_text))
        rows.append((key, len(toks(hyp_text)), x_s, w))
        print(f"    {key}: {len(toks(hyp_text))} words, {x_s:.0f}s, WER {w:.1%}", flush=True)

    print("\n" + "=" * 60)
    print(f"WHOLE-MEETING WER vs turbo  ({audio_s:.0f}s, {len(ref_tok)} ref words)")
    print("=" * 60)
    print(f"{'model':10s} {'words':>7s} {'xscribe':>8s} {'WER↓':>7s}")
    print(f"{'turbo':10s} {len(ref_tok):>7d} {ref_x:>7.0f}s {'ref':>7s}")
    for key, nw, x_s, w in rows:
        print(f"{key:10s} {nw:>7d} {x_s:>7.0f}s {w:>6.1%}")


if __name__ == "__main__":
    main()
