# 14.36 — TCC warmup timing: why the user waits ~60s per dialog

**Question.** On a fresh install (M4 Air, v0.1.42), after clicking Allow on
the Microphone dialog the user waited ~60s for the System Audio dialog to
appear, then ~60s more after granting it. We expect the warmup to move on the
instant the user clicks. Why doesn't it?

## Method

`spike.py` resets ONLY the helper's two grants (`tccutil reset Microphone /
AudioCapture com.1-800-operator.audio-capture`), relaunches the **installed**
helper, and timestamps the helper's own stderr breadcrumbs relative to launch.
Two launch mechanisms compared: `_disclaimed_spawn` (lets us capture stderr)
and the production `open -W -n -a` (no stderr; measured by wall-time + a
disclaimed `--probe`).

## Results

### Run via `_disclaimed_spawn` (stderr captured) — events.log

```
AFTER reset: {"system_audio":"not_determined","microphone":"not_determined"}  ← cold path reproduced
[t=  0.02] Microphone permission not determined — requesting
[t=  1.73] Microphone access granted=true          ← completion fired AT click (fast)
[t=  1.84] tap created                              ← system dialog surfaces here
[t=  1.84] waiting for Audio Capture grant (up to 60s)
[t= 62.28] Audio Capture grant resolved: not_determined   ← hit the FULL 60s deadline
[t= 64.01] [S] system-audio tap capturing
```
Fresh disclaimed `--probe` immediately after: `{"system_audio":"ok","microphone":"ok"}`
— i.e. **the user DID grant system audio, but the running helper's poll never
saw it and ran the full 60s.**

## Root causes (two, independent)

1. **System-audio leg — `TCCAccessPreflight` is per-process stale.**
   `tccAudioCaptureStatus()` (the poll's early-bail signal) caches its result
   for the helper's whole lifetime. Once it returns `not_determined` at startup
   it keeps returning `not_determined` even after the grant is written — so the
   60s poll (`operator-audio-capture.swift:381`) never early-bails and always
   runs to the deadline. A FRESH process reads the true `ok` instantly. **This
   alone is a guaranteed 60s on every install, every launch mechanism.**
   Confirmed beyond doubt above.

2. **Mic leg (production `open` only) — completion handler starves on the
   blocked main thread.** Under `open`/LaunchServices the app fully activates
   NSApplication and `AVCaptureDevice.requestAccess`'s completion is delivered
   on the **main queue** — which the warmup blocks via `sema.wait` on the main
   thread (`:225`) before `RunLoop.main.run()` ever executes. So the semaphore
   isn't signalled until the 60s timeout; recovery is the post-timeout fresh
   `authorizationStatus` read (`:226`). Under `_disclaimed_spawn` the completion
   lands on a background queue → fires at click (t=1.73s above), which is why
   the spike did NOT reproduce the mic delay. **Because the system tap (and thus
   the system dialog) is created only AFTER the mic leg resolves, a blocked mic
   leg also delays the system dialog by 60s** — exactly the user's "waited 60s
   for the system prompt to pop up."

Net for the user (production `open`): mic leg 60s (delays the 2nd dialog) +
system leg 60s (stale preflight) = the ~120s they sat through. install.sh still
printed "✓ granted" because `PROBE_AFTER` is a fresh, non-stale process.

History: S247 (commit 05552dd) bumped both warmup timeouts 3s/10s → 60s/60s to
stop the helper exiting before the user could click. That was correct, but it
relies on an "early-bail when granted" signal that — for both legs — never fires
in-process, so users now eat the full 60s twice.

## Side findings

- **Dialogs DO surface under `_disclaimed_spawn`** (the open question left by
  14_31_tcc_warmup_spike/RESULTS.md): mic + system both prompted and granted.
- Under `open -W`, the helper does **not** reliably exit when capture succeeds
  (no stdin EOF; the 10s watchdog only fires on *zero* mic callbacks), so
  `open -W` can hang. The user's install returned only because their capture
  exited. `open -W` is a poor lifecycle fit for the warmup.

## Implication for the fix

Detection must be **cross-process** (fresh `--probe`, never stale) and the
helper must **not block the main thread** so both dialogs appear up front. The
no-rebuild path: launch the warmup via `_disclaimed_spawn` (mic completion fires
promptly → both dialogs surface), and have the PARENT poll a fresh `--probe`
every 0.5s and move on / kill the helper the instant both resolve — instead of
`open -W` waiting on the helper's stale 60s in-process poll. This reverses the
14_31 "use open -W for warmup" decision, on new evidence.
