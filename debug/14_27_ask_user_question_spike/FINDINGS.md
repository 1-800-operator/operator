# 14.27 — AskUserQuestion-in-meeting spike: findings

## TL;DR

Inner-claude **calls AskUserQuestion natively** when the model decides it
needs to disambiguate. In an operator meeting:

- The tool's pick-list UI renders **only in the PTY** as ANSI text — operator
  drains those bytes into `_pty_dump` but doesn't parse them.
- The Claude Code **transcript JSONL writes nothing** about the call until
  it completes. While the question is in flight, the most recent transcript
  event is whatever came before. Operator's `_run_turn._poll_transcript`
  sees zero new events.
- Claude **blocks indefinitely** waiting for a tool_result. We left it
  parked for 3 full minutes with no transcript activity; the spinner
  ("Shimmying… / Skedaddling…") keeps spinning, never times out on its own.
- The Stop hook **never fires** — `replies.jsonl` would never get a new row.
  Operator's `_wait_for_next_reply` would hit its 600s per-turn timeout and
  raise. After one retry that also wedges, `_unavailable` latches and the
  room gets the `"claude is unavailable — run /operator:doctor"` message.

So today's failure mode is: meeting hangs for 10 minutes, then announces
"unavailable." Doctor would have nothing useful to say — the inner-claude
is healthy, just waiting on a UI prompt that has no driver.

## What the data shows

Three runs in `out/`:

| Run | Prompt | Observe | Transcript events | Assistant text |
|-----|--------|---------|-------------------|----------------|
| `run_…_230500` | `Use the AskUserQuestion tool…` | 90s | 7, frozen at t+0s | none |
| `run_…_230918_sanity` | `Reply with just the single word 'hello'.` | 60s | 10 | `'hello'` in <2s |
| `run_…_231109_aukq_180s` | same as first | 180s | 7, frozen at t+0s | none |
| `run_…_231502_tool_inventory` | `List every tool…` | 90s | 10 | `'Agent, AskUserQuestion, Bash, Edit, Read, ScheduleWakeup, ShareOnboardingGuide, Skill, ToolSearch, Write, CronCreate, …'` in ~25s |

The tool-inventory run confirms `AskUserQuestion` is in the always-loaded
core toolset — it's not enumerated in the `deferred_tools_delta` attachment
(151 names there, starting with `CronCreate`), but the model itself lists
it second.

The frozen-7-events shape for the AskUserQuestion runs is:

```
0: permission-mode
1: file-history-snapshot
2: user                ← our trigger
3: attachment (deferred_tools_delta, 151 names)
4: attachment (mcp_instructions_delta — Figma/Linear/github prose)
5: attachment (skill_listing)
6: ai-title            ← Claude Code auto-titles the session, then waits
```

No `assistant` event with the `tool_use` block lands. That's the key
constraint: **operator's structured channel (transcript) is blind to an
in-flight AskUserQuestion**. The unstructured channel (PTY bytes) does
show the UI, but it's interleaved with thousands of cursor-positioning
ANSI escapes from the spinner.

## What the PTY render looks like

Stripped of ANSI and squashed:

```
─────────────  ☐ X or Y  Which do you prefer, X or Y?
❯ 1. X   Choose option X
Enter to select · ↑/↓ to navigate · Esc to cancel
```

So the navigation is **arrow keys + Enter, Esc to cancel** — same affordances
the workspace-trust dialog uses. (The existing
`_PROMPT_AFFORDANCE_NEEDLES`/`_TRUST_DIALOG_NEEDLES` in `claude_cli.py`
match exactly this kind of UI for the *boot-time* stuck-prompt heuristic.)

## Mitigation options (decide separately)

Picking is a follow-up — this spike just maps the terrain. Options, easiest
first:

**A. Brief the model not to reach for it.** Add a sentence to `_BRIEFING`:

> You have no UI in this meeting. Do not call AskUserQuestion — answer
> your best guess inline, or ask the question as a normal chat message
> and wait for a reply.

Cheap, no code. Risk: prompt-level guidance is soft. Empirically the
naked-spawn invariant says we steer via tool descriptions, not prompts —
but the briefing is already an exception to that, and adding one more
line costs nothing.

