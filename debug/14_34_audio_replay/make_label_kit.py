"""Build a hand-labeling kit for a cross-talk window of the S (remote) leg.

For each window it writes, into ./labelkit/<slug>/:
  <win>.wav            — playable 16kHz clip (open in QuickTime / Finder)
  <win>_words.json     — cached whisper words+timestamps (scoring uses THIS exact
                         transcription, so labels align by word index)
  <win>_label.txt      — the transcript as flowing text for you to annotate:
                         drop a [Name] marker wherever the speaker changes.

Then score_against_labels.py reads the annotated _label.txt back.

Usage:
    venv/bin/python debug/14_34_audio_replay/make_label_kit.py <slug>
"""
from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from load_corpus import load, Corpus  # noqa: E402

_FW_MODEL_REPO = "deepdml/faster-whisper-large-v3-turbo-ct2"

# (window name, rel_start_s, rel_end_s). winA/winB already labeled; these four
# are handoff-dense windows added to pin the word-clock offset. The script skips
# any window whose _label.txt already exists, so re-running won't clobber labels.
WINDOWS = [
    ("winC_90-115", 90.0, 115.0),
    ("winD_215-240", 215.0, 240.0),
    ("winE_525-550", 525.0, 550.0),
    ("winF_615-640", 615.0, 640.0),
]


def write_wav(path: Path, samples: np.ndarray, sr: int) -> None:
    pcm = np.clip(samples, -1.0, 1.0)
    pcm16 = (pcm * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm16.tobytes())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("slug")
    args = ap.parse_args()

    c: Corpus = load(args.slug)
    leg = c.s
    sr = leg.sample_rate
    a0 = leg.first_sample_wall_clock
    speakers = sorted({n for _, n, _ in c.timeline})

    outdir = Path(__file__).resolve().parent / "labelkit" / args.slug
    outdir.mkdir(parents=True, exist_ok=True)

    from faster_whisper import WhisperModel
    print(f"loading {_FW_MODEL_REPO} ...", flush=True)
    model = WhisperModel(_FW_MODEL_REPO, device="cpu", compute_type="int8")

    for name, rs, re_ in WINDOWS:
        if (outdir / f"{name}_label.txt").exists():
            print(f"  skip {name}: label file already exists")
            continue
        i0, i1 = leg.index_at(a0 + rs), leg.index_at(a0 + re_)
        clip = leg.samples[i0:i1]
        clip_t0 = leg.sample_t(i0)
        write_wav(outdir / f"{name}.wav", clip, sr)

        segments, _ = model.transcribe(
            clip, beam_size=5, word_timestamps=True, language="en")
        words = []
        for seg in segments:
            for w in (seg.words or []):
                words.append({"word": w.word,
                              "w0": clip_t0 + w.start,
                              "w1": clip_t0 + w.end})
        (outdir / f"{name}_words.json").write_text(json.dumps(words, indent=0))

        text = "".join(w["word"] for w in words).strip()
        sheet = (
            f"# LABEL SHEET — {args.slug}  {name}  (window {rs:.0f}-{re_:.0f}s)\n"
            f"# Listen to {name}.wav. Speakers in this meeting (remote/system leg):\n"
            f"#   {', '.join(speakers)}\n"
            f"# Drop a [Name] marker at the START of the transcript and again\n"
            f"# EVERY time the speaker changes. Exact name spelling matters.\n"
            f"# If a word/chunk is unintelligible, just leave it under whoever's\n"
            f"# speaking. Don't worry about whisper's transcription errors — we\n"
            f"# only need the speaker boundaries, not perfect text.\n"
            f"# Example:  [Matthew Gorski] file add folder right [Kyle Butler] exactly yeah ...\n"
            f"#\n"
            f"[ ] {text}\n"
        )
        (outdir / f"{name}_label.txt").write_text(sheet)
        print(f"  wrote {name}: {len(clip)/sr:.0f}s wav, {len(words)} words")

    print(f"\nlabel kit -> {outdir}")
    print("open the .wav files, annotate the _label.txt files, then run score_against_labels.py")


if __name__ == "__main__":
    main()
