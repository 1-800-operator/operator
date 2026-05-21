"""While we have the oracle: try levers beyond a single symmetric offset.

  1. error breakdown @ best symmetric offset — how many wrong words are short
     backchannels vs gap-fallbacks (no halo overlap at all)?
  2. 2-D edge sweep — shift interval START and END independently (halo attack
     vs release are different latencies). Can it beat the symmetric +100ms?
  3. midpoint vs interval-overlap attribution.
  4. post-hoc smoothing (_group_words absorb of lone flips) vs accuracy — does
     absorbing 1-word flips fix mis-slices or destroy real backchannels?
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from load_corpus import load
from word_level_attribution import intervals_from_timeline, speaker_of_word, group_words
from score_against_labels import parse_label, gt_per_word

SLUG = "sqr-vyex-wob_20260520"
c = load(SLUG)
IV = intervals_from_timeline(c.timeline)
kit = Path(__file__).resolve().parent / "labelkit" / SLUG

WINS = []
for lab in sorted(kit.glob("*_label.txt")):
    content = [l for l in lab.read_text().splitlines()
               if l.strip() and not l.lstrip().startswith("#")]
    if not content or content[-1].lstrip().startswith("[ ]"):
        continue
    win = lab.stem[:-len("_label")]
    words = json.loads((kit / f"{win}_words.json").read_text())
    gt = gt_per_word(words, parse_label(lab))
    WINS.append((win, words, gt))
N = sum(sum(1 for g in gt if g is not None) for _, _, gt in WINS)
print(f"{len(WINS)} windows, {N} scoreable words\n")


# Convention everywhere below: shift the WORD by (a,b) -> attribute interval
# [w0+a, w1+b] against the halo. Matches offset_sweep (+100ms word = best).
def attr_edges(w, a, b):
    return speaker_of_word(IV, w["w0"] + a, w["w1"] + b, "?")


def score(attr):
    good = tot = 0
    for _, words, gt in WINS:
        for w, g in zip(words, gt):
            if g is None:
                continue
            tot += 1
            good += (attr(w) == g)
    return good, tot


# --- 1. error breakdown @ +100ms (word shift, the GOOD direction) ---
BACKCH = {"yeah", "yep", "right", "cool", "ok", "okay", "sure", "nice", "mm",
          "mhm", "uh", "um", "oh", "huh", "yes", "no", "always", "exactly", "got"}
off = 0.10
n_err = n_back = n_gap = n_short = 0
for _, words, gt in WINS:
    for w, g in zip(words, gt):
        if g is None:
            continue
        p = attr_edges(w, off, off)
        wt0, wt1 = w["w0"] + off, w["w1"] + off
        ov = any(min(t1, wt1) - max(t0, wt0) > 0 for t0, t1, _ in IV)
        if p != g:
            n_err += 1
            tok = "".join(ch for ch in w["word"].lower() if ch.isalpha())
            n_back += tok in BACKCH
            n_short += len(tok) <= 3
            n_gap += not ov
print(f"1) errors @ +100ms: {n_err}/{N} ({100*(N-n_err)//N}% acc)  "
      f"backchannel-word {n_back} ({100*n_back//max(n_err,1)}%)  "
      f"<=3char {n_short} ({100*n_short//max(n_err,1)}%)  "
      f"gap-fallback {n_gap} ({100*n_gap//max(n_err,1)}%)\n")

# --- 2. 2-D word-edge sweep: shift start (a) and end (b) independently ---
print("2) 2-D word-edge sweep (start/end ms) — top 8 by pooled acc:")
results = []
rng = range(-150, 351, 50)
for a in rng:
    for b in rng:
        g, t = score(lambda w, a=a, b=b: attr_edges(w, a / 1000, b / 1000))
        results.append((g / t, a, b))
results.sort(reverse=True)
for acc, a, b in results[:8]:
    print(f"   start{a:+5d}  end{b:+5d}  -> {acc:5.1%}")
sym = next(acc for acc, a, b in results if a == 100 and b == 100)
print(f"   (symmetric +100/+100 = {sym:.1%})\n")

# --- 3. midpoint(point) vs interval-overlap, both @ +100ms ---
g, t = score(lambda w: speaker_of_word(IV, (w["w0"]+w["w1"])/2 + off, (w["w0"]+w["w1"])/2 + off, "?"))
print(f"3) point/midpoint@+100ms = {g/t:.1%}   vs interval-overlap@+100ms = {sym:.1%}\n")

# --- 4. smoothing vs accuracy ---
print("4) post-hoc smoothing (absorb lone flips < gap, flanked by same spk):")
for smooth in (0.0, 0.3, 0.5, 0.8, 1.2):
    good = tot = 0
    for _, words, gt in WINS:
        ww = [{"speaker": speaker_of_word(IV, w["w0"] + off, w["w1"] + off, "?"),
               "word": w["word"], "start": w["w0"], "end": w["w1"]} for w in words]
        segs = group_words(ww, smooth_gap=smooth)
        flat = []
        for spk, wl in segs:
            flat.extend((spk,) * len(wl))
        for sp, g in zip(flat, gt):
            if g is None:
                continue
            tot += 1
            good += (sp == g)
    print(f"   smooth={smooth}s -> {good/tot:.1%}")
