# Session 245 handoff (2026-05-18)

## What got done

Three shipped changes on operator-main + a plugin v0.1.24 copy
rewrite. Doctor got a new `_check_meeting_record_mcp` check that
verifies both registration (`claude mcp list` parse) and allowlist
(`mcp__operator-meeting-record__*` in settings.json) — closes the
S243 PM carry. New `_installer_fix()` helper replaced 8 "re-run
install.sh" fix strings across MCP / audio-helper / faster-whisper
/ aec3 checks with consistent dual-target wording ("ask Claude to
fix this, or re-run the installer: curl -LsSf
https://1-800-operator.com/install | bash"). Inner-claude /
outer-claude jargon scrubbed from all user-facing doctor strings.
The biggest user-facing fix was a 47-word `_BRIEFING` addition
that killed a confabulation bug: when the user closed a Meet tab
(instead of `/operator:hangup`), Claude Code sessions were sitting
in "Needs input" state with claude falsely claiming "another bot
joined" / "3 bot instances running simultaneously (PIDs …)". Root
cause: PTY claude resumes the shared session with no closure
marker in scrollback, confabulates duplicates from partial signal.
Initial design instinct was Option B (on-shutdown injection of a
[SYSTEM] meeting-ended turn); user pushed for the cheaper
prompt-level fix first. Live-validated. Plugin v0.1.24 also
shipped: scrubbed "guarded mode" everywhere, reframed slip-yolo
as the goal mode ("the chat panel becomes a full Claude session"),
added Hears/Context bullets across all three slip modes,
emphasized local privacy + drove users to `/operator:recap` in
wiretap. Two commits on operator-main pushed to both remotes;
plugin tagged + pushed; marketplace cache pulled; desktop app
updated 0.1.22 → 0.1.24 + user restarted.

## Exact next step

No required next step. Optional pickups in priority order:
**Live-validate worker respawn** (S244 carry — kill the worker pid
mid-meeting via `kill <pid>` while someone is speaking; expect
"whisper_worker (pid=X) died mid-meeting — respawning" + captions
resume on the new worker).

## Open items / blockers

- **Live-validate worker respawn** (S244 carry).
- **Live-validate audio.py drain fix in production** (S244 carry).
- **Worker-spawn-failure path not tested** (S244 carry).
- **Option B fallback (S245)** — if the briefing tab-close fix
  ever degrades, the next-tier fix is on-shutdown injection of a
  `[SYSTEM] Meeting <slug> ended` turn into the shared session
  before SIGTERM. Not building until briefing-only proves
  insufficient.
- **Validate post-change Chrome eviction with an actual evict**
  (S243 carry).
- **TCC warmup on a fresh user account** (S237 carry).
- **A3 promotion candidates + duplication cleanup** (S241 carry).
- **Long-meeting CPU/heat for faster-whisper** (S233 carry).
- **H-23 AEC** (multi-session scope).
- **QA items the user is updating async** in `docs/qa-monday.md`.

## Working-tree state

Pre-existing modifications left untouched (not from this session):
`debug/14_22_pty_spike/bench/state/replies.jsonl`,
`docs/handoff.md` (will be overwritten by this skill — that's
expected), plus a pile of untracked debug artifacts + docs from
prior days. None are S245's to commit.
