# Speaker attribution spike — findings

## Bug under investigation

In `~/.operator/history/dko-pgom-bfe.jsonl`, captions are correctly
transcribed but mis-attributed in back-and-forth stretches —
e.g. Kyle's and Michael's lines flip across consecutive utterances.
Initial hypothesis was that the DOM speaking class (`BlxGDf`) persists
on the previous speaker's tile until the next one starts, causing
ambiguous "who is speaking right now" reads.

## What we ruled out

A four-state DOM snapshot in slip Chrome's DevTools (script in
`debug/14_29_speaker_attribution_spike/console_snapshot.js` not
committed — lived briefly in conversation) showed that `BlxGDf` is
**removed the instant speech stops**, on both camera-off and
camera-on tiles. The persistent border the user observes visually
is almost certainly a CSS fade-out transition on the way down —
the class is already gone from the DOM.

So `chat_dom_js.py`'s `INSTALL_SPEAKING_OBSERVER_JS` is watching
the right signal. The class isn't the problem.

## What it actually is

`attach_adapter.py:1573-1581` attributes a Whisper segment to a
speaker by **reading the speaking set at the moment Whisper
finalizes**. But Whisper waits for silence before committing a
segment — typically 300-1000ms after the speaker stops. In a
real conversation, by the time finalize fires, the *next* speaker
has often already started, so:

- `_speaking_participants` now contains the new speaker
- `len(active) == 1` → effective_label = new speaker
- The previous speaker's words get stamped with the new speaker's name

`_last_s_speaker` doesn't save us either — it also tracks the most
recent *start*, which is also the new speaker by the time finalize
runs.

## Validation (`simulate.py`)

Five scenarios, ground truth known. Current strategy mirrors the
production logic; timeline strategy looks up who was speaking
during the chunk's actual time window `[chunk_start, chunk_end]`.

```
TOTAL: current 5/11, timeline 11/11
```

Per-scenario breakdown:

| scenario                       | current | timeline |
|--------------------------------|---------|----------|
| Kyle/Michael flip (real bug)   | 1/3     | 3/3      |
| Solo speaker                   | 1/1     | 1/1      |
| Three-way round-robin          | 1/4     | 4/4      |
| Overlap (A dominant)           | 0/1     | 1/1      |
| Silent then speaker (fallback) | 2/2     | 2/2      |

Timeline strategy fixes every failing case **without regressing
the cases the current logic already handles** (solo, fallback).

## Proposed production change

1. In `AttachAdapter._drain_speaking_queue`, in addition to
   updating `_speaking_participants`, append every event to a
   bounded `_speaking_history: deque[(t, name, kind)]`. Cap at
   something like the last 60-120 seconds of events
   (`maxlen=512` is plenty).
2. In `_audio_utterance_loop`, when Whisper hands back a finalized
   segment, thread through the **chunk's actual start time** —
   `time.time()` at the moment the first non-silent frame entered
   the chunk buffer. Today that timestamp is discarded; we only
   keep the finalize time.
3. Replace lines 1573-1581 with a function
   `_attribute(chunk_start, chunk_end) -> str` that scans the
   history deque for the speaker with maximum overlap, falling
   back to most-recent-`stop`-before-chunk-start, then to
   `speaker_label`.

The fix is mechanically straightforward — the only piece that
needs careful threading is the chunk-start timestamp through the
audio path. Worth eyeballing `pipeline/audio.py` to see how
`AudioProcessor` currently structures the chunk it hands back —
the spike doesn't touch that code.

## Open questions for prod implementation

- **History cap.** 512 events ≈ 8 minutes of dense conversation.
  Probably enough; revisit if we ever attribute against
  long-historical chunks (e.g. a delayed Whisper backlog after
  a CPU spike).
- **Overlap policy.** Spike picks the speaker with maximum
  interval overlap. Real conversations sometimes have genuine
  simultaneous speech where neither name is "right." Default to
  max-overlap; consider returning a joint label like
  `"Alice + Bob"` only if the overlap is ≥40% and roughly equal,
  but that's a polish item, not a correctness one.
- **Bleed dedupe ordering.** The S-leg bleed dedupe at
  `attach_adapter.py:1584` operates on `text` only — unaffected
  by attribution. Leave it alone.

## Not in scope here

- Halo (camera-off) animation detection. The DOM snapshot showed
  `getAnimations()` doesn't pick it up — likely SVG `<animate>`.
  We don't need it: `BlxGDf` already serves as the
  start/stop signal correctly.
- Sound-wave (`stripeJiggleAnimation` on `UBNDXc`/`HPxjXe`/`DwvCqe`)
  was caught by `getAnimations()`. Could be a secondary signal
  but doesn't help fix the timing race.
