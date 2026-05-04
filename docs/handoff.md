# Session 187 handoff (2026-05-04) — closed-source split + Phase 14.17 shipped + Phase 14.18 queued as launch-blocker

Two structural moves and one shipped phase this session.

## What landed

**1. Closed-source pivot split into `~/Desktop/operator-cloud/`** (commit `63d4934`). User reversed yesterday's "full pivot" decision — the OSS product ships first, closed-source becomes a fast-follow. This repo's `main` reverted to its S182 state via revert of the seven pivot commits (`beff05b..5d6cd06`). Pivot work continues at the cloud repo with full git history + the `experiments/cloud-browser-spike/` artifacts + four pivot-era memory files mirrored into `~/.claude/projects/-Users-jojo-Desktop-operator-cloud/memory/`. This repo's memory dir cleaned: deleted `project_closed_source_pivot.md`, `project_cloud_browser_auth_requirement.md`, `project_v1_track_a_only.md`; restored `project_oss_ethos.md` to pre-pivot wording; rebuilt `MEMORY.md`. `experiments/cloud-browser-spike/` removed; `experiments/captions/` kept (OSS-relevant).

**2. Phase 14.17 shipped** (commit `4a4035f`) — `claude_cli --resume` for crash recovery. Replaced the synthesized-opener crash-recovery path with claude's native `--resume` mechanism. When the per-meeting `claude -p` subprocess dies, a new subprocess spawns with `--resume <session-id>` and rehydrates the prior message history (incl. tool calls + tool results) from claude's on-disk session store. Closes the deferred S166 carry-over (synthesized-opener was lossy on tool fidelity). Spike confirmed `--resume` + stream-json + `--include-partial-messages` compose; cache survives the rehydration so recovery is fast as well as faithful. Implementation:
- Dropped `--no-session-persistence` from spawn (mutually exclusive with `--resume`).
- Added `_session_id` field captured idempotently in `_validate_init_event`.
- Spawn cmd appends `--resume <session_id>` when set.
- Deleted `_build_synthesized_opener` (~38 LOC) + `_turn_history` field + all append sites.
- Net ~70 LOC removed.
- New mock test pins `--resume <id>` in the cmd shape; renamed live test asserts `_session_id` survives the restart.
- All 8 claude_cli tests pass.

README "Privacy & logs" gained a "Resume your meeting in Claude Code" subsection — frames the on-disk session co-location as a feature (user can `claude --resume` from the same dir afterwards to keep the meeting going in their terminal) and surfaces the implications honestly.

**3. Phase 14.18 queued in `docs/roadmap.md` as launch-blocker** — transcript tool API beef-up + codex captions parity. See "Exact next step" below for the full sequencing.

## Exact next step (session 188): Phase 14.18 — start with Part A (tool beef-up), then Part B (codex parity)

User decision (S187): the existing `recall_transcript` tool is too thin for real long-meeting use. Its only knobs are `minutes_back` and `last_n`; no keyword filter, no time-window-around-X, no by-speaker, no length cap. We're going to beef it up FIRST, then extend to codex — so codex inherits the better tool from day one rather than us shipping the thin one twice. This entire phase is launch-blocking; user wants to ship Phase 14.18 today (session 188).

**Phase 14.18 step-by-step (full version in `docs/roadmap.md`):**

