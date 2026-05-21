"""Offline prototype: boundary-based (debounced) speaker attribution.

Same whisper transcription, three attribution strategies, side by side:

  LIVE       single-winner per VAD utterance (from the shipped JSONL) -> WELDS
             (long captions that fuse several speakers under one name)
  raw-word   word-level max-overlap vs RAW halo intervals (v0.1.43)   -> SHREDS
             (follows the 90-300ms halo strobe, fragments one speaker into many)
  debounced  word-level vs a DEBOUNCED dominant-speaker TRACK (NEW)   -> middle
             (halo blips dropped + hysteresis hold; cut at *sustained* turns)

No ground-truth who-said-what exists for this corpus, so we score with PURITY:
for each emitted caption, the fraction of halo speaking-time inside its span
that belongs to the assigned speaker. Welds -> low purity (span covers several
speakers). Shred -> high purity but a flood of micro-captions. The win is high
purity AND a sane caption count / median length.

Usage:
    venv/bin/python debug/14_34_audio_replay/boundary_attribution.py <slug> \
        [--rel-start S] [--rel-end S] [--blip 0.3] [--hold 0.6]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from load_corpus import load, Corpus, LegAudio  # noqa: E402
from word_level_attribution import (  # noqa: E402
    intervals_from_timeline,
    speaker_of_word,
    group_words,
)

_FW_MODEL_REPO = "deepdml/faster-whisper-large-v3-turbo-ct2"
_FW_COMPUTE_TYPE = "int8"
_FW_BEAM_SIZE = 5


def build_debounced_track(intervals, blip=0.3, hold=0.6, grid=0.05):
    """Collapse strobing halo intervals into committed dominant-speaker turns.

    1. drop sub-`blip` intervals (the ping-pong slivers)
    2. walk a fine grid; at each instant pick the locally dominant active
       speaker, but only COMMIT a switch once a new speaker has been dominant
       continuously for `hold` seconds (hysteresis). The first speaker commits
       immediately; silence holds the current committed speaker.
    Returns [(t0, t1, name)] committed turns."""
    iv = [(s, e, n) for s, e, n in intervals if e != float("inf") and e - s >= blip]
    if not iv:
        return []
    t_start = min(s for s, _, _ in iv)
    t_end = max(e for _, e, _ in iv)

    def candidate(t, committed, W=0.5):
        active = [(s, e, n) for s, e, n in iv if s <= t < e]
        if not active:
            return None
        if len(active) == 1:
            return active[0][2]
        best, best_ov = None, -1.0
        for s, e, n in active:
            ov = min(e, t + W) - max(s, t - W)
            if n == committed:
                ov += 1e-6  # tie-break sticky toward the current speaker
            if ov > best_ov:
                best_ov, best = ov, n
        return best

    committed = pending = pending_since = None
    track_grid = []
    t = t_start
    while t < t_end:
        cand = candidate(t, committed)
        if cand is None:
            pass  # hold committed through gaps
        elif committed is None:
            committed, pending = cand, None
        elif cand == committed:
            pending = None
        elif cand == pending:
            if t - pending_since >= hold:
                committed, pending = cand, None
        else:
            pending, pending_since = cand, t
        track_grid.append((t, committed))
        t += grid

    out = []
    for t, spk in track_grid:
        if spk is None:
            continue
        if out and out[-1][2] == spk:
            out[-1] = (out[-1][0], t + grid, spk)
        else:
            out.append((t, t + grid, spk))
    return out


def speaker_of_word_track(track, w0, w1, default):
    """Assign a word to the committed-track turn it overlaps most."""
    best, best_ov = "", 0.0
    for s, e, n in track:
        ov = max(0.0, min(e, w1) - max(s, w0))
        if ov > best_ov:
            best_ov, best = ov, n
    if best:
        return best
    if track:  # word fell in a gap -> nearest turn by start edge
        return min(track, key=lambda iv: abs(iv[0] - w0))[2]
    return default


def purity(c0, c1, assigned, intervals):
    """Fraction of halo speaking-time in [c0,c1] belonging to `assigned`."""
    tot = mine = 0.0
    for s, e, n in intervals:
        ov = max(0.0, min(e, c1) - max(s, c0))
        if ov > 0:
            tot += ov
            if n == assigned:
                mine += ov
    return (mine / tot) if tot > 0 else None


def _emit(label, segs, intervals, audio_t0):
    print(f"--- {label} ---")
    purs, wlens = [], []
    for spk, ws in segs:
        t0 = ws[0]["start"]
        t1 = ws[-1]["end"]
        text = "".join(w["word"] for w in ws).strip()
        p = purity(t0, t1, spk, intervals)
        purs.append(p if p is not None else 1.0)
        wlens.append(len(text.split()))
        ptag = f"{p:.0%}" if p is not None else "  -"
        print(f"  [{t0 - audio_t0:6.1f}s] pur={ptag:>4} {spk:14s} | {text}")
    n = len(segs)
    avg_p = statistics.mean(purs) if purs else 0.0
    med_w = statistics.median(wlens) if wlens else 0
    print(f"  >> {n} captions  median {med_w:.0f} words  avg purity {avg_p:.0%}\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("slug")
    ap.add_argument("--rel-start", type=float, default=128.0)
    ap.add_argument("--rel-end", type=float, default=153.0)
    ap.add_argument("--blip", type=float, default=0.3)
    ap.add_argument("--hold", type=float, default=0.6)
    args = ap.parse_args()

    c: Corpus = load(args.slug)
    leg: LegAudio = c.s
    audio_t0 = leg.first_sample_wall_clock
    start_wall = audio_t0 + args.rel_start
    end_wall = audio_t0 + args.rel_end
    i0, i1 = leg.index_at(start_wall), leg.index_at(end_wall)
    clip = leg.samples[i0:i1]
    clip_t0 = leg.sample_t(i0)
    intervals = intervals_from_timeline(c.timeline)
    track = build_debounced_track(intervals, blip=args.blip, hold=args.hold)

    print(f"=== {args.slug}  window [{args.rel_start:.0f}s, {args.rel_end:.0f}s] "
          f"({end_wall - start_wall:.0f}s)  blip={args.blip} hold={args.hold} ===\n")

    print("--- debounced track turns in window ---")
    for s, e, n in track:
        if e > start_wall and s < end_wall:
            print(f"  [{s - audio_t0:6.1f} - {e - audio_t0:6.1f}] ({e - s:4.1f}s) {n}")
    print()

    print("--- LIVE captions (single-winner, shipped JSONL) ---")
    with c.meeting_jsonl_path.open() as f:
        live = [json.loads(l) for l in f if l.strip()]
    live = [d for d in live if d.get("kind") == "caption"
            and start_wall - 10 <= d.get("timestamp", 0) <= end_wall + 4
            and d.get("sender") != "Jojo Shapiro"]
    lp = []
    for d in live:
        ts = d["timestamp"]
        p = purity(ts, ts + max(len(d["text"].split()) / 2.5, 2), d["sender"], intervals)
        lp.append(p if p is not None else 1.0)
        ptag = f"{p:.0%}" if p is not None else "  -"
        print(f"  [{ts - audio_t0:6.1f}s] pur={ptag:>4} {d['sender']:14s} | {d['text']}")
    if live:
        print(f"  >> {len(live)} captions  avg purity "
              f"{statistics.mean(lp):.0%}  (note: post-transcribe timestamps, approximate)\n")
    else:
        print("  (none)\n")

    from faster_whisper import WhisperModel
    print(f"loading {_FW_MODEL_REPO} ...", flush=True)
    model = WhisperModel(_FW_MODEL_REPO, device="cpu", compute_type=_FW_COMPUTE_TYPE)
    segments, _ = model.transcribe(
        clip, beam_size=_FW_BEAM_SIZE, word_timestamps=True, language="en")

    words_raw, words_tr = [], []
    for seg in segments:
        for w in (seg.words or []):
            w0, w1 = clip_t0 + w.start, clip_t0 + w.end
            words_raw.append({"speaker": speaker_of_word(intervals, w0, w1, "?"),
                              "word": w.word, "start": w0, "end": w1})
            words_tr.append({"speaker": speaker_of_word_track(track, w0, w1, "?"),
                             "word": w.word, "start": w0, "end": w1})
    if not words_raw:
        print("!! no words from whisper for this clip")
        return
    print()
    _emit("raw-word (vs strobing halo intervals — v0.1.43)",
          group_words(words_raw), intervals, audio_t0)
    _emit("debounced-track (NEW — vs committed dominant-speaker turns)",
          group_words(words_tr), intervals, audio_t0)


if __name__ == "__main__":
    main()
