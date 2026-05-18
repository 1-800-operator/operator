# Launch Audit Findings — RECOVERED (Audit 2 + Audit 4)

> These two audit sections were written into `docs/launch-audit-findings.md`
> earlier in session S240 and then overwritten when the Audit 1 re-triage
> rewrote the whole file. Reconstructed verbatim from session transcript;
> merge back into `launch-audit-findings.md` when convenient.
>
> Audit 4 used the original `docs/launch-audit-plan.md` format (titles +
> short prose). Audit 2 was written in the same critical/high schema as
> the Audit 1 section.

---

# Audit 4 · Hook conversion opportunities

**Run:** 2026-05-17 (parallel agents across components 1–4; components
5–8 N/A per the audit plan).
**Bar:** "Materially better" — only swaps that replace a polling loop,
replace transcript-tailing for events hooks emit cleanly, or give
structured `tool_use_id` correlation we're currently inferring.

**TL;DR:** All four cells clean. Operator's hook surface is already
saturated where it makes sense. Provider rides `SessionStart` + `Stop`
+ `PreToolUse` already; the remaining transcript-tail is for per-block
streaming (no hook event exists for that), and `PostToolUse` can't
suppress denied-tool narration because it doesn't fire on `PreToolUse`
denies.

---

## Audit 4 · Component 1 (CLI entry & lifecycle)

Clean — no conversion opportunities. This layer is OS-level process
lifecycle (singleton lockfile, daemonize fork, TCC warmup, signal-driven
shutdown, orphan reap) and CLI subcommand dispatch; it has no polling
loops over inner-claude state or transcript-tailing — the only
inner-claude interaction is constructing the provider/runner and calling
`runner.stop()` in the signal handler, neither of which a Claude Code
hook (`Stop` / `SessionEnd` / `PostToolUse` / etc.) could observe or
replace.

## Audit 4 · Component 2 (Slip Chrome connector)

Clean — no conversion opportunities. The connector layer talks only to
Chrome (CDP, Meet DOM, chat-panel observer) and the Swift audio helper;
it never observes inner-claude tool calls, transcripts, or session
state, so Claude Code hooks have no surface to attach to here.

## Audit 4 · Component 3 (Chat runner & trigger logic)

Clean — no conversion opportunities. ChatRunner does no transcript/log
tailing of its own (provider owns that, Component 4), the existing
PreToolUse hook already drives the permission round-trip with allow/deny
purge resolving via the answer-file write, and trigger gating /
continuation window / sender filtering are pure operator concerns with
no hook surface that would map.

## Audit 4 · Component 4 (LLM provider & PTY)

Clean — no materially better conversion opportunities. The provider
already rides four hook channels (`SessionStart` → `ready.flag`, `Stop`
→ `replies.jsonl`, `PreToolUse` → `permreq_requests.jsonl`, and reads
structured Stop payloads for `transcript_path` / `session_id` /
`last_assistant_message`). The remaining file-tailing surface is the
Claude Code transcript JSONL, which is read for **real-time
assistant-text streaming mid-turn** — there's no hook event that fires
per-assistant-text-block, so this can't be hook-replaced.
Process-lifecycle uses kernel signals (`proc.poll()`, PTY EOF on the
drain thread) which are stricter than a `SessionEnd` hook would be. The
deny-aware `tool_use_id` correlation in `_assistant_texts_split` could
in theory consume structured `PostToolUse` events, but `PostToolUse`
doesn't fire on `PreToolUse` denies (the actual case we need to
suppress), so the swap would not produce the signal the parser needs.

---

# Audit 2 · Edge cases

**Run:** 2026-05-17 (single pass across components 1–6, parallel agents; 7–8 N/A per the audit plan matrix).
**Severity bar:** critical = launch-blocker. high = real user-visible
reliability, data-integrity, or trust-promise bug an early user would
hit and write up. Lower findings dropped (the agents surfaced ~60
moderate / nit items — not recorded).

