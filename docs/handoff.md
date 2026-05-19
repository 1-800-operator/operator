# Session 248 handoff (2026-05-19)

## What got done

**Phase 14.32 — Core Audio Tap API migration shipped end-to-end.**

The dial-mode audio helper was rewritten to capture system audio via
`AudioHardwareCreateProcessTap` + a private aggregate device + IOProc
(macOS 14.4+, gated behind `kTCCServiceAudioCapture` = "System Audio
Recording Only") and the user's mic via AVCaptureSession (gated behind
`kTCCServiceMicrophone`). The pre-migration SCStream + captureMicrophone
single-stream design is gone, along with the ~50 lines of Quit-and-Reopen
recovery install.sh accrued across v0.1.30-34 to soften SCStream's Screen
Recording UX.

Phase breakdown:

- **Phase 0** — Spiked AVCaptureSession for the BT HFP exclusivity risk
  that drove the original S209 SCStream-for-mic decision. Baseline 94 cb/sec
  100% non-zero on built-in mic. **Validated end-to-end with AirPods +
  Chrome holding the mic in a Meet lobby**:
    1. **Raw frames** — 570 callbacks in 12s, 100% non-zero, RMS 0.030
       when user talked.
    2. **STT round-trip** — captured 13.12s of mic frames through the
       production faster-whisper-large-v3-turbo path (same model, beam=5,
       0.5s silence pad). Two harvard-style sentences read aloud
       transcribed to "and the birch canoe slid on the smooth planks,
       glue the sheet to the dark blue background." — every content
       word landed correctly; only a stray "and the" hallucination and a
       comma-vs-period delta. AirPods → SCO link → AVCaptureSession →
       16kHz Float32 mono → whisper produces intelligible text.
  Bonus finding from the BT run: the tap stream and mic both switched
  to 24kHz (AirPods' AAC/HFP bidirectional rate), and the helper's lazy
  converter init from observed source format handled the device-rate
  flip transparently — same 16kHz Float32 mono target downstream
  regardless of source rate. A 10s silent-buffer watchdog ships in the
  helper as defense in depth; if a future user setup ever trips it the
  failure surfaces loudly in `/tmp/operator.log` rather than silently
  corrupting their meeting.

- **Phase 1** — Re-verified the Tap API on this machine. The original
  `/tmp/tapspike` v1 binary fired callbacks but reported `bytes=0` due to
  a buggy `AudioBufferList` iteration (treated `mBuffers` as a true array
  rather than a Swift tuple). Wrote `tapspike2` using the correct
  `UnsafeMutableAudioBufferListPointer` pattern + queried tap stream format
  explicitly — got 94 cb/sec, 192 KB/s = 48kHz × Float32, rms 0.099 across
  77% non-zero callbacks. Tap delivers MONO directly (vs. SCStream's
  stereo+downmix); one resample step gone from the pipeline.

- **Phase 2** — Wrote the production helper (`src/_1_800_operator/swift/operator-audio-capture.swift`).
  Architecture mirrors the pre-migration one: lazy converter init from
  first observed source format, module-scope strong references, lock-
  protected `writeFrame`, restart queue for serialized session rebuild,
  default-input device listener for mic device tracking, per-stream stats
  with `callbacks` + `nonZeroCallbacks` + `bytes`. New surface: a
  `TCCAccessPreflight` private-API probe (same precedent as
  `responsibility_spawnattrs_setdisclaim` in `_disclaimed_spawn.py`) so
  `--probe` mode can answer kTCCServiceAudioCapture state without
  prompting. Wire format unchanged: `[1B tag 'S'|'M'][4B BE u32 length][N
  bytes Float32 16kHz mono PCM]`. End-to-end smoke validated against
  `say`: [S] 736 cb / 332 nz / 502 KB, [M] 569 cb / 569 nz / 388 KB, frame
  counts match helper-internal stats exactly.

- **Phase 3** — Info.plist: `NSScreenCaptureUsageDescription` →
  `NSAudioCaptureUsageDescription`; `LSMinimumSystemVersion` 13.0 → 14.4.
  `helper.entitlements` unchanged (audio-input still needed for
  AVCaptureSession mic; Tap API uses TCC grant only, no entitlement).

- **Phase 4** — Python TCC checks: `doctor._check_screen_recording` →
  `_check_system_audio` (reads new `system_audio` probe key); fix string
  points at "System Audio Recording Only" pane. `__main__`:
  `_SETTINGS_DEEP_LINK_SCREEN_CAPTURE` → `_AUDIO_CAPTURE`
  (`Privacy_AudioCapture` URL fragment); `_parse_probe_status` reads new
  key; warmup messaging + TIMING fields renamed `sr_*` → `sa_*`.
  `attach_adapter.py` + `_disclaimed_spawn.py` doc comments brought into
  line. All 22 tests pass.

- **Phase 5** — `install.sh`: removed the ~50-line Quit-and-Reopen recovery
  block (kTCCServiceAudioCapture has no such quirk per the spike), updated
  probe grep keys + user-facing wording for the new Settings pane. File
  shrunk from 599 → 543 lines.

- **Phase 6** — Full test pass. All 22 unit tests pass exit-code-0. Live
  capture smoke against the production-installed helper (Developer
  ID-signed, notarized, stapled): [S] 546 cb / 435 nz / 372 KB, [M] 527 cb
  / 527 nz / 359 KB. Both streams produce real, non-zero, well-formed
  PCM at the expected rate.

- **Phase 7** — Ran `scripts/build_signed_helper.sh` end-to-end:
  notarization Accepted in ~90s (submission id
  `343af2b9-ca51-4d6a-976f-8a4dc09a1831`); stapler + validator both
  worked. Copied notarized `Operator.app` from `~/.operator/bin/` →
  `src/_1_800_operator/swift/Operator.app` (the wheel-bundled location).
  Updated `CLAUDE.md` to describe the new dual-pipeline architecture.

- **Phase 7.1 (follow-up fix)** — User reported a spurious "Operator
  wants to access files on your Desktop" dialog at helper startup. Root
  cause: the helper inherits its CWD from the operator parent process
  (which runs from `~/Desktop/operator/` during development), and
  AppKit / AVFoundation / Core Audio frameworks internally touch the
  current directory during init — tripping macOS Files-and-Folders TCC
  even though the helper never reads or writes Desktop files itself.
  Pre-existing issue (the v0.1.34 SCStream helper had no chdir either),
  surfaced more visibly post-migration because AVCaptureSession + Core
  Audio HAL initialize more file-system machinery than SCStream did.
  Fix: `FileManager.default.changeCurrentDirectoryPath("/")` at the very
  top of the helper, before any framework call. Re-notarized
  (submission id `4c4e8316-c62a-4384-84f3-513da099a100`, Accepted).
  Validated by re-running the smoke from `~/Desktop/operator/` — both
  streams flow cleanly, no Desktop prompt.

- **Phase 7.2 (follow-up fix)** — Symmetrized the install/first-run
  warmup with the mic-permission wait. The mic dialog has always been
  handled correctly via `AVCaptureDevice.requestAccess(.audio)` +
  `sema.wait(timeout: 60)` blocking on user click. But the System Audio
  Recording dialog had no equivalent: there's no public
  `requestAccess` API for `kTCCServiceAudioCapture` — the dialog fires
  implicitly when `AudioHardwareCreateProcessTap` is called, and that
  call returns immediately. Under the install.sh / __main__ warmup
  (`open -W -n -a` with stdin=/dev/null), the helper would then hit the
  stdin-EOF lifecycle handler within ~100ms and exit BEFORE the user
  could click — leaving the post-warmup `PROBE_AFTER` reporting
  `not_determined` and printing a spurious "permissions not granted"
  warning. Fix: poll `TCCAccessPreflight` for up to 60 s after the tap
  is created, but only when the initial probe returned `not_determined`
  (fast-path skipped on already-granted). Re-notarized again.
  Validated: fast-path test shows no "waiting for grant" line in stderr
  when status was already `ok`; probe still returns `{"system_audio":"ok","microphone":"ok"}`.

## Exact next step

**Ship as a release.** Migration is complete in the working tree; nothing
has been committed yet. Recommended flow:

1. Bump version. Currently pinned to `v0.1.34` in `install.sh`
   (`OPERATOR_INSTALL_REF`) and presumably `pyproject.toml`. Suggest
   `v0.1.35` (minor; user-visible change).
2. `git add` the helper source, Info.plist, install.sh, doctor.py,
   __main__.py, attach_adapter.py, _disclaimed_spawn.py, CLAUDE.md,
   `src/_1_800_operator/swift/Operator.app/**`, handoff.md.
3. Commit + tag + push (operator-main); bump operator-plugin in lockstep
   if any user-facing slash-command behavior changed (it didn't — same
   surface).
4. Pull `claude plugin marketplace update` locally to refresh the
   cached marketplace pin.
5. On a fresh laptop or after `tccutil reset SystemAudioRecording
   com.1-800-operator.audio-capture`: run the installer one-liner and
   verify the install.sh warmup surfaces the new System Audio Recording
   Only dialog correctly + the helper resumes capture afterward.

## Open items / blockers

- **AEC3 pre-shift may need re-tuning.** The aec3 binary has a hardcoded
  150ms mic-delay queue compensating for SCStream's 63ms anti-causal
  mic-leads-ref skew. The Tap API has different timing characteristics
  and the existing tuning may be slightly off (or unnecessary). The wire
  format is unchanged so aec3 still functions; the cancellation quality
  may have shifted. Re-evaluate post-migration:
    1. Run a live Meet with system audio playing through built-in speakers
       (the only scenario where AEC matters; headphone users are unaffected).
    2. Compare transcripts of the user's mic before/after — look for
       residual remote-audio bleed.
    3. If degraded, measure new skew with a debug spike and adjust
       `MIC_DELAY_MS` in `src/_1_800_operator/rust/aec3/src/main.rs`.

- **TCC probe accuracy.** `TCCAccessPreflight` returned `not_determined`
  for the Developer ID-signed bundle on first call even though the tap
  itself succeeded after the dialog was granted. May be a TCC cache
  freshness issue. Doctor / install.sh + warmup logic compensate (they
  always do the warmup if status != "ok"). Worth a follow-up to see
  whether re-probing after the runtime tap call surfaces a fresh "ok",
  in which case the warmup could be made more idempotent.

- **AVCaptureDeviceTypeExternal deprecation warning** in stderr at every
  startup. Cosmetic — AVFoundation's default audio-device enumeration
  internally references `.external`. Fix is to add
  `NSCameraUseContinuityCameraDeviceType` to Info.plist (irrelevant
  since we don't capture video) or to explicitly enumerate audio devices
  via `AVCaptureDevice.DiscoverySession` instead of
  `AVCaptureDevice.default(for: .audio)`. Defer.

- **Old `OPERATOR_INSTALL_REF=v0.1.34` pin in install.sh.** Bumped at
  release time, not now.

- **AEC3 universal-binary CI run `26080752009` from S247** — still
  unverified. After the Tap migration, AEC3 criticality may be lower
  (less speaker bleed via the new path), so this is less urgent.

## Working-tree state

Modified (not yet committed):
- `src/_1_800_operator/swift/operator-audio-capture.swift` — full rewrite
- `src/_1_800_operator/swift/Info.plist`
- `src/_1_800_operator/swift/Operator.app/**` — newly signed + notarized
- `src/_1_800_operator/pipeline/doctor.py`
- `src/_1_800_operator/pipeline/_disclaimed_spawn.py`
- `src/_1_800_operator/__main__.py`
- `src/_1_800_operator/connectors/attach_adapter.py`
- `install.sh`
- `CLAUDE.md`
- `docs/handoff.md` (this file)

New under `debug/`:
- `debug/14_32_avcapture_mic_spike/` — Phase 0 spike artifacts (kept for
  reference; can be deleted at next cleanup pass).

Pre-existing untracked / modified state from prior sessions (not S248):
`debug/14_22_pty_spike/bench/state/replies.jsonl`, the various draft docs
in repo root (`mvp.md`, `mvp-copy.md`, `color.md`, etc.), and the
`debug/14_*` spike directories.

Spike binaries at `/tmp/tapspike/` and `/tmp/operator-audio-capture-test/`
remain. The original `/tmp/Operator.app.backup-pre-14.32` safety backup
of the v0.1.34 helper is also still around — can be removed once the new
helper is confirmed working in a live Meet.
