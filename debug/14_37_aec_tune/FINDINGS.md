# 14.37 — AEC tune attempt → disproved AEC; it's not echo

**Question (S252):** the dial mic leg ([M]) shows phantom captions that look
like the system audio ([S]) bleeding in. Tune the AEC against the morning
debug corpus (`~/.operator/debug/raw_sqr-vyex-wob_20260520/{M,S}.f32`,
~18 min raw pre-AEC mic + system reference) to cancel the bleed.

## Method

`xcorr.py`: align M to S by the meta wall-clock start offset (M starts 364 ms
after S), then normalized cross-correlation of mic vs reference over ±300 ms,
in every 1.5 s window where the mic is active in the bleed range
(0.008 < M_rms < 0.06) AND the reference is active (S_rms > 0.03).

## Result — the mic is NOT a delayed copy of the reference

| metric | value |
|---|---|
| mic-active windows tested | 20 |
| max\|corr\| with S — median | **0.029** |
| max\|corr\| with S — p90 / max | 0.145 / 0.269 |
| windows with corr > 0.3 (echo-like) | **0 / 20** |
| best-window lag | scattered: +166, −243, −14, +26, +188 ms … (no consistent delay) |

Loudest-reference window (@1060 s): S_rms 0.127 but **M_rms 0.001** — mic
essentially silent while the reference is loud. The opposite of echo.

## Conclusion

**There is no cancelable speaker→mic echo in this corpus.** Speaker echo would
be a delayed, attenuated copy of the reference → high correlation at one
consistent small lag. We see ~zero correlation at random lags. That is why the
live AEC's `echo_return_loss_enhancement` sat at ~0 dB across every run — not a
misconfiguration; there was simply nothing to cancel.

The [M] content is the user's **own direct voice** (quiet — speaking toward a
phone, not the Mac) plus ambient. The Mac's built-in mic+speaker hardware AEC
almost certainly already strips the speaker echo; software AEC3 cannot remove
the user's direct voice (it isn't echo of the reference).

**Implication:** tuning `MIC_DELAY_MS` / AEC3 config would not fix the phantom
[M] captions — there is no echo to cancel.

## Follow-up: an amplitude floor is ALSO wrong (transcribe_validate.py)

Transcribing the morning [M] utterances disproved the second theory too. The
mic-leg content is **the local participant's (Jojo's) real speech**, not faint
echo/garbage:

```
0.028  "Hey Kyle, how's it going?"
0.021  "...did you want me to qa that"
0.020  "otherwise same old for me I'm gonna finish that Brown's wishlist stuff I promise Kyle"
0.049  "um not too much well did you get everything you need for the stockist and store locator stuff"
```

Real Jojo speech spans rms **0.004–0.051** — there is **no RMS threshold that
separates "real" from "drop"**. A floor (the briefly-added MIC_ECHO_FLOOR_RMS,
now REVERTED) would silently delete real meeting transcript. Both proposed
fixes — AEC tune and amplitude floor — are wrong for this data.

## Corrected root cause

The [M] leg legitimately captures the dial Mac's local mic. When a human is at
the dial Mac (the morning), [M] is their real speech — correct, just labeled
`speaker: None` pre-v0.1.43. The afternoon "phantom Operator" caption was the
SAME voice (Jojo) captured twice: directly by the dial-Mac mic ([M], quiet,
mis-transcribed "Julia", labeled with the dial tile's self-name "Operator")
AND via his phone's Meet round-trip ([S], clean, "Jojo Shapiro"). That dual
capture only happens when one person is two co-located participants (phone +
physically at the dial Mac) — a test setup, not normal use.

So there is **no echo and no faint-garbage subset to remove**. The narrow real
issues are (a) the mic-leg label is the dial tile's self-name, which mismatches
when the person at the Mac isn't that identity, and (b) genuine same-speaker
dual-capture, only in the co-located-two-devices case. Neither is fixed by AEC
or an amplitude floor.

## DEFINITIVE confirmation — controlled test (wos-ioww-qeg, S252)

User ran a clean A/B in debug mode: "jojo" spoke one sentence from a phone **in
another room** (no direct acoustic path to the Mac), then "deepak" spoke one
sentence **at the dial Mac**. Raw corpus
(`~/.operator/debug/raw_wos-ioww-qeg_20260520/`):

| event | [S] rms | [M] rms |
|---|---|---|
| jojo (remote, other room) t≈13–16s | **0.10–0.25** | **0.001** (silent) |
| deepak (at dial Mac) t≈37–40s | 0.000 | 0.02–0.09 |

While jojo's audio blasted out the dial-Mac speakers (S up to 0.245), the mic
captured **0.001** — i.e. **zero speaker→mic echo**. Captions: exactly two,
"Jojo Shapiro" (her sentence, [S]) and "Deepak Chopra" (his, [M]). No phantom.

**Conclusion: there is no echo bleed.** The product already behaves as desired —
[M] captions only the dial-Chrome participant; the remote arrives only on [S].
The original "Hey it's Julia" phantom was the user's **own direct voice** picked
up by the Mac mic while they sat next to it talking into a phone — an artifact
of being co-located with the dial Mac on a second device, not a product defect.
No AEC tune, no amplitude floor, no code change needed for this. Case closed.
