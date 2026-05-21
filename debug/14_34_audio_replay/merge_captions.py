"""Rendering fix prototype: merge consecutive same-speaker captions.

Attribution is already ~95% right (hand-label test). The "captions split up a
lot" complaint is fragmentation: one person talking across several VAD
utterances becomes several captions. This merges adjacent SAME-speaker captions
separated by < gap seconds into one. It NEVER changes which speaker a word is
assigned to (only merges runs already attributed to the same person), so the
95% per-word accuracy is preserved by construction. A real cross-speaker
interjection (A B A) is left as three captions — that's correct, not shred.

Usage:
    venv/bin/python debug/14_34_audio_replay/merge_captions.py <meeting.jsonl> [--gap 3]
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def spk(c):
    return c.get("speaker") or c.get("sender") or "?"


def merge(caps, gap):
    out = []
    for c in caps:
        if out and spk(out[-1]) == spk(c) and c["timestamp"] - out[-1]["_end"] <= gap:
            out[-1]["text"] = (out[-1]["text"].rstrip() + " " + c["text"].lstrip()).strip()
            out[-1]["_end"] = c["timestamp"]
        else:
            d = dict(c)
            d["_end"] = c["timestamp"]
            out.append(d)
    return out


def stats(caps, label):
    wl = [len(c["text"].split()) for c in caps]
    micro = sum(1 for w in wl if w <= 2)
    sw = sum(1 for i in range(1, len(caps)) if spk(caps[i]) != spk(caps[i - 1]))
    print(f"  {label:22s} {len(caps):4d} caps  median {statistics.median(wl):2.0f}w  "
          f"<=2w {micro:3d} ({100*micro/len(caps):2.0f}%)  switches {sw}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl")
    ap.add_argument("--gap", type=float, default=3.0)
    ap.add_argument("--show", type=float, default=None, help="rel-seconds to show a 25s sample window")
    args = ap.parse_args()
    rows = [json.loads(l) for l in Path(args.jsonl).read_text().splitlines() if l.strip()]
    caps = [r for r in rows if r.get("kind") == "caption"]
    t0 = caps[0]["timestamp"]

    print(f"=== {Path(args.jsonl).name} ===")
    stats(caps, "original")
    for g in (1.0, 2.0, 3.0, 5.0):
        stats(merge(caps, g), f"merged gap<={g}s")

    win = args.show if args.show is not None else 250.0
    print(f"\n--- sample window (orig vs merged gap<={args.gap}s) @ rel {win:.0f}s ---")
    merged = merge(caps, args.gap)
    for tag, cs in (("ORIG", caps), ("MERGED", merged)):
        print(f"  [{tag}]")
        for c in cs:
            if 0 <= c["timestamp"] - t0 - win < 25:
                print(f"    {c['timestamp']-t0:6.1f} {spk(c)[:14]:14s} ({len(c['text'].split()):2d}w) {c['text'][:58]}")


if __name__ == "__main__":
    main()
