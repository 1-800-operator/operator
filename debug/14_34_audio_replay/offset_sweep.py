"""Pin the word-clock vs halo offset against all hand-labeled windows.

Auto-discovers every labelkit/<slug>/*_label.txt that has been annotated (i.e.
no longer starts with the '[ ]' placeholder), aligns to its cached words, and
sweeps a fixed offset added to word timestamps before halo attribution. Reports
per-window and pooled accuracy so we can pick a robust offset.

Usage:
    venv/bin/python debug/14_34_audio_replay/offset_sweep.py <slug>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from load_corpus import load  # noqa: E402
from word_level_attribution import intervals_from_timeline, speaker_of_word  # noqa: E402
from score_against_labels import parse_label, gt_per_word  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("slug")
    args = ap.parse_args()
    c = load(args.slug)
    iv = intervals_from_timeline(c.timeline)
    kit = Path(__file__).resolve().parent / "labelkit" / args.slug

    windows = []
    for lab in sorted(kit.glob("*_label.txt")):
        # annotated? last non-comment line should not start with "[ ]"
        content = [l for l in lab.read_text().splitlines()
                   if l.strip() and not l.lstrip().startswith("#")]
        if not content or content[-1].lstrip().startswith("[ ]"):
            print(f"  (skip unlabeled {lab.stem})")
            continue
        win = lab.stem[:-len("_label")]
        wj = kit / f"{win}_words.json"
        if not wj.exists():
            continue
        words = json.loads(wj.read_text())
        turns = parse_label(lab)
        gt = gt_per_word(words, turns)
        n_score = sum(1 for g in gt if g is not None)
        windows.append((win, words, gt, n_score))
        spk_set = sorted({s or "?" for s, _ in turns})
        print(f"  {win}: {len(words)} words, {len(turns)} turns, "
              f"{n_score} scoreable ({len(words)-n_score} unknown)  speakers={spk_set}")

    if not windows:
        print("no labeled windows found")
        return
    total_words = sum(n for _, _, _, n in windows)
    print(f"\n{len(windows)} labeled windows, {total_words} scoreable words\n")

    offsets = list(range(-300, 451, 50))
    # per-window peak + pooled curve
    print(f"{'offset(ms)':>10}  pooled" + "".join(f"{w[:9]:>11}" for w, _, _, _ in windows))
    best = None
    for off_ms in offsets:
        off = off_ms / 1000.0
        pooled_good = 0
        cells = []
        for _, words, gt, n_score in windows:
            good = sum(1 for w, g in zip(words, gt)
                       if g is not None
                       and speaker_of_word(iv, w["w0"] + off, w["w1"] + off, "?") == g)
            pooled_good += good
            cells.append(f"{good/n_score:>10.0%} " if n_score else f"{'-':>10} ")
        acc = pooled_good / total_words
        if best is None or acc > best[1]:
            best = (off_ms, acc)
        mark = " *" if off_ms == 0 else "  "
        print(f"{off_ms:>+8d}{mark}{acc:>7.1%} " + "".join(cells))
    print(f"\nbest pooled offset: {best[0]:+d}ms -> {best[1]:.1%}   (0ms = current production)")


if __name__ == "__main__":
    main()
