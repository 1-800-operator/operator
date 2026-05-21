"""Score attribution strategies against the hand-labeled ground truth.

Reads labelkit/<slug>/<win>_label.txt (annotated with [Name] turn markers) and
the cached <win>_words.json, aligns the labeled word stream to the exact whisper
words by sequence-match, then for every word compares the GROUND-TRUTH speaker
against three attribution strategies:

  weld       single-winner over the whole window (max total halo overlap)  -> the OLD path
  raw-word   word-level max-overlap vs RAW strobing halo intervals (v0.1.43) -> SHREDS
  debounced  word-level vs the debounced dominant-speaker track (NEW)        -> proposed fix

Reports per-word accuracy (unweighted + duration-weighted) and sweeps the
debounce (blip, hold) settings so we can pick one. This is the FAIR test:
ground truth is independent of the halo.

Usage:
    venv/bin/python debug/14_34_audio_replay/score_against_labels.py <slug>
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from load_corpus import load  # noqa: E402
from word_level_attribution import intervals_from_timeline, speaker_of_word  # noqa: E402
from boundary_attribution import build_debounced_track, speaker_of_word_track  # noqa: E402

WINDOWS = ["winA_128-153", "winB_585-612"]
_TOK = re.compile(r"[a-z0-9']+")


def norm_tokens(s: str) -> list[str]:
    return _TOK.findall(s.lower())


# canonical full names (must match the halo-timeline names) keyed by substrings
# we accept in labels — handles bare/short/typo'd names in any case.
_NAME_KEYS = [
    ("matt", "Matthew Gorski"), ("gorski", "Matthew Gorski"),
    ("michael", "Michael Powell"), ("mike", "Michael Powell"), ("powell", "Michael Powell"),
    ("kyle", "Kyle Butler"), ("butler", "Kyle Butler"),
]


def canon_speaker(tok: str):
    """-> canonical full name, or None for unknown ('?', blank, unrecognized)."""
    t = tok.strip().strip("[]").strip().lower()
    if not t or t == "?" or t.startswith("unknown"):
        return None
    for key, full in _NAME_KEYS:
        if key in t:
            return full
    return None  # unrecognized -> treat as unknown (excluded from scoring)


def parse_label(path: Path):
    """Per-line labels: each turn on its own line, leading speaker token either
    bracketed ('[Kyle Butler] ...', '[matt ] ...', '[ ?] ...') or bare
    ('michael ...'). -> list of (canonical_speaker_or_None, [normalized tokens])."""
    out = []
    for raw in path.read_text().splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("["):
            end = s.find("]")
            spk_tok, rest = (s[1:end], s[end + 1:]) if end != -1 else (s, "")
        else:
            parts = s.split(None, 1)
            spk_tok, rest = parts[0], (parts[1] if len(parts) > 1 else "")
        toks = norm_tokens(rest)
        if toks:
            out.append((canon_speaker(spk_tok), toks))
    return out


def gt_per_word(words, label_turns):
    """Assign each cached word a ground-truth speaker by aligning the cached
    word stream to the labeled word stream (difflib), inheriting across gaps."""
    label_toks, label_spk = [], []
    for spk, toks in label_turns:
        for t in toks:
            label_toks.append(t)
            label_spk.append(spk)
    cached_toks = [norm_tokens(w["word"]) for w in words]
    flat = [(i, t) for i, ts in enumerate(cached_toks) for t in ts]
    cached_flat = [t for _, t in flat]

    UNSET = "\x00UNSET"  # alignment gap (inherit); distinct from None (unknown)
    sm = difflib.SequenceMatcher(a=cached_flat, b=label_toks, autojunk=False)
    gt_flat = [UNSET] * len(cached_flat)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                gt_flat[i1 + k] = label_spk[j1 + k]  # may be None (unknown turn)
    # forward-fill alignment gaps from the previous EXPLICIT label
    last = UNSET
    for k in range(len(gt_flat)):
        if gt_flat[k] is UNSET:
            gt_flat[k] = last
        else:
            last = gt_flat[k]
    first = next((g for g in gt_flat if g is not UNSET), None)
    gt_flat = [(first if g is UNSET else g) for g in gt_flat]
    # collapse flat-token GT back to one speaker per cached word (majority;
    # None survives as genuine "unknown" and is excluded from scoring later).
    word_gt = []
    pos = 0
    for ts in cached_toks:
        if not ts:
            word_gt.append(word_gt[-1] if word_gt else first)
            continue
        seg = gt_flat[pos:pos + len(ts)]
        pos += len(ts)
        word_gt.append(max(set(seg), key=seg.count))
    return word_gt


def acc(words, gt, pred):
    n = sum(1 for g, p in zip(gt, pred) if g == p)
    tot_t = sum(w["w1"] - w["w0"] for w in words)
    good_t = sum((w["w1"] - w["w0"]) for w, g, p in zip(words, gt, pred) if g == p)
    return n / len(words), (good_t / tot_t if tot_t else 0.0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("slug")
    args = ap.parse_args()
    c = load(args.slug)
    intervals = intervals_from_timeline(c.timeline)
    kit = Path(__file__).resolve().parent / "labelkit" / args.slug

    grand = {}  # method -> [correct_words, total_words]
    for win in WINDOWS:
        words = json.loads((kit / f"{win}_words.json").read_text())
        turns = parse_label(kit / f"{win}_label.txt")
        gt = gt_per_word(words, turns)
        n_turns = len(turns)
        n_changes = sum(1 for i in range(1, len(gt)) if gt[i] != gt[i - 1])

        # weld baseline: one winner for the whole window
        w0, w1 = words[0]["w0"], words[-1]["w1"]
        weld_spk = speaker_of_word(intervals, w0, w1, "?")
        weld_pred = [weld_spk] * len(words)
        raw_pred = [speaker_of_word(intervals, w["w0"], w["w1"], "?") for w in words]

        print(f"=== {win}: {len(words)} words, {n_turns} labeled turns, "
              f"{n_changes} word-level speaker changes ===")
        a = acc(words, gt, weld_pred)
        print(f"  weld (single-winner)         word={a[0]:5.0%}  time={a[1]:5.0%}   (always '{weld_spk}')")
        grand.setdefault("weld", [0, 0])
        a = acc(words, gt, raw_pred)
        grand.setdefault("raw", [0, 0])
        print(f"  raw-word (v0.1.43)           word={a[0]:5.0%}  time={a[1]:5.0%}")

        # debounced sweep
        best = None
        print("  debounced sweep (blip, hold):")
        for blip in (0.2, 0.3):
            for hold in (0.4, 0.6, 0.8, 1.0):
                track = build_debounced_track(intervals, blip=blip, hold=hold)
                pred = [speaker_of_word_track(track, w["w0"], w["w1"], "?") for w in words]
                aw = acc(words, gt, pred)
                tag = f"    blip={blip} hold={hold}: word={aw[0]:5.0%} time={aw[1]:5.0%}"
                print(tag)
                if best is None or aw[0] > best[0]:
                    best = (aw[0], blip, hold, pred)
        # accumulate grand totals on the canonical (0.3, 0.6) + raw + weld
        for name, pred in (("weld", weld_pred), ("raw", raw_pred)):
            g = grand[name]
            g[0] += sum(1 for x, y in zip(gt, pred) if x == y)
            g[1] += len(words)
        track = build_debounced_track(intervals, blip=0.3, hold=0.6)
        deb_pred = [speaker_of_word_track(track, w["w0"], w["w1"], "?") for w in words]
        grand.setdefault("debounced(.3/.6)", [0, 0])
        gd = grand["debounced(.3/.6)"]
        gd[0] += sum(1 for x, y in zip(gt, deb_pred) if x == y)
        gd[1] += len(words)
        print(f"  best this window: blip={best[1]} hold={best[2]} word={best[0]:5.0%}\n")

    print("=== GRAND TOTAL (both windows, per-word) ===")
    for name, (good, tot) in grand.items():
        print(f"  {name:20s} {good}/{tot} = {good / tot:.0%}")


if __name__ == "__main__":
    main()
