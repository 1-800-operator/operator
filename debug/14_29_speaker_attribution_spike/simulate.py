"""Speaker attribution spike — current snapshot strategy vs timeline strategy.

Reproduces the Kyle/Michael flip seen in
~/.operator/history/dko-pgom-bfe.jsonl and validates that a
timeline-based attribution lookup eliminates it.

Run:
    python debug/14_29_speaker_attribution_spike/simulate.py

The simulator does NOT touch production code. It models two streams:
  (a) DOM speaking events  — (t, name, "start" | "stop"), driven by the
      observer in attach_adapter.py / chat_dom_js.py.
  (b) Whisper finalizations — (chunk_start, chunk_end, raw_text),
      emitted by the audio leg after Whisper commits a segment. The
      gap between chunk_end and the finalize timestamp models
      Whisper's silence-detection lag (typically 0.3-1.0s).

For each finalization we run two attribution strategies side by side:
  - current  : reads the speaking state AT FINALIZE TIME, falling back
               to _last_s_speaker (mirrors attach_adapter.py:1573-1581).
  - timeline : looks up who was speaking during the CHUNK's time window
               (proposed fix).

Output is a table per scenario showing how each strategy attributes
each utterance, plus a summary of mismatches against ground truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class SpeakingEvent:
    t: float
    name: str
    kind: Literal["start", "stop"]


@dataclass
class Finalization:
    chunk_start: float         # when the audio chunk began capturing
    chunk_end: float           # when speech actually ended in the chunk
    finalize_t: float          # when Whisper committed the segment
    text: str
    truth: str                 # ground-truth speaker (for scoring)


@dataclass
class Scenario:
    name: str
    description: str
    events: list[SpeakingEvent]
    finalizations: list[Finalization]


# --- Strategy A: current production logic --------------------------------
# Mirrors attach_adapter.py:1573-1581 — read current speaking set at
# the moment Whisper finalizes; if exactly one is active use it, else
# fall back to _last_s_speaker.

def current_strategy(scn: Scenario) -> list[str]:
    speaking: set[str] = set()
    last_speaker: str = ""
    out: list[str] = []
    ev_idx = 0
    for f in scn.finalizations:
        # advance event timeline up to finalize_t
        while ev_idx < len(scn.events) and scn.events[ev_idx].t <= f.finalize_t:
            e = scn.events[ev_idx]
            if e.kind == "start":
                speaking.add(e.name)
                last_speaker = e.name
            else:
                speaking.discard(e.name)
            ev_idx += 1
        active = list(speaking)
        if len(active) == 1:
            out.append(active[0])
        elif last_speaker:
            out.append(last_speaker)
        else:
            out.append("?")
    return out


# --- Strategy B: timeline-based attribution ------------------------------
# Build a list of speaking intervals [t_start, t_end, name] from the
# event stream, then for each finalization pick the speaker whose
# interval has the largest overlap with [chunk_start, chunk_end].
# Fallback: most-recent speaker whose interval ended at or before
# chunk_start.

def _intervals(events: list[SpeakingEvent]) -> list[tuple[float, float, str]]:
    """Convert start/stop events to closed intervals.

    Multiple concurrent speakers are allowed — each name's start/stop
    pair becomes its own interval, regardless of who else is talking.
    Unmatched 'start' (still speaking at end-of-scenario) gets a
    sentinel large end time so the lookup still works.
    """
    open_starts: dict[str, float] = {}
    out: list[tuple[float, float, str]] = []
    for e in events:
        if e.kind == "start":
            open_starts[e.name] = e.t
        else:
            t0 = open_starts.pop(e.name, None)
            if t0 is not None:
                out.append((t0, e.t, e.name))
    # close any still-open intervals at +inf
    for name, t0 in open_starts.items():
        out.append((t0, float("inf"), name))
    return out


def timeline_strategy(scn: Scenario) -> list[str]:
    intervals = _intervals(scn.events)
    out: list[str] = []
    for f in scn.finalizations:
        # max overlap with [chunk_start, chunk_end]
        best_name = ""
        best_overlap = 0.0
        for t0, t1, name in intervals:
            overlap = max(0.0, min(t1, f.chunk_end) - max(t0, f.chunk_start))
            if overlap > best_overlap:
                best_overlap = overlap
                best_name = name
        if best_name:
            out.append(best_name)
            continue
        # fallback: most-recent speaker whose interval ended at or before chunk_start
        candidates = [(t1, name) for (t0, t1, name) in intervals if t1 <= f.chunk_start]
        if candidates:
            candidates.sort(reverse=True)
            out.append(candidates[0][1])
        else:
            out.append("?")
    return out


# --- Scenarios ----------------------------------------------------------

def scenario_kyle_michael_flip() -> Scenario:
    """The exact pattern from dko-pgom-bfe.jsonl, idealized.

    Michael speaks 0..3, Kyle 3.2..7, Michael 7.2..10. Whisper has
    ~0.5s finalize lag, so each segment commits AFTER the next
    speaker has already started.
    """
    events = [
        SpeakingEvent(0.0,  "Michael", "start"),
        SpeakingEvent(3.0,  "Michael", "stop"),
        SpeakingEvent(3.2,  "Kyle",    "start"),
        SpeakingEvent(7.0,  "Kyle",    "stop"),
        SpeakingEvent(7.2,  "Michael", "start"),
        SpeakingEvent(10.0, "Michael", "stop"),
    ]
    finalizations = [
        Finalization(chunk_start=0.0, chunk_end=3.0,  finalize_t=3.5,  text="LOE 60h…",      truth="Michael"),
        Finalization(chunk_start=3.2, chunk_end=7.0,  finalize_t=7.5,  text="potentially…",  truth="Kyle"),
        Finalization(chunk_start=7.2, chunk_end=10.0, finalize_t=10.5, text="um things…",    truth="Michael"),
    ]
    return Scenario(
        name="Kyle/Michael flip (dko-pgom-bfe)",
        description="Back-to-back speakers, ~200ms gap, Whisper finalizes ~500ms post-stop.",
        events=events,
        finalizations=finalizations,
    )


def scenario_solo_speaker() -> Scenario:
    events = [
        SpeakingEvent(0.0, "Alice", "start"),
        SpeakingEvent(4.0, "Alice", "stop"),
    ]
    finalizations = [
        Finalization(chunk_start=0.0, chunk_end=4.0, finalize_t=4.5, text="hi all", truth="Alice"),
    ]
    return Scenario(
        name="Solo speaker",
        description="No overlap, no contention — both strategies should agree.",
        events=events,
        finalizations=finalizations,
    )


def scenario_three_way() -> Scenario:
    events = [
        SpeakingEvent(0.0,  "A", "start"),
        SpeakingEvent(2.0,  "A", "stop"),
        SpeakingEvent(2.3,  "B", "start"),
        SpeakingEvent(4.0,  "B", "stop"),
        SpeakingEvent(4.1,  "C", "start"),
        SpeakingEvent(6.0,  "C", "stop"),
        SpeakingEvent(6.5,  "A", "start"),
        SpeakingEvent(8.0,  "A", "stop"),
    ]
    finalizations = [
        Finalization(0.0, 2.0, 2.4, "A1", "A"),
        Finalization(2.3, 4.0, 4.2, "B1", "B"),
        Finalization(4.1, 6.0, 6.6, "C1", "C"),  # finalize AFTER A starts again
        Finalization(6.5, 8.0, 8.5, "A2", "A"),
    ]
    return Scenario(
        name="Three-way round-robin",
        description="A → B → C → A with tight handoffs and one late finalization.",
        events=events,
        finalizations=finalizations,
    )


def scenario_overlap() -> Scenario:
    """A and B speak simultaneously for a stretch.

    Whisper's segment captures mostly A's voice (longer interval
    overlap), so timeline-strategy should pick A. Current
    strategy at finalize_t sees both → falls back to last_speaker.
    """
    events = [
        SpeakingEvent(0.0, "A", "start"),
        SpeakingEvent(2.5, "B", "start"),   # B joins mid-A
        SpeakingEvent(3.0, "A", "stop"),
        SpeakingEvent(3.5, "B", "stop"),
    ]
    finalizations = [
        Finalization(chunk_start=0.0, chunk_end=3.0, finalize_t=3.6, text="overlapping chunk", truth="A"),
    ]
    return Scenario(
        name="Overlap (A dominant)",
        description="A talks 0-3, B cuts in 2.5-3.5. Whisper chunk covers A's full interval.",
        events=events,
        finalizations=finalizations,
    )


def scenario_silent_then_speaker() -> Scenario:
    """Edge case: Whisper finalizes a chunk that began during silence,
    well after the previous speaker stopped, and finalizes BEFORE the
    next speaker starts. Should fall back to last-known speaker.
    """
    events = [
        SpeakingEvent(0.0, "A", "start"),
        SpeakingEvent(1.0, "A", "stop"),
    ]
    finalizations = [
        Finalization(chunk_start=0.0, chunk_end=1.0, finalize_t=1.5, text="A says hi", truth="A"),
        # A long silence finalization (shouldn't really happen but test fallback)
        Finalization(chunk_start=2.0, chunk_end=2.5, finalize_t=3.0, text="murky", truth="A"),
    ]
    return Scenario(
        name="Silent then speaker (fallback exercise)",
        description="Finalize during silence — both strategies should fall back to A.",
        events=events,
        finalizations=finalizations,
    )


SCENARIOS: list[Scenario] = [
    scenario_kyle_michael_flip(),
    scenario_solo_speaker(),
    scenario_three_way(),
    scenario_overlap(),
    scenario_silent_then_speaker(),
]


def run() -> None:
    grand_current_correct = 0
    grand_timeline_correct = 0
    grand_total = 0
    for scn in SCENARIOS:
        cur = current_strategy(scn)
        tl = timeline_strategy(scn)
        print(f"\n=== {scn.name} ===")
        print(f"    {scn.description}")
        print(f"    {'chunk':>12}  {'truth':<8}  {'current':<8}  {'timeline':<8}  text")
        cur_correct = tl_correct = 0
        for f, c, t in zip(scn.finalizations, cur, tl):
            cmark = "✓" if c == f.truth else "✗"
            tmark = "✓" if t == f.truth else "✗"
            cur_correct += int(c == f.truth)
            tl_correct += int(t == f.truth)
            window = f"{f.chunk_start:>4.1f}-{f.chunk_end:<4.1f}"
            print(f"    {window:>12}  {f.truth:<8}  {c:<7}{cmark} {t:<7}{tmark} {f.text!r}")
        n = len(scn.finalizations)
        grand_current_correct += cur_correct
        grand_timeline_correct += tl_correct
        grand_total += n
        print(f"    score:  current {cur_correct}/{n}    timeline {tl_correct}/{n}")
    print(f"\n=== TOTAL: current {grand_current_correct}/{grand_total}, "
          f"timeline {grand_timeline_correct}/{grand_total} ===")


if __name__ == "__main__":
    run()
