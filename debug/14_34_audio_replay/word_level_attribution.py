"""Offline experiment: word-level speaker attribution vs the live single-winner.

Motivation (S250 cross-talk bug): the live path transcribes one VAD utterance,
then `_attribute_speaker` stamps the WHOLE window with one speaker (max total
overlap). On cross-talk that merges several people's words under one name.

This spike re-transcribes a window of the S (system / remote) leg with
faster-whisper `word_timestamps=True`, maps each WORD to the speaker whose DOM
speaking-interval it overlaps most, then groups consecutive same-speaker words
into separate captions. It prints the live captions for the same window beside
the new word-level split so we can eyeball whether it does better.

Per-word timing also removes the `chunk_end=time.time()` post-transcribe bias
for free — each word carries its own wall-clock.

Usage:
    venv/bin/python debug/14_34_audio_replay/word_level_attribution.py <slug> \
        [--start-wall <epoch>] [--end-wall <epoch>]
    # default window brackets the Michael Powell cross-talk example.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from load_corpus import load, Corpus, LegAudio  # noqa: E402

# Match audio.py production model config exactly (turbo, CPU, int8, beam 5).
_FW_MODEL_REPO = "deepdml/faster-whisper-large-v3-turbo-ct2"
_FW_COMPUTE_TYPE = "int8"
_FW_BEAM_SIZE = 5


def intervals_from_timeline(timeline):
    """[(t,name,'start'|'stop')] -> [(t0,t1,name)] wall-clock speaking intervals."""
    open_starts: dict[str, float] = {}
    intervals: list[tuple[float, float, str]] = []
    for t, name, kind in timeline:
        if kind == "start":
            if name in open_starts:
                intervals.append((open_starts[name], t, name))
            open_starts[name] = t
        else:
            t0 = open_starts.pop(name, None)
            if t0 is not None:
                intervals.append((t0, t, name))
    for name, t0 in open_starts.items():
        intervals.append((t0, float("inf"), name))
    return intervals


def speaker_of_word(intervals, w0: float, w1: float, default: str) -> str:
    """Max-overlap attribution for a single word's [w0,w1] wall-clock span."""
    best_name, best_overlap = "", 0.0
    for t0, t1, name in intervals:
        overlap = max(0.0, min(t1, w1) - max(t0, w0))
        if overlap > best_overlap:
            best_overlap, best_name = overlap, name
    if best_name:
        return best_name
    # Fallback: most recent speaker to have stopped before this word.
    prior = [(t1, name) for (t0, t1, name) in intervals if t1 <= w0]
    if prior:
        prior.sort(reverse=True)
        return prior[0][1]
    return default


def group_words(words, smooth_gap=0.0):
    """Group consecutive same-speaker words into segments.

    smooth_gap>0: a lone flip shorter than smooth_gap that is flanked by the
    SAME speaker on both sides gets absorbed back into that speaker (removes
    fragmentation from a single mis-timed word / a sub-half-second halo blip).
    Applied iteratively until stable."""
    segs = []  # [speaker, [words...]]
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
            ws = segs[i][1]
            dur = ws[-1]["end"] - ws[0]["start"]
            if segs[i - 1][0] == segs[i + 1][0] and dur < smooth_gap:
                segs[i - 1][1].extend(ws)
                segs[i - 1][1].extend(segs[i + 1][1])
                del segs[i:i + 2]
                changed = True
                break
    return segs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("slug")
    ap.add_argument("--start-wall", type=float, default=None)
    ap.add_argument("--end-wall", type=float, default=None)
    ap.add_argument("--smooth", type=float, default=0.0,
                    help="merge lone speaker-flips shorter than this many seconds")
    args = ap.parse_args()

    c: Corpus = load(args.slug)
    leg: LegAudio = c.s
    sr = leg.sample_rate
    audio_t0 = leg.first_sample_wall_clock
    audio_t1 = leg.sample_t(len(leg.samples))

    # Default window: bracket the Michael Powell example if present, else whole.
    start_wall = args.start_wall if args.start_wall is not None else audio_t0 + 75.0
    end_wall = args.end_wall if args.end_wall is not None else audio_t0 + 100.0
    start_wall = max(start_wall, audio_t0)
    end_wall = min(end_wall, audio_t1)

    i0 = leg.index_at(start_wall)
    i1 = leg.index_at(end_wall)
    clip = leg.samples[i0:i1]
    clip_t0 = leg.sample_t(i0)
    print(f"=== window: [{start_wall:.1f}, {end_wall:.1f}] wall  "
          f"({end_wall - start_wall:.1f}s, {len(clip)} samples) ===\n")

    # --- live captions in this window (single-winner, for comparison) ---
    print("--- LIVE captions (single-winner) in window ---")
    live = []
    with c.meeting_jsonl_path.open() as f:
        for line in f:
            d = json.loads(line)
            if d.get("kind") != "caption":
                continue
            ts = d.get("timestamp", 0)
            if start_wall - 12 <= ts <= end_wall + 4:  # ts is post-transcribe
                live.append(d)
                print(f"  [{ts - audio_t0:6.1f}s] {d.get('sender'):14s} | {d.get('text')}")
    if not live:
        print("  (none)")
    print()

    # --- transcribe the clip with word timestamps ---
    from faster_whisper import WhisperModel
    print(f"loading {_FW_MODEL_REPO} ...", flush=True)
    model = WhisperModel(_FW_MODEL_REPO, device="cpu", compute_type=_FW_COMPUTE_TYPE)
    segments, _info = model.transcribe(
        clip, beam_size=_FW_BEAM_SIZE, word_timestamps=True, language="en",
    )

    intervals = intervals_from_timeline(c.timeline)
    words = []
    for seg in segments:
        for w in (seg.words or []):
            w0 = clip_t0 + w.start
            w1 = clip_t0 + w.end
            spk = speaker_of_word(intervals, w0, w1, default="?")
            words.append({"speaker": spk, "word": w.word, "start": w0, "end": w1})

    if not words:
        print("!! no words returned by whisper for this clip")
        return

    # --- NEW word-level split captions ---
    for label, smooth in (("naive", 0.0), (f"smoothed<{args.smooth}s", args.smooth)):
        if smooth == 0.0 and label != "naive":
            continue
        segs = group_words(words, smooth_gap=smooth)
        print(f"--- NEW word-level captions ({label}) ---")
        for spk, ws in segs:
            t = ws[0]["start"] - audio_t0
            text = "".join(w["word"] for w in ws).strip()
            print(f"  [{t:6.1f}s] {spk:14s} | {text}")
        print()


if __name__ == "__main__":
    main()
