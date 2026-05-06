# Session 197 handoff (2026-05-05) — Phase 14.20.2 + 14.20.3 SHIPPED

Skipped 14.19.9-11 (small polish, not launch-blocking) and went straight to the meaty 14.20.x audio work since the spike from S193 was still fresh. **14.20.2** lands the Swift dual-stream audio helper at `src/_1_800_operator/swift/operator-audio-capture.swift` (288 LOC) — single binary captures system audio via ScreenCaptureKit + mic via AVAudioEngine, writes framed `[1B tag 'S'|'M'][4B BE u32 length][N bytes Float32 16kHz mono PCM]` chunks to stdout, with per-stream 10s no-callback watchdog and TCC preflight for both perms. Single-binary chosen over two-procs (one cdhash → cleaner TCC UX). **14.20.3** adds a `--probe` flag to the same binary (read-only TCC report, prints JSON, never prompts; necessary because TCC.db is SIP-protected) and two new `operator doctor` checks for Screen Recording + Microphone, gated on darwin-only, with fix copy pointing at exact System Settings panes. Smoke-tests passed: clean compile, mic stream flowed (105 frames over 12s with real RMS signal during `say`), framing decoded zero-error, watchdog fired at exactly 10s for the system stream — the empirical replay of the silent-failure mode DECISION.md predicted, fixed by Developer-ID code-signing at release time.

## What landed (origin/main + 1 unpushed S196 commit + 1 new S197 commit pending)

- `a30537a` 14.19.8 — chat-surfaced permission flow (carried from S196, not yet pushed)
- **(this commit)** 14.20.2 + 14.20.3 — Swift dual-stream helper + doctor TCC checks

Files in this commit:
- `src/_1_800_operator/swift/operator-audio-capture.swift` (new, 288 LOC)
- `src/_1_800_operator/pipeline/doctor.py` (+112 / -1)
- `.gitignore` (+3, ignore the compiled binary)

Untracked spike utility kept for posterity: `debug/14_20_audio_spike/decode_frames.py` (50-line Python frame decoder used in smoke testing).

## Exact next step (session 198)

**Phase 14.20.4 — wire the helper into AttachAdapter + MeetingRecord.** Real session-sized work (~3h):

1. Add `mlx-whisper`, `numpy`, `soundfile` back to `pyproject.toml` (all dropped post-14.19.7 since voice-preserved-only).
2. Port a simplified `AudioProcessor` from `voice-preserved:pipeline/audio.py` — keep VAD constants (`UTTERANCE_SILENCE_RMS=0.02`, `UTTERANCE_SILENCE_THRESHOLD=2`, `UTTERANCE_MAX_DURATION=10`), keep the 0.5s silence pre-pad in `transcribe()` (whisper drops the first word without it), keep mlx-whisper-base. Drop the TTS echo-guard (`is_speaking`) — slip is chat-only.
3. Two `AudioProcessor` instances in `AttachAdapter` — one fed by `[S]` frames (speaker="other"), one by `[M]` frames (speaker="user"). Each runs its own utterance loop on its own thread.
4. Spawn helper as `subprocess.Popen([helper_path], stdin=PIPE, stdout=PIPE, stderr=...)` in `AttachAdapter.join()` after CDP connection. Reader thread parses framed stdout and dispatches PCM to the right processor. Keep stdin held open for meeting lifetime; closing triggers helper's clean shutdown.
5. When a processor finalizes an utterance, call `MeetingRecord.append_caption(speaker, text, timestamp)` — same JSONL shape `captions_js.py` produces, so `mcp_servers/transcript_server.py` reads slip captions identically to dial.
6. Smoke-test mic-only end-to-end (system-stream confirmation belongs in 14.20.5 post code-signing).

`install.sh` build wiring rolls into 14.20.4 since both touch helper-path resolution. After `uv tool install`, install.sh resolves the package's swift/ via `python -c "import _1_800_operator; print(_1_800_operator.__file__)"`, runs `swiftc <pkg>/swift/operator-audio-capture.swift -O -o ~/.operator/bin/operator-audio-capture`, marks executable. Mac-only branch; Linux skips.

Then **14.20.5** (live-test) — gated on Developer-ID code-signing → release time per `project_apple_dev_account_deferred.md`.

## Open questions / blockers

- **14.19.9 / 14.19.10 / 14.19.11 skipped this lineage.** Not launch-blocking. Carry forward as post-launch polish: 14.19.9 (install.sh sendoff/welcome refresh — surface `--yolo` + `~/.claude/settings.json` overlay), 14.19.10 (fresh-Mac ladder live-test), 14.19.11 (docs refresh + ~700 LOC `mcp_client.py` deletion per S195 handoff).
- **Apple Developer account** for production code-signing — explicitly deferred to release-time per memory. 14.20.4 doesn't need it (mic-side validation works under ad-hoc signature; system-side waits for 14.20.5).
- **Commit `a30537a` (14.19.8) still local on `main`, not yet pushed** — carried from S196.

## Don't forget

- The compiled binary `src/_1_800_operator/swift/operator-audio-capture` is now gitignored. Source ships in the wheel via hatchling default (per `pyproject.toml:60`). install.sh compiles per-machine.
- The 10s no-callback watchdog is load-bearing diagnostic. Don't remove it in 14.20.4 even if the system stream starts working — it's the only signal that distinguishes "TCC silent-failure" from "system is just quiet."
- The `excludesCurrentProcessAudio = true` flag on the SCStream config means the helper won't echo its own stderr / log noise. Don't flip it.
- The mic side uses `AVAudioConverter` to downsample from hardware format (48kHz 1ch on this Mac) to 16kHz Float32 mono target. The converter callback supplies the input buffer once via the `supplied` flag — don't loop or you'll re-feed the same buffer.
- Doctor's `_check_microphone` deliberately defers to `_check_screen_recording` when the upstream cause (helper missing) is shared. This avoids printing two near-identical "rebuild the binary" fix lines. If you split the helper into separate binaries later (don't — see TCC reasoning), reconsider this.
- TCC has a permission-restart-pending mode where a granted permission isn't yet live to a running parent app. If smoke tests show "permissions granted in Settings, but binary still gets the silent-failure callback drought," quit + relaunch the parent terminal app. This is the user-NOTE.md gotcha from S193.