**B. Detect-and-Esc.** PTY-tail mid-turn for the AskUserQuestion UI
signature (`"Enter to select"` + `"↑/↓ to navigate"` or similar). On
match, write `\x1b` (Esc) to the master fd to cancel the question; the
model gets a "user cancelled" tool result and continues, presumably with
a best-guess answer. Chat-narrate something brief ("I had a question but
no way to ask it here — going with my best guess").

Pros: structural signal, generalises to future Anthropic-shipped UI
prompts that use the same TUI primitive. Pros for operator: we already
have `_PROMPT_AFFORDANCE_NEEDLES` infrastructure for boot-time stuck
prompts; this is the same pattern, applied mid-turn.

Cons: PTY-string-matching is fragile — TUI redraws will eventually
change wording.

**C. Detect-and-bridge.** Same detect as B, then parse the question
text + options out of the PTY render, post to chat, wait for a reply,
translate the reply to arrow-key + Enter presses, send via PTY.

Pros: best UX — the room gets the actual question. Cons: significant
new surface area (regex-parsing TUI screens, mapping chat replies to
keystrokes, race conditions if the user pastes a reply while the UI is
mid-redraw). Probably too much for v1.

**D. Disable the tool.** If Claude Code supports a setting like
`"disabledTools": ["AskUserQuestion"]` in `~/.claude/settings.json`,
we could disable it for the meeting cwd via a project-level config
operator writes. Worth a quick grep of Claude Code docs.

## Recommendation

**Ship option A.** The briefing-nudge holds in both spike scenarios — no
backstop needed for the typical case.

## Control matrix: briefing × prompt-shape

Four runs across two prompt shapes (does the prompt name the tool by hand?)
× two conditions (briefing present or not). Each cell answers: does claude
call AskUserQuestion?

| Prompt shape | No briefing | Strict briefing |
|---|---|---|
| **Explicit naming** — `"Use the AskUserQuestion tool to ask me whether X or Y…"` | **WEDGED** (`run_…_231109_aukq_180s`) — 180s of no transcript events, PTY shows the pick-list UI | plain text refusal: `"I can't use AskUserQuestion here — this is a meeting chat…"` (`run_…_233403_briefing_vs_explicit`) |
| **Natural elicitation** — `"Ask me whether to use X or Y, then ask me whether to use A or B."` | **WEDGED** (`run_…_233802_control_natural_nobriefing`) — same wedge shape, PTY shows `☐ X or Y / Which should I use: X or Y? / 1. X / Enter to select` | plain text questions: `"Got it — two quick questions: 1) X or Y? 2) A or B?"` (`run_…_233132_briefing_natural`) |

The diagonal pair is the proof:

- **Top-left** establishes the failure case (explicit trigger, no briefing → wedge).
- **Bottom-left** is the control the user flagged: confirms the model
  *naturally* reaches for `AskUserQuestion` when asked to "ask me whether
  X or Y" — without the tool being named. So the natural pull is real.
- **Bottom-right** then shows the briefing prevents that natural call.
- **Top-right** shows briefing also overrides an explicit user instruction.

Briefing wins every cell on the right.

## Briefing-nudge: validated against natural and explicit triggers

The briefing-prepended runs use this turn-0 text (476 chars):

> You are in a live Google Meet chat. There is no UI here — participants
> type plain text into chat. You do NOT have access to any kind of
> multiple-choice or pick-list UI. Critical rule: do not call the
> AskUserQuestion tool under any circumstances. It will hang the meeting
> because participants cannot answer it. If you need to ask a question
> or disambiguate, just ask it as plain text in chat and wait for someone
> to type a reply. Acknowledge this and wait for the next message.

**Test 1 — natural elicitation** (`run_…_233132_briefing_natural`):

- Trigger (no tool named): `"Ask me whether to use X or Y, then ask me whether to use A or B."`
- Turn-0 ack: `"Understood — plain text only in chat, no AskUserQuestion. Standing by for the next message."`
- Turn-1 reply: `"Got it — two quick questions: 1) X or Y? 2) A or B? Reply with your picks (e.g. 'X, A') and I'll go from there."`
- **No tool call.** Plain text only. Stop hook fired normally both turns.

**Test 2 — explicit instruction by name** (`run_…_233403_briefing_vs_explicit`):

- Trigger: `"Use the AskUserQuestion tool to ask me whether X or Y, then ask whether A or B."` (the *exact* trigger that wedged claude for 180s without the briefing)
- Turn-0 ack: `"Acknowledged — plain text only, no AskUserQuestion."`
- Turn-1 reply: `"I can't use AskUserQuestion here — this is a meeting chat, so it would hang the room. Just type your question and I'll a[nswer]…"`
- **No tool call.** Model actively refused the user's explicit naming of the tool.

Two-for-two: the briefing-rule outranks both natural pull *and* direct
user instruction. That's strong enough to ship without the PTY-detect
backstop.

## Plan mode wedges the same way

Run `run_…_235153_plan_mode_no_briefing` — no briefing, prompt:
`"Use plan mode to come up with a quick 3-step plan for adding a hello
world script. Use the EnterPlanMode tool to start and ExitPlanMode when
you have the plan."`

Sequence:

1. `ToolSearch` loads `EnterPlanMode` / `ExitPlanMode` (they're deferred).
2. `EnterPlanMode` → returns "Entered plan mode" tool_result.
3. `Write` → plan persisted to `~/.claude/plans/<random-slug>.md`.
4. `ExitPlanMode` at t+15s.
5. **Wedge.** Transcript frozen at 21 events. PTY-only spinner for 110s.
   Tool_result for `ExitPlanMode` never arrives.

PTY render of the wedge UI:

```
Ready to code? Here is Claude's plan:
1. Create scripts/hello_world.py …
2. …
3. Verify …
Claude has written up a plan and is ready to execute. Would you like to proceed?
❯ 1. Yes, and bypass permissions
  2. Yes, manually approve edits
  3. No, refine with Ultraplan on Claude Code on the web
shift+tab to approve with this feedback · ctrl-g to edit in Vim
```

Same UI primitive as AskUserQuestion (numbered list, arrow-key nav,
Enter confirms), different affordance text. Side effect worth flagging:
**the plan file is written to `~/.claude/plans/` before the wedge** —
every wedged meeting would leave one of these behind.

## Final validated briefing line

Run `run_…_235524_expanded_briefing_plan_mode` validates one
expanded briefing covers both tools:

> Don't use any tool that pops up a UI for the user to click —
> specifically AskUserQuestion and plan mode (EnterPlanMode /
> ExitPlanMode). Both will hang the meeting because participants can't
> click anything here. If you need to ask something, type it as a normal
> chat message and wait for a reply. If you'd normally use plan mode,
> just write the plan inline as chat text instead.

Against the explicit plan-mode trigger, claude replied:

> `"I'll skip plan mode since it would pop a UI dialog that nobody in
> the meeting can click. Here's the plan inline instead:"` (followed by
> a 3-step inline plan).

No plan-mode entry, no plan-file litter, no wedge. Stop hook fired.

Insert this paragraph in `_BRIEFING` between the "narrate tool calls"
paragraph and the "don't reply to this message" paragraph.

## Esc-cancel as fallback (kept but unblocked)

## Esc cancellation: verified, with one caveat

Run `run_…_232124_esc_cancel` sent `\x1b` to the PTY 30s after the trigger.
Immediately after the keystroke, the transcript jumped from 7 → 11 events:

| # | type | what landed |
|---|------|-------------|
| 7 | assistant | empty thinking block, `stop_reason=tool_use` |
| 8 | assistant | `tool_use=AskUserQuestion`, full structured `questions` array, `stop_reason=tool_use` |
| 9 | user | `tool_result` = canonical rejection: `"The user doesn't want to proceed with this tool use. The tool use was rejected… STOP what you are doing and wait for the user to tell you how to proceed."` |
| 10 | user | text: `'[Request interrupted by user for tool use]'` (Claude Code's interrupt marker) |

After event 10, claude is **idle and waiting** — 80+s of PTY silence, no
retry, no further tool calls. The model receives the rejection + interrupt
marker and halts cleanly. Option B is mechanically viable.

**Caveat — Stop hook may not fire on interrupt.** The sanity probe
(`run_…_230918_sanity`) produced two extra events from the user's own
`~/.claude/settings.json` hooks: `stop_hook_summary` and `turn_duration`.
The Esc run produced **neither**. That suggests Claude Code treats an
Esc-during-tool-call as an interrupt, not a natural turn end, and Stop
doesn't fire — which means operator's `replies.jsonl` would also not get
a new row, and `_wait_for_next_reply` would still time out at 600s even
after the cancellation succeeded.

Mitigations for that, in order of effort:
1. **Backfill operator's turn boundary off transcript activity.** The
   `_run_turn` loop already tails the transcript; an `interrupt`-marker
   user event (`'[Request interrupted by user for tool use]'`) is a
   clean turn-end signal we can detect there.
2. Confirm via Claude Code docs whether the PostToolUse / SubagentStop
   hook fires on tool-interrupt; if so, the operator-plugin Stop hook
   payload could be augmented or replicated.

Either way, the spike confirms: **Esc → model halts → operator can detect
turn-end on the transcript interrupt marker, even if Stop doesn't fire**.

## Open follow-ups (not done here)

- **Does Claude Code respect a `disabledTools` setting?** Quick docs grep.
- **Does AskUserQuestion fire in plugin/skill contexts?** A skill might
  invoke it as part of an interactive flow — worth knowing if operator's
  skill ecosystem expects it.
- **How robust is the PTY UI-detection regex?** The current `_PROMPT_AFFORDANCE_NEEDLES`
  match `"entertoselect"` / `"esctocancel"`. The AskUserQuestion screen
  shows both — so the existing matcher would already classify it. But
  the screen also redraws across many PTY chunks; needs a small bench
  to confirm the needle is hit on every reasonable chunk window, not
  just an idealized single snapshot.
