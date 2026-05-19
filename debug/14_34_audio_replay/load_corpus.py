"""Load an OPERATOR_AUDIO_RAW_DUMP corpus for offline VAD / attribution replay.

Produced by whisper_worker when OPERATOR_AUDIO_RAW_DUMP=1 during a meeting:

    ~/.operator/debug/raw_<slug>/
        S.f32        — system-audio leg, raw float32 LE, 16kHz mono, header-less
        M.f32        — mic leg, same format
        meta.json    — wall-clock anchors + byte counts + sample rate

Plus the paired DOM speaker-snapshot file (set OPERATOR_DEBUG_SPEAKER_SNAPSHOTS=1
in the same run):

    ~/.operator/debug/speaker_snapshots_<slug>.jsonl

The loader returns numpy arrays for S and M plus the snapshot timeline, all
keyed on wall-clock so they line up at sample resolution. From there a spike
can re-run any VAD config + attribution variant against deterministic input
and diff the resulting JSONL against the live one captured at meeting time.

Usage:
    python debug/14_34_audio_replay/load_corpus.py <slug>
        — prints summary stats for the corpus matching the slug.
    from load_corpus import load
    corpus = load("sqr-vyex-wob_20260519")
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class LegAudio:
    leg: str                    # "S" or "M"
    samples: np.ndarray         # float32, 16kHz mono
    first_sample_wall_clock: float  # time.time() at sample 0
    sample_rate: int            # always 16000 today

    def sample_t(self, i: int) -> float:
        return self.first_sample_wall_clock + i / self.sample_rate

    def index_at(self, wall_clock: float) -> int:
        return max(0, int(round((wall_clock - self.first_sample_wall_clock) * self.sample_rate)))


@dataclass
class Corpus:
    slug: str
    meeting_jsonl_path: Path
    speaker_snapshot_path: Path | None
    s: LegAudio
    m: LegAudio
    # (t, name, "start"|"stop"). t is wall-clock seconds.
    timeline: list[tuple[float, str, str]]


def load(slug: str, base_dir: str = "~/.operator/debug") -> Corpus:
    base = Path(os.path.expanduser(base_dir))
    raw_dir = base / f"raw_{slug}"
    meta_path = raw_dir / "meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"no raw dump corpus at {raw_dir} (run with OPERATOR_AUDIO_RAW_DUMP=1)")
    meta = json.loads(meta_path.read_text())
    if meta.get("dtype") != "float32" or meta.get("channels") != 1:
        raise ValueError(f"unexpected format in {meta_path}: {meta}")
    sr = int(meta["sample_rate"])

    def _load_leg(tag: str) -> LegAudio:
        info = meta[tag]
        path = raw_dir / info["path"]
        samples = np.fromfile(path, dtype=np.float32)
        return LegAudio(
            leg=tag,
            samples=samples,
            first_sample_wall_clock=float(info["first_byte_wall_clock"] or 0.0),
            sample_rate=sr,
        )

    s_leg = _load_leg("S")
    m_leg = _load_leg("M")

    # Paired speaker-snapshot file (optional — corpus is still loadable
    # without it; you just lose the DOM timeline).
    snapshot_path = base / f"speaker_snapshots_{slug}.jsonl"
    timeline: list[tuple[float, str, str]] = []
    if snapshot_path.exists():
        with snapshot_path.open() as f:
            for line in f:
                d = json.loads(line)
                ev = d.get("event") or {}
                name = ev.get("name") or ""
                if not name:
                    continue
                kind = "start" if ev.get("speaking") else "stop"
                timeline.append((float(d["t"]), name, kind))
        timeline.sort()
    else:
        snapshot_path = None  # type: ignore[assignment]

    return Corpus(
        slug=slug,
        meeting_jsonl_path=Path(meta["meeting_jsonl_path"]),
        speaker_snapshot_path=snapshot_path,
        s=s_leg,
        m=m_leg,
        timeline=timeline,
    )


def _stats(c: Corpus) -> None:
    def _leg_summary(leg: LegAudio) -> str:
        dur = len(leg.samples) / leg.sample_rate if leg.samples.size else 0.0
        rms = float(np.sqrt(np.mean(leg.samples ** 2))) if leg.samples.size else 0.0
        return (
            f"  {leg.leg}: {len(leg.samples):>10d} samples  "
            f"{dur:>6.1f}s  rms={rms:.4f}  "
            f"first_byte_t={leg.first_sample_wall_clock:.3f}"
        )

    print(f"slug: {c.slug}")
    print(f"meeting_jsonl: {c.meeting_jsonl_path}")
    print(f"speaker_snapshot: {c.speaker_snapshot_path or '(none)'}")
    print(_leg_summary(c.s))
    print(_leg_summary(c.m))
    if c.timeline:
        speakers = sorted({n for _, n, _ in c.timeline})
        print(f"  timeline: {len(c.timeline)} events across {len(speakers)} speakers")
        print(f"    speakers: {', '.join(speakers)}")
        t0 = c.timeline[0][0]
        t1 = c.timeline[-1][0]
        print(f"    span: {t1 - t0:.1f}s")
    else:
        print("  timeline: (no snapshot file paired)")


def _main() -> int:
    ap = argparse.ArgumentParser(description="Load + summarize an OPERATOR_AUDIO_RAW_DUMP corpus.")
    ap.add_argument("slug", help="meeting slug (e.g. sqr-vyex-wob_20260519)")
    ap.add_argument("--base", default="~/.operator/debug", help="debug base dir")
    args = ap.parse_args()
    try:
        corpus = load(args.slug, base_dir=args.base)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    _stats(corpus)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