1. **A1 — scoping spike.** At session start: skim the GitHub MCP tool surface (`search_code`, `search_issues`, `get_file_contents`, `list_commits`, etc.) as loose inspiration. Same problem shape: model navigating a large indexed corpus of text. Don't copy directly — our use case is much narrower (meeting captions, ~minutes-of-conversation scale, not arbitrary repo). The takeaway: what's the curated set of 3–5 query verbs that covers 90% of real-meeting query shapes?
2. **A2 — finalize tool surface in conversation.** Lock the new shape together before code. Candidate verbs: keyword/semantic search, time-window-around-X (not just since-X), by-speaker filter, summary-of-window, list-speakers, get-N-around-line. Add a length cap with pagination/truncation hint so the model can never blow context unintentionally.
3. **A3 — implement** in `src/_1_800_operator/mcp_servers/transcript_server.py`. Keep `recall_transcript` for back-compat or rename if we add a clearer verb.
4. **A4 — test.** Fixture-based unit tests against a synthetic 60–90 minute meeting transcript (multiple speakers, varied density). User has agreed to a live test; we'll need a fixture for that too.
5. **A5 — live-test, claude bot, real Meet.** Run the new query battery against a real meeting. Verify the model picks the right tool for each question shape.
6. **B1 — marker file mechanism.** Write the meeting record path to `~/.operator/.current_meeting` from `_wire_meeting_record`; delete from `connector.leave()` + shutdown handler.
7. **B2 — modify `transcript_server.py`** to read meeting path from marker file on each tool call. Fall back to the existing `OPERATOR_MEETING_RECORD_PATH` env var so claude's path keeps working unchanged.
8. **B3 — register transcript MCP for codex** in `agents/codex/config.yaml`. Append static `-c` flags to `mcp_servers.codex.args`:
   ```yaml
   - mcp-server
   - -c
   - 'mcp_servers.transcript.command="python"'
   - -c
   - 'mcp_servers.transcript.args=["-m","_1_800_operator.mcp_servers.transcript_server"]'
   ```
   (Codex 0.128.0 supports `-c key=value` overrides on `mcp-server` per the S187 spike — the S179 "mcp-server doesn't accept --mcp-config" finding is now obsolete.)
9. **B4 — drop "captions disabled" disclaimer** in the codex YAML comments + flip `transcript.captions_enabled: true` for the codex agent.
10. **B5 — live-test end-to-end** with `operator dial codex <meet-url>` against a real Meet; run the same query battery from A5; confirm codex parity with claude.

**Done when:** both bots have caption parity, the new tool handles realistic long-meeting query shapes, live tests pass against a real Meet for both bots.

## Risks to flag at session 188 start

- TOML nested-key override (`mcp_servers.X.env.KEY=value`) verified for top-level `command`/`args`/`env` but not yet for nested env. ~30s verify before locking the YAML.
- Codex's transcript-MCP child needs to inherit operator's PYTHONPATH so `python -m _1_800_operator.mcp_servers.transcript_server` resolves under codex's spawn. Likely fine via env trickle-down, but confirm.
- Fixture test data — already surveyed at end of S187: `~/.operator/history/avd-axqi-obq.jsonl` is the strongest real-meeting fixture (**424 captions across 85 minutes**) — use it as the primary realistic fixture. Smaller real candidates: `fkr-echg-mfw.jsonl` (98 captions, 63 min) and `fsv-qbyd-amd.jsonl` (73 captions, 41 min). **All real history JSONLs are single-speaker (just "Jojo Shapiro")** — zero multi-speaker meetings on disk. So for testing the by-speaker filter specifically we need to synthesize a small multi-speaker fixture (~30 captions, 3 speakers, 15 min span). Plan: do that fixture-prep at the top of S188 before tool implementation.
- **Tool naming**: user has approved renaming `recall_transcript` if a clearer verb emerges during A2 — proceed without back-compat aliasing concerns, but coordinate the system-prompt directive at `claude_cli.py:~196` so the new verb name lands in the prompt nudge too.
- `recall_transcript` is the existing API claude has been using since S98 — back-compat matters if we rename or restructure. Either keep it as an alias or coordinate the system-prompt directive (`claude_cli.py` line ~196 backstop) so the new verb names land in the prompt nudge too.

## Open carry-overs (still applicable on OSS main)

1. **Repo flip + Cloudflare Pages walkthrough** — pending from S182. `gh repo edit 1-800-operator/operator --visibility public --accept-visibility-change-consequences`, then dashboard steps 2–3, then `curl | sh` second-Mac QA. Resume after Phase 14.18.
2. **Re-snapshot procedure** for any `install.sh`/`_redirects`/ship-list changes.
3. **install.sh end-to-end unverified** — covered by second-Mac QA.
4. **Older S181 carry-overs:** PyPI bump from v0.0.1 (post-launch), tier-2 audit + ~70 appendix nits (post-launch), S177 nits (post-launch), 5 test files needing in-file `OPERATOR_BOT` (post-launch).

## Where the closed-source work continues

`~/Desktop/operator-cloud/`. Open in a fresh Claude Code session to resume Phase A1 (web stack + DB pick + signup/trial-trigger scaffold). Pivot-era memory files survive there.
