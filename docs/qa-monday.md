# QA checklist — Monday meeting (2026-05-18)

Covers user-facing changes from S234–S237 that haven't been validated in
a real meeting yet. Group by what you'd naturally exercise during the
flow of a meeting.

## 1. Install / TCC permissions (S237)

*Only relevant if you reinstall on a fresh machine before Monday —
otherwise skip.*

- `./install.sh` triggers Mic + Screen Recording prompts attributed
to **operator-audio-capture.app** (not to Terminal/IDE). Click
Allow on both.
- Re-run `./install.sh`: prints "Audio permissions already granted"
and doesn't re-prompt.
- (If perms drift mid-life) running `/operator:dial` re-warms TCC  
via the preflight before "operator: joining…" — synchronous,  
~10s on first run.

## 3. Trigger + sticky conversation window (S234)

- First `@claude <question>` from you → bot answers.
- **Follow-up from you within 90s** without `@claude` → bot still
answers (sticky window).
- **A different participant** speaks without `@claude` during your
window → bot ignores (window is sender-scoped).
- After ~90s of silence, follow-up without `@claude` → bot ignores
again.
- Rapid-fire corrections from you (two messages in <2s) → bot only  
responds to the latest (debounce).

## 6. Recap / list_meeting_record (S236)

*Best tested at end of meeting or after.*

- In meeting: `@claude give me a recap so far` on a long meeting  
(45+ min). Claude should produce a coherent recap **without**  
saying "only the tail was captured" or "I only have partial  
transcripts."

## 7. Audio drain on shutdown (S244)

*The whisper_worker subprocess drains residual audio after main
exits, but there's also an in-process `AudioProcessor.drain` flush
that has only been unit-tested. Validate the trailing utterance
lands in the JSONL across the three shutdown paths.*

- **Leave button** mid-sentence → wait for the worker to seal →
`jq '.kind == "caption"' ~/.operator/history/<slug>_<date>.jsonl`
shows the trailing utterance.
- **Tab close** mid-sentence → same expectation.
- **`/operator:hangup`** mid-sentence → same expectation.

