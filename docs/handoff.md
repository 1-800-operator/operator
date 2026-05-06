# Session 193 handoff (2026-05-05) — three small launch-blockers shipped, audio spike landed, mass deletion at 3/8

Started by pushing S192's 21 backlogged commits (bridge architecture cutover + slip pivot to dedicated-Chrome-window) and tagging `slip-cdp-v1-foundation` after a green live-test of slip in a real meeting. Then knocked through Phase 14.19.4 (`operator login claude` — single-purpose Playwright Google sign-in lifted from the wizard's step-2), 14.19.5 (`operator doctor` — five-check world-readiness gate built from existing primitives), and 14.19.6 (slip reply-prefix locked to `[🤖 Claude] ` after a real-Meet eyeball test). All three live-validated. Spawned a background subagent for Phase 14.20.1 (audio capture spike) which delivered three artifacts at `debug/14_20_audio_spike/`: a compileable Swift script, an STT comparison locking mlx-whisper-base, and a decision doc — and empirically confirmed two risks for 14.20.2 (ScreenCaptureKit's silent-failure mode + Developer-ID code-signing as a day-one requirement, not polish). Phase 14.20 promoted from Post-MVP to launch-blocker, slotted between 14.19 and Phase 16. Then started 14.19.7 (mass deletion of wizard-era code) and chunked it into 8 surgical sub-steps — A, B, C all shipped this session (~4,200 net lines deleted across three commits), each smoke-tested green between commits.

## What landed (all pushed to `origin/main`)

- `ac93e89` Phase 14.19.4 — `operator login claude`
- `7bd9b08` roadmap — Phase 14.20 (Swift+ScreenCaptureKit+Whisper) promoted to launch-blocker
- `3c1dc52` Phase 14.19.5 — `operator doctor`
- `82694ff` Phase 14.19.6 — slip reply prefix locked to `[🤖 Claude] `
- `0b7ad5a` 14.19.7-A — strip wizard CLI surface from `__main__` (-238)
- `ae04a8e` 14.19.7-B — delete the wizard files (-2908: setup.py, build_card.py, picker.py, face.py, terminal.py, custom_template.yaml, `_sync_claude_imports`)
- `e7ccadf` 14.19.7-C — delete bundled `agents/{claude,codex}/` and `skills/` directories (-1090)

Plus the foundation tag (`slip-cdp-v1-foundation`) and S192's 21 commits pushed at session start.

## Exact next step (session 194)

**Resume Phase 14.19.7 at step D+E** — one atomic config.py-rewrite-plus-callers chunk. D (OPERATOR_BOT env routing) and E (config.py schema parsing) are deeply intertwined: config.py's module load is gated on `OPERATOR_BOT`, so stripping the env var without the YAML loader leaves config.py crashing at import. Approach:

1. Catalog every `config.*` reference in surviving files (`pipeline/chat_runner.py`, `pipeline/llm.py`, `pipeline/providers/*`, `pipeline/mcp_client.py`, `connectors/*`, `pipeline/meeting_record.py`, `__main__.py`).
2. Triage each reference: hardcode (constants like `MAX_TOKENS`, `TOOL_TIMEOUT_SECONDS`), pull from `bridges/claude.py` (per-bridge values like trigger phrase, reply prefix), or remove (wizard-era — `AGENT_NAME`, `MCP_SERVERS`, `SKILLS_*`, `SYSTEM_PROMPT`, `INTRO_ON_JOIN`, `FIRST_CONTACT_HINT`, `AGENT_TAGLINE`, `PERMISSIONS_AUTO_APPROVE`, `PROGRESS_NARRATION_*`).
3. Write the new minimal `config.py` (~50 LOC: paths + meeting-mechanic constants + runtime tunables; no YAML, no OPERATOR_BOT, no `load_dotenv`).
4. Update each caller; commit per-file or as one cohesive commit (your call — single commit is cleaner if test green, per-file is safer if something snags).
5. Drop `OPERATOR_BOT` from `__main__.py` (lines ~226, 534, 571, 745, 1001, 1029, 1114), `readiness.py` (line 43, 225 — comments only), `oauth_cache.py` (line 5 — comment only), `google_signin.py` (lines 36, 58 — comments + the inlined "avoid importing operator.config" workaround can collapse).
6. Smoke: `python -c "import _1_800_operator.__main__"`, `operator doctor`, `python -m _1_800_operator slip --help`, `python -m _1_800_operator login claude` (don't actually re-auth).
7. Then step F (chat_runner cleanup — drops `_narration_auto_approve`, `permission_chat_handler`, `codex_elicitation_handler`, `_tail_claude_stream`, then deletes `mcp_servers/claude_code.py` + `pipeline/auth.py`), step G (test triage — expect ~30 test files affected, mostly delete-the-file), step H (final smoke).

## Open questions / blockers

- **Apple Developer account for 14.20.2** — Developer-ID code-signing is now a day-one requirement (Risk #2 from the audio spike was empirically confirmed). Does Anthropic / operator org have an Apple Developer account ($99/year + DUNS prep)? If not, getting one is on the critical path before 14.20.2 ships its first release. Without it, every operator update will TCC re-prompt every user, and dev-loop iteration on the helper requires re-granting permissions on every `swiftc` recompile.
- **Doctor's TCC checks** — `operator doctor` doesn't yet check macOS Screen & System Audio Recording or Microphone grants. Phase 14.20.3 is the natural place to extend it via in-process `CGPreflightScreenCaptureAccess()` + `AVCaptureDevice.authorizationStatus(for: .audio)` probes (simpler than the SIP-protected `TCC.db` query path which requires Full Disk Access).
- **Spike's runtime audio test still failing** — terminal-from-Cursor and terminal-from-Terminal.app both produce 0 bytes from spike_capture. Not blocking 14.20.2 (the architecture is independently validated by `voice-preserved`'s shipped ScreenCaptureKit code + Granola's same approach), but live runtime confirmation needs the Developer-ID-signed helper before it'll work cleanly. Treat current spike result as "API path validated, runtime grant attribution deferred to 14.20.2."

## Don't forget

- All session-193 commits are on `origin/main`; nothing on `public/main` yet (the public-snapshot dance is per-release, not per-commit).
- `debug/14_20_audio_spike/` is untracked. Spike artifacts can be committed for archival or kept untracked — they're historical reference for 14.20.2, not load-bearing.
- The "macOS will lie about granted permissions" framing of Risk #1 was overstated. Actual observed behavior: preflight returns true when the responsible-process attribution chain has *some* ancestor with a grant, but frame delivery enforces against the immediate responsible process. Real edge cases are narrower (Sequoia weekly re-prompt, TCC cache staleness, helper-bundle drift). Watchdog is still cheap insurance, just don't oversell it as the primary failure mode.
- 14.19.7 step D+E rewrite of config.py is the largest single piece of remaining surgery in 14.19.7. Plan ~1.5h for it. Step F (chat_runner) is the second-largest — also touches a long-running threading-heavy file. G + H are smaller. Total remaining 14.19.7 work: ~2-2.5h.
