# Session 233 handoff (2026-05-15)

## What got done

Three converging threads in one session. **(1)** Spiked and shipped the
**UI-tool wedge briefing** — `_BRIEFING` in `claude_cli.py` now steers
inner-claude away from `AskUserQuestion` and plan-mode tools (both render
TUI prompts no one can click in a meeting, model blocks indefinitely
waiting for tool_result). 2×2 spike + fresh-Claude-session control + live
meeting all green. Artifacts at `debug/14_27_ask_user_question_spike/`.
**(2)** Live validation tripped a new MLX/Metal completion-handler abort
variant (S227 family, but operator main this time) — diagnosed,
documented as HWK, and **eliminated entirely** by swapping the STT
backend from `mlx-whisper` to `faster-whisper-large-v3-turbo` (CTranslate2
on CPU). Identical WER, lower worst-case latency, ~5× slower p50 that's
invisible because transcripts are queried via the MCP not consumed live.
`aec_cleaner.py`'s `os.posix_spawn` workaround reverted to plain
`subprocess.Popen` (-132 LOC). Bench at `debug/14_28_cpu_whisper_spike/`.
**(3)** **operator-plugin 0.1.17** bundled the S232 yolo-off mode +
S230 doctor SKILL.md interpretation guide. 15/15 tests green, audio-helper
end-to-end test included.

## Exact next step

**Push everything** when ready — five local commits across two repos:

```
cd /Users/jojo/Desktop/operator        # 4 commits ahead of origin
git push                                # 7903ac1, d0a90c7, 00e7d4f, 958166e

cd /Users/jojo/Desktop/operator-plugin  # 2 commits ahead of origin
git push                                # 5f8bc52 (from S232), 00bf582 (S233)
```

After push: `git pull` the local plugin cache so the desktop app sees
0.1.17 (per `project_plugin_publish_two_steps` memory). Then proceed
with **Phase 5 live validation of `/operator:slip-guarded`** per
`debug/14_24_permreq_spike/PHASE_5_LIVE_TEST_CHECKLIST.md` — that
checklist was unblocked by the 0.1.17 bump.

## Open items / blockers

- **Push is gated on user — none of today's work is on origin yet.**
  All commits are local. Nothing has shipped externally.
- **`debug/model-log.md` still does not exist** (carried from S229+).
  S233 changes the audio-pipeline log strings:
  `"AudioProcessor: warming faster-whisper-large-v3-turbo"` and
  `"AudioProcessor: faster-whisper-large-v3-turbo ready"` replaced the
  prior `mlx-whisper-base` variants in two places. Document when
  reconstituted.
- **Real-meeting WER validation for the new STT pipeline.** Spike
  numbers say faster-whisper-large-v3-turbo on CPU int8 should hold
  (14.4% WER vs MLX's 13.0% on the same 12-utterance set, within
  noise) but production conditions differ. S231's deferred caption-
  quality validation is now superseded by this — the new pipeline
  wants its own real-meeting WER check before we trust the captions
  in anger.
- **Long-meeting CPU/heat behavior of faster-whisper** — flagged as
  open follow-up in `debug/14_28_cpu_whisper_spike/FINDINGS.md`.
  ~4 cores pegged in burst during transcribe (388% with int8);
  sustained behavior on a 1-hour meeting not benched. Not blocking
  but worth a sanity check before launch.
- **Orphan inner-claude after operator exit (tracked from S234 QA).**
  On 2026-05-15, after Chrome was closed manually mid-meeting, the
  operator daemon exited cleanly (per /tmp/operator.log) but the
  inner-claude subprocess from an earlier session (PID 60180) plus its
  child transcript MCP (PID 60192) were observed still running ~20
  minutes after their operator parent had died. The `start_new_session=True`
  spawn flag means the inner-claude survives parent SIGKILL by design,
  but the explicit `provider.stop()` call should have reached it. Worth
  investigating before launch — accumulating orphan `claude` processes
  (and their child MCP servers) is a long-meeting / repeated-session
  footgun. Not blocking today's work; flag as a tracked follow-up.
- **June 15 Anthropic classification** — macro unknown, still carried.
- **`README.md`, `SECURITY.md`, `docs/security.md`** continue to show
  as modified on `main` from prior sessions — user-owned, intentionally
  not committed.