**TL;DR:** **0 critical**, **13 high**. Three clusters:

1. **Lifecycle is leaky around shutdown** (H-15, H-25, H-26) — the
   singleton lock is released too early, the orphan-reap SIGKILL fires
   too fast, and `/operator:hangup` returns "hung up" before the
   daemon is actually gone. Each one independently breaks the
   "one-slip-at-a-time" promise or the "hangup means hung up" promise.
2. **Provider has no recovery path and a memory leak** (H-18, H-19,
   H-20) — a single transient PTY error latches `_unavailable` for the
   meeting's life; `_pty_dump` grows forever; the deny-filter for
   pre-tool narration can lose the race if events split across polls.
3. **The multi-meeting memory pitch is undermined by data-model bugs**
   (H-21, H-22) — a stale marker after a crash makes MCP silently
   serve the *wrong* meeting; rejoining the same Meet code (recurring
   standups, the canonical use case) merges every session into one
   blob.

Plus two audio-quality bugs (H-23 AEC headphones misalignment, H-24
helper-starvation utterance cutoff), one wrong-Chrome attach (H-16),
one UX footgun (H-17 send_chat clobbering the user's draft), and one
trigger-gating leak (H-27).

> Note: H-numbers continued from Audit 1's H-14. Re-number to fit your
> final ordering as you merge.

---

## HIGH

### H-15 · Shared-resource handoff during in-flight teardown isn't safe

**Where:** `src/_1_800_operator/__main__.py:1099-1144` (`_shutdown`), plus the audio-helper spawn site and the JSONL open path on slip startup.

**What:** `_shutdown` releases `slip.pid` early **by design** (per the
comment at `__main__.py:1106-1116`) — so `/operator:status` doesn't lie
and `/operator:slip` doesn't refuse during the 5–12s teardown window.
That UX is correct and worth preserving. **But** a second
`/operator:slip` acquiring the lock during teardown then races the
still-tearing-down first on the specific shared resources that aren't
serialized by the lock:

- **Audio helper bundle** — single-instance per macOS bundle ID; new
  slip's spawn collides with the old slip's still-running helper.
- **Meeting JSONL** — different slug per meeting today, so safe by
  accident. But after H-22's day-scoped slug ships, rejoining the same
  Meet code same-day means the old `MeetingRecord.close()`
  (`meeting_end` line + attendee bake) can race the new instance's
  open + append.
- **`.current_meeting` marker** — old `_shutdown` unlinks it after
  new slip writes it.

**Why high:** Each one is silent corruption rather than a loud error.
User retries hangup → slip back-to-back, gets a partially-truncated
prior meeting JSONL, a half-attributed audio helper, or a missing
`.current_meeting` marker — none surfaced.

**Fix sketch:** Keep the early lock release (don't revert the UX). Add
a `~/.operator/.teardown_in_progress` sentinel that `_shutdown`
creates immediately after releasing the lock and removes after
`connector.leave()` returns. New slip's startup polls for that
sentinel after acquiring the lock and waits up to ~5s for it to clear
before touching the audio helper / opening the JSONL. Narrows the
fix to the actual unsafe handoff; preserves "hangup feels fast,
re-slip works immediately."

---

### H-16 · CDP reuse path attaches to *any* Chrome on port 9222, not just the slip profile

**Where:** `src/_1_800_operator/connectors/attach_adapter.py:571-602` (`_browser_session`)

**What:** S239's three-way branch only evicts in the zero-context arm.
If `_cdp_endpoint_alive()` is True AND `_cdp_page_count() > 0`,
operator takes the reuse path and `connect_over_cdp`'s to whatever
Chrome is on 9222 — even if it's the user's own debug-Chrome running
for a different tool. The meeting URL then opens as a new tab in the
*wrong* profile: different Google identity, no slip cookies, possibly
attributed to the user's main account.

**Why high:** The entire point of the dedicated slip profile (separate
user-data-dir, dodging Chrome 121+ CDP restrictions, isolating meeting
identity from the user's primary Google session) is silently bypassed.
Anyone who runs a separate `--remote-debugging-port=9222` workflow
(Puppeteer dev, browser automation tests, another LLM browser tool)
hits this on first slip attempt.

**Fix sketch:** Before taking the reuse path, verify the user-data-dir
of the attached Chrome matches `~/.operator/slip_profile/` (via
`Browser.getVersion` + process introspection, or by writing a slip
marker into the profile and checking it's reachable). Mismatch →
treat as "not slip Chrome," fall through to evict + relaunch.

**Status — LARGELY RESOLVED by security C-1.** The per-launch random
Origin nonce stored in `~/.operator/slip_profile/.cdp_origin` is now
required on the `connect_over_cdp` Origin header. A foreign Chrome on
9222 launched with default Origin lockdown (Chrome 121+ default)
rejects the WebSocket upgrade because its allowed-origin list doesn't
contain operator's nonce — operator sees a clean "Origin not allowed"
failure instead of silently attaching to the wrong Chrome. **Residual:**
a foreign Chrome explicitly launched with `--remote-allow-origins=*`
would still accept the connection. If you want to fully close the
residual, add the user-data-dir verification originally proposed in the
fix sketch.

---

### H-17 · `send_chat` silently overwrites the user's in-progress chat draft

**Where:** `src/_1_800_operator/connectors/attach_adapter.py:857`

**What:** Outbound chat uses `input_box.fill(full_message)`, which
clears the textarea before typing. In slip mode the human user and the
bot share the same Meet chat input. If the user is mid-typing when
claude's reply lands (likely — the user often types a follow-up while
claude is still answering), their draft is destroyed with no warning,
no save, no restore.

**Why high:** This is a daily-driver UX bug for slip mode's primary
use case. Lost text, no recovery, no surfaced cause — the user will
attribute their disappearing draft to "Meet being flaky" until they
figure out it's operator. Trust-eroding on first encounter.

**Fix sketch:** Read the textarea contents before `fill`; if non-empty
and not equal to what we last wrote, queue/defer the send (or use
`input_box.press_sequentially` to *append* the bot reply to a freshly
opened chat after the user's draft is committed). At minimum: log a
warning and stash the clobbered draft to `~/.operator/debug/` for
manual recovery.

---

### H-18 · `_unavailable` latch has no recovery path — every subsequent @claude gets the failure message

**Where:** `src/_1_800_operator/pipeline/providers/claude_cli.py:312-313`, `:1336-1361`

**What:** `_unavailable` is set after **two consecutive turn failures**
(the per-incident retry in `_run_turn` already burned its one retry
and the respawn-and-retry also raised). Causes: spawn failure (slow
MCP at boot, `ready.flag` timeout), PTY EOF mid-reply, transcript-tail
parse failure, missing `replies.jsonl` after a `Stop` timeout.

The original `@claude` mention that triggered the failure does get a
user-visible message — `ChatRunner._narrate_failure` posts the
`[🤖 Claude] …` failure line into chat. **The bug is that every
subsequent `@claude` after the latch flips also raises immediately
and gets the same failure message**, with no path for ChatRunner to
ask the provider to recover. User's only recovery is
`/operator:hangup` + rejoin — they have to figure that out by trial
and error from "claude keeps saying the same error."

**Why high:** Looks like the bot is dead-but-attached for the rest of
the meeting. Common pattern on launch day: user retries, gets the
identical canned failure, assumes operator is broken, drops the
meeting. The underlying provider state is often recoverable on attempt
#3 (transient MCP slowness, brief network hiccup).

**Fix sketch:** Clear `_unavailable` when a fresh `@claude` mention
arrives — that's the natural "new traffic" signal that says the user
wants to try again. Expose `provider.retry()` (or just have
`complete()` reset the latch on entry); ChatRunner calls it before
dispatching a new turn if the provider is currently latched. Next
turn re-attempts boot + the standard one-retry path. If it fails
again, the latch re-flips and the user gets a fresh failure message —
no infinite-retry storm, but recovery is always one mention away.

---

### H-19 · `_pty_dump` grows unbounded for the meeting's life

**Where:** `src/_1_800_operator/pipeline/providers/claude_cli.py:217-238`, `:554-560`

**What:** `_drain_pty_thread` appends every PTY chunk to
`self._pty_dump` forever; nothing trims it. `_pty_tail` only reads the
last 2000 bytes for display, so the growth is invisible until OOM.
Claude's TUI repaints on every keystroke and tool call — chunks
accumulate at MB/min during active turns.

**Why high:** A 2-hour meeting accumulates hundreds of MB of PTY dump
in RSS with no surfacing. Slow degradation → swap → eventual OOM
during the long meetings operator is explicitly designed for. Caught
only when the user reports "operator got slow / crashed after an hour."

**Fix sketch:** Replace `self._pty_dump` with a `collections.deque`
sized to ~256 KB (10× the `_pty_tail` window). Trim on append.

---

### H-20 · Denied tool calls can still be announced to the room

**Where:** `src/_1_800_operator/pipeline/providers/claude_cli.py:1091-1218`, `:1430-1445` (`_assistant_texts_split`, `_poll_transcript`)

**What — plain English:** In slip / slip-strict mode, the user denies a
tool call (say `rm foo`) from the meeting chat permreq. The deny is
supposed to do two things: (1) the tool doesn't run, and (2) claude's
voice-over about it ("I'm going to remove that file…") is suppressed
so the room never sees the announcement of a tool the user vetoed.

Operator processes claude's transcript in ~150ms batches. If claude's
"I'm about to run rm" sentence is in batch N and the denial
acknowledgment is in batch N+1, operator already posted the sentence
to chat before it saw the deny. Once the deny lands in the next batch,
the announcement is already out there and there's nothing to retract.

**Why high:** Trust in the deny path is the core promise of guarded
slip mode. User vetoes a tool from chat and still watches claude
announce it to everyone in the meeting — feels like the deny didn't
work even though the file is safe. Particularly bad on sensitive ops
(file deletion, message sending, code changes) where the *announcement*
itself can be the embarrassing part.

**Fix sketch:** Hold pending pre-tool announcements in a small buffer
keyed by `tool_use_id`; release to chat only after the next poll has
confirmed the matching `tool_result` isn't a deny. Or have ChatRunner
subscribe to the PreToolUse-deny stream and retract already-posted
announcements if the deny lands within ~500 ms (Meet chat doesn't
support edit/delete, so retraction means posting a follow-up
"(denied — ignore previous)" — less clean but works).

---

### H-21 · Stale `.current_meeting` marker after crash makes MCP serve the wrong meeting as "live"

**Where:** `src/_1_800_operator/mcp_servers/record_server.py:105-121` (`_resolve_record_path`)

**What:** The MCP server trusts the marker file unconditionally. If
operator crashes (SIGKILL, OOM, panic) without running `_shutdown`,
the marker file persists pointing at the prior meeting's JSONL. The
next `claude` session that calls `list_captions` / `search_captions`
silently returns content from the *previous* meeting and labels it
"the live meeting." `__main__._cmd_status` has stale-marker cleanup,
but only when `status` runs; live MCP queries have no freshness check.

**Why high:** User says "what did we just discuss?", claude responds
confidently with content from yesterday's standup. Trust-eroding
exactly the way "AI hallucinates" is — except it's not hallucination,
it's stale state served with a confident frame. Crashes happen.

**Fix sketch:** Cross-check the marker against `slip.pid`: if no live
operator owns the lock, treat marker as stale and return "no live
meeting." Or have the MCP `mtime`-check the marker file (>N minutes
without a participant snapshot update → stale).

**Disposition:** AGREED — ship the freshness check.

**Status — PARTIALLY RESOLVED by security H-6.** During a live
operator meeting, inner-claude inherits `OPERATOR_MEETING_RECORD_PATH`
which the MCP now prefers over the marker file — so the live-meeting
case is no longer race-prone or stale-prone, and the new
`_is_safe_record_path` validator rejects poisoned paths. **Residual:**
a *bare* claude session run by the user (no operator running, no env
var inherited) falls back to the marker file. If operator crashed
without `_shutdown`, that marker still points at the prior meeting and
the bare session will get yesterday's transcripts framed as the live
meeting. The freshness check originally proposed (cross-check against
`slip.pid`) is still needed to close this residual.

---

### H-22 · Slug collision: recurring meetings (same Meet code) merge into one JSONL

**Where:** `src/_1_800_operator/pipeline/meeting_record.py:31-46` (`slug_from_url`); rendering side at `mcp_servers/record_server.py:662-688`, `:760-784`

**What:** `slug_from_url` is deterministic per URL. Rejoining the same
Meet code (recurring standups, weekly 1:1s — the canonical use case
for the multi-meeting memory pitch) appends to the *same* JSONL as
the prior session. The `session_start` marker scopes `tail()`
per-session, but `_read_all_events`, `find_meetings`, and
`list_meetings` all conflate every session into one "meeting":
duration spans weeks, attendee list pools across sessions, mtime
shows only the latest.

**Why high:** The v1 product story is "operator gives claude memory
across meetings." Recurring meetings are the meetings users care most
about remembering. The find/list surface returns one merged blob
labeled with the latest mtime — the user asks "what did Jane say in
last week's standup?" and gets the union of every standup since
operator was installed, attributed to the most recent date. Breaks
the headline feature.

**Fix sketch:** Append the **calendar day** to the slug
(`<meet-code>_<YYYYMMDD>`), not a timestamp. Day-scoped means
drop-and-rejoin within the same day correctly reattaches to the same
JSONL (the `session_start` marker inside the file still distinguishes
individual joins), and recurring meetings (the canonical case) cleanly
separate by date. **Edge case to accept:** same Meet code used for two
distinct meetings on the same day (e.g., a personal-room URL used for
both a 9am standup and a 4pm retro) collides — rare with normal
meeting hygiene; if it ever becomes a real complaint, downgrade to
timestamp-scoped at that point.

---

### H-23 · AEC pre-shift hardcoded for built-in speakers — headphones (recommended config) get unaligned reference

**Where:** `src/_1_800_operator/pipeline/aec_cleaner.py:34-40` + the Rust `aec3_spike` binary; memory `project_aec_design_findings`

**What:** The 150ms pre-shift lives in the Rust binary, not switchable
from Python. The shift compensates for SCStream's output-buffer skew
when the user is on built-in speakers. When the user is on headphones
(the *recommended* config per the design memory), the system audio
doesn't leak into the mic — but AEC3 still runs, applying a misaligned
cancellation against a phantom reference. Misaligned AEC3 mangles
clean mic input with residual-subtraction artifacts.

**Why high:** Operator silently corrupts the M-leg whisper input on
the recommended setup. Captions degrade in subtle, hard-to-attribute
ways ("whisper is bad sometimes") and no diagnostic surfaces the
cause. Doctor doesn't check the speaker/headphones state.

**Fix sketch (revised):** The root cause is misaligned reference, not
"AEC running where it shouldn't" — current 150ms is a hardcoded guess
for built-in speakers, used on every device type. Two-step fix:

**A (substantive — likely fixes it alone):** Read actual device latency
from CoreAudio at meeting start via `kAudioDevicePropertyLatency` +
`kAudioStreamPropertyLatency` on the default output device. Pass that
value to the Rust `aec3_spike` binary as the pre-shift instead of the
hardcoded 150ms. AEC3's internal echo-path detector then has correctly
aligned reference data and converges cleanly regardless of device type
(built-in, BT, USB, Aux, display audio). Current bug is "we're sending
real reference data at the wrong time, which AEC3 mistakes for an echo
path and cancels phantom speech" — proper alignment makes the
misdetection go away.

**B (belt-and-suspenders if A isn't enough):** Cross-correlate first
~3 seconds of mic against the reference signal at meeting start. High
correlation → real echo path, send reference normally. Near-zero
correlation → no echo (headphones, separate-room speakers, etc.) →
send a *zeroed* reference into AEC3 from then on. Silence reference =
near-no-op AEC; clean mic passes through untouched. No device-type
detection, no user-facing flags, works for any output device whether
or not its audio loops back into the mic.

Both require the Rust binary to accept the pre-shift (and ideally
reference-zeroing toggle) as runtime parameters over its stdin
protocol — currently both are baked into the binary per the
`project_aec_design_findings` design memory.

---

### H-24 · `SILENCE_THRESHOLD` cuts utterances during helper starvation, not actual trailing silence

**Where:** `src/_1_800_operator/pipeline/audio.py:189-199`

**What:** `silence_count` is incremented both when `raw` is non-empty
silence AND when `raw` is empty (the helper's buffer is starved). On
transient helper backpressure (TCC mid-renegotiation, system pressure,
even a brief Whisper inference stall feeding back to read scheduling),
empty reads tick `silence_count` at 0.5s per tick. With
`SILENCE_THRESHOLD=2` the utterance finalizes after 1s of helper
starvation — *not* 1s of trailing speech silence. The trailing word
of a sentence gets sliced off and posted as a separate utterance
(often mid-word).

**Why high:** Captions get cut mid-sentence under any system load.
Particularly visible on long Zoom-style meetings where macOS may
throttle the helper process during CPU pressure. Reads as "whisper
randomly breaks long utterances."

**Fix sketch:** Distinguish "received silent frame" from "received no
frame." Only increment `silence_count` on the former. On starvation,
pause the silence countdown.

---

### H-25 · `_kill_orphaned_children` SIGTERM→SIGKILL gap is 0.5s — truncates in-flight claude transcript writes

**Where:** `src/_1_800_operator/__main__.py:106` (`_kill_orphaned_children`)

**What:** The wait between SIGTERM and SIGKILL is hardcoded to 0.5s.
But inner-claude's PTY shutdown legitimately takes >0.5s in the common
case (Node.js MCP teardown, transcript JSONL flush, hook script
cleanup). The reaper turns a slow-but-clean exit into a SIGKILL with
half-written transcript lines on disk.

**Why high:** Data integrity on shutdown — the meeting JSONL ends
with a malformed line that record_server's reader silently skips
(`json.JSONDecodeError` swallowed). The *last few seconds* of every
meeting (often the most important — wrap-up, decisions, action items)
are silently lost. Combined with H-21 (stale marker), the user has
no signal anything went wrong.

**Fix sketch:** Wait on `MeetingRecord.close()` to complete before
reaping. Durable signal — the reaper fires only after the JSONL is
known-flushed, rather than picking a fixed timeout that's either too
short (truncates) or too long (slows shutdown).

---

### H-26 · `/operator:hangup` returns "hung up" 7+ seconds before the daemon is actually gone

**Where:** `src/_1_800_operator/__main__.py:618` (`_run_hangup`)

**What:** Hangup polls up to 3s for the daemon to exit, then prints
"hung up (1 session)." But the slip daemon's `_shutdown` waits up to
10–12s on `connector.leave()`. So the user-facing success message
fires while the daemon is still draining — and a follow-up
`/operator:slip` within those 7s hits the singleton guard with "another
slip session is running" (despite hangup just having claimed success).

**Why high:** Direct contradiction between two user-facing commands —
hangup says done, slip says it's not. Same launch-day path that
`/operator:hangup; /operator:slip <next-meeting>` traverses for every
back-to-back meeting. The error message is misleading and the
workaround (`wait 10s and retry`) is not documented.

**Fix sketch:** Change hangup's poll signal from "daemon pid exited"
to "slip lock released." The daemon's `_shutdown` already releases
the lock early (~500ms after SIGTERM — intentional design per H-15),
so polling on lock-released returns truthfully in <1s in the common
case. Background teardown continues as it does today; the next
`/operator:slip` finds the lock free and proceeds (with H-15's
`.teardown_in_progress` sentinel wait covering the shared-resource
handoff). Two findings, one architecturally consistent fix.

---

### H-27 · Permreq question's trailing `?` opens an indefinite continuation window that leaks tail-chatter into claude

**Where:** `src/_1_800_operator/pipeline/chat_runner.py:1136`, `:1189-1218` (`_check_permreq_chat_for_answer`); flag set in `_send` based on `?`-detection

**What:** The permreq question is posted via `_send(kind="chat")`,
which sets `_last_reply_had_question = True` based on the `?` in the
text. The question always ends in "— OK?" — so this always trips and
opens an indefinite continuation window. After the permreq resolves,
`_check_permreq_chat_for_answer` consumes the answer reply directly
**without routing through `_process_messages`**, so the flag is
cleared only when the *next* non-self message arrives. That next
message is then forwarded to claude as a continuation prompt even if
it was casual side chat ("ok cool", "lol", "anyway as I was saying…").

**Why high:** Violates the "speak when spoken to" promise that the
trigger-gating design explicitly markets. The user trusts that
non-`@claude` chat stays off-record; this path leaks the immediate
post-permreq message into claude's turn. Particularly bad in
multi-party meetings where the message after a permreq is often
unrelated.

**Fix sketch:** One line — have `_check_permreq_chat_for_answer`
clear `_last_reply_had_question = False` when it consumes the answer
reply. (Alternatively skip the `?`-detection for `kind="permreq_question"`
posts, but the one-liner in the answer-consume path is more localized
and matches the cause.)

---

## What was checked and cleared

Findings deliberately *not* recorded for Audit 2 (verified safe,
intentionally designed, or below the high bar):

- ChatRunner's send-queue tick callback runs on the polling thread (per
  provider contract); off-thread `_send_lock` mutations are serialized.
- `MeetingRecord.append` uses an in-process `threading.Lock` — chat poll
  + caption finalizer + roster snapshot serialize correctly within a
  single process (cross-process MCP read race exists but is moderate;
  POSIX append atomicity covers it for typical caption sizes).
- `_attribute_s_leg` max-overlap logic (S235) correctly handles the
  most common overlap shapes; tie-on-equal-overlap is non-deterministic
  but practically benign.
- `leave()` is idempotent and safe to call before `join()` returns
  (browser thread guards the state machine correctly).
- `_chat_queue` orphaned waiters time out at 10s — annoying under flap,
  not corrupt.
- Helper-side framing (length-prefixed PCM) is robust to partial reads
  via `_read_exact`.
- Bracketed-paste in PTY is covered by Audit 1's classifier-injection
  / PTY-escape findings; not re-recorded here.
- Hooks (component 7) and install/packaging (component 8) are N/A for
  this audit per the plan matrix.

---

## Suggested fix ordering for Audit 2

1. **Lifecycle promise PR** (H-15 + H-25 + H-26) — three small fixes
   that together restore the "hangup means hung up, then slip works"
   contract and stop truncating transcript tails.
2. **Provider reliability PR** (H-18 + H-19) — `_unavailable` retry
   path + `_pty_dump` bounded deque. Same file.
3. **Meeting-record durability PR** (H-21 + H-22) — stale-marker
   freshness check + session-scoped slugs. Touches `record_server.py`
   + `meeting_record.py`; bigger change because of the slug-format
   migration.
4. **Audio quality PR** (H-23 + H-24) — headphones AEC bypass + fix
   the silence-vs-starvation counter.
5. **Standalone:** H-16 (CDP wrong-Chrome attach), H-17 (send_chat
   clobbers draft), H-20 (deny-filter race), H-27 (permreq
   continuation leak).

H-17 (draft-clobber) is the most user-visible bug per encounter and
worth landing first if you want a single high-leverage fix.
