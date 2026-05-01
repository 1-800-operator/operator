# Spike: subprocess-per-turn vs subprocess-per-meeting

Phase 14.12.2 prep, session 166. Both variants of the `claude -p` lifecycle
probed under subscription auth (`apiKeySource: "none"`).

## Setup

Same 3-turn arithmetic conversation in both probes so latency numbers are
apples-to-apples:

```
T1: "What is 2+2?"
T2: "Now multiply that by 3."
T3: "Now subtract 1."
```

No tools, no MCP, no hooks — pure LLM round-trip on the user's Claude Max
subscription. Probe scripts:

- `cli_probe_04_baseline_hello.py` — sanity floor
- `cli_probe_05_per_turn.py` — Spike A
- `cli_probe_06_per_meeting.py` — Spike B

Run with `env -u ANTHROPIC_API_KEY python3 …` so subscription auth is forced.

## Results

| Spike | Spawn | T1 | T2 | T3 | Total | Avg/turn |
| --- | --- | --- | --- | --- | --- | --- |
| **A: per-turn** (fresh subprocess each ask) | n/a (paid each turn) | 5.61s | 5.55s | 5.05s | 16.22s | 5.41s |
| **B: per-meeting** (one long-lived subprocess) | <0.01s | 11.03s | 1.43s | 1.17s | 13.63s | **4.54s avg, 1.3s on follow-ups** |

Both produced the correct final answer (11). All turns ran under
`apiKeySource: "none"`. The streaming-json input envelope that worked is the
obvious one — undocumented but stable empirically:

```json
{"type":"user","message":{"role":"user","content":"<user message>"}}
```

Each turn emits a single `result` event we can latch onto as the
"turn-complete, ready for next input" signal. Closing stdin terminates the
process cleanly.

## What the latency numbers mean for a real meeting

A typical Operator meeting averages 5–15 LLM turns once you count tool-call
chains and follow-up clarifications. Modeling both at 10 turns:

- **Per-turn (A):** ~54s of pure LLM latency, ~5s minimum response time on
  every ask.
- **Per-meeting (B):** ~11s init (one-time, hidden behind the join sequence
  if we warm the subprocess at meeting start), then ~1.3s/turn — ~12s
  cumulative on follow-ups.

Per-turn pays a ~5s cold-start tax on every single user message. That's
visibly worse than today's `anthropic.py` path. Per-meeting hides the cost
in the join sequence and produces sub-2s replies thereafter, which is
better than the current Anthropic streaming experience.

## Code complexity

**A (per-turn): ~30 LOC of subprocess plumbing.**
- Mirrors `anthropic.py` shape: build prompt → spawn → wait → parse → return.
- No threading, no readiness signal, no restart logic.
- Stateless: each call is independent, failures isolated.
- Parent owns history (read meeting JSONL, render transcript), same model
  as today.

**B (per-meeting): ~120 LOC of subprocess plumbing.**
- One reader thread to consume stdout NDJSON without deadlocking.
- Queue + `result`-event latch to know when the model is done with a turn.
- Lifecycle hooks: spawn at `LLMClient.start_meeting()`, teardown at
  `leave()`, restart on death (mid-meeting recovery — re-feed history).
- Inner-claude owns context across turns. We send only the latest user
  message; we don't control prompt cache.
- Need careful EOF/error handling; broken-pipe and stuck-state detection.

**Gut estimate:** B is a one-day add over A. Most of the complexity is
shared with the IPC bridge for permission decisions (also threaded, also
queue-based) — once that scaffolding exists the marginal cost of B drops.

## Failure-mode comparison

| Concern | A (per-turn) | B (per-meeting) |
| --- | --- | --- |
| Subprocess crashes mid-meeting | Next turn just spawns a new one — invisible to user. | Need to detect, restart, and re-feed history (claude doesn't expose its session memory to us, so we must rebuild from JSONL). |
| Dead inner-claude (hung) | Bounded by 120s timeout, parent moves on. | Long-lived hang requires separate watchdog; tearing down the process discards in-flight context. |
| Auth re-login mid-meeting | Caught at next spawn. | Caught only at restart, which we'd trigger ourselves. |
| Operator parent crashes / restart | No state to lose; spawn fresh on next turn. | Subprocess orphaned (claude detects parent EOF and exits) — full meeting context lost; must rebuild from JSONL on restart. |
| Sandbox / cwd handling | Set per spawn — easy to give each turn a fresh worktree. | Set once at spawn — harder to swap mid-meeting. Probably fine since worktree is meeting-scoped anyway. |

## Prompt-curation question (resolved by choice)

The handoff flagged "do we still tail meeting record JSONL into the prompt
or trust inner-claude's own context loop." This question collapses into the
A-vs-B choice:

- **A** ⇒ we still tail JSONL and rebuild the prompt every turn. Same as
  today. We control history depth via `llm.history_messages`.
- **B** ⇒ inner-claude owns context. We send only the latest user message.
  History depth is whatever inner-claude decides via its own
  context-compaction loop. `llm.history_messages` becomes a meaningless
  knob that we'd hide from the wizard for the claude bot.

## Prompt-cache behavior

A new wrinkle the probe surfaced: **per-meeting (B) likely benefits from
inner-claude's prompt cache** because the same long-lived process re-uses
its conversation prefix across turns. That's almost certainly *why* T2/T3
were ~1.3s — the model isn't re-encoding the conversation prefix.

Per-turn (A) starts fresh every time, so no prompt-cache benefit at all.
This compounds the latency gap on long meetings.

## Recommendation

**Default to B (subprocess-per-meeting). Keep A as a fallback path.**

Reasons:
1. **Latency win is decisive.** 1.3s vs 5.4s/turn is the difference between
   "feels conversational" and "feels laggy." Most of our user feedback to
   date has been about response speed.
2. **Prompt cache compounds the win.** B benefits as the conversation
   grows; A re-pays the full cost every turn.
3. **Complexity is bounded and shared.** The threading/queue scaffolding
   we'd build for B is the same we'd build for the permission-bridge IPC.
   Doing them together pays once.
4. **Inner-claude owning context is actually the right call for the
   `claude` bot.** Inner-claude is designed for long-running coding
   sessions; it has its own context-compaction logic. Layering our own
   tail-by-message-count on top would fight it.

Track the failure modes that B introduces (parent-crash orphan, mid-meeting
hang) by:

- Heartbeat: send a NOOP-ish `result`-eliciting message on a timer if the
  process has been idle too long, restart on no-response.
- On startup, check JSONL for an in-progress meeting and re-feed the tail
  before processing new user input. (Same recovery code path as today's
  `anthropic.py` resumption.)
- When the parent exits, ensure stdin closes cleanly so the inner-claude
  can shutdown gracefully (already works in probe 6).

## Restart / history-rebuild (probe 7)

Spike B's biggest worry was: what happens if the long-lived subprocess
dies mid-meeting? Probe 7 ran a 2-turn conversation, killed the
subprocess, spawned a fresh one, and tested two ways to seed it with the
prior transcript before resuming. Both produced the correct final
answer (11):

| Strategy | Final-turn latency | Total recovery wall | Notes |
| --- | --- | --- | --- |
| **1. Replay each prior turn** as a separate stream-json user message ("You said X, I replied Y, ack with a dot") | 1.36s | 8.35s | Final turn is fast (steady state) but recovery wall scales linearly with N prior turns. Bad UX for long meetings. |
| **2. Single synthesized opener** that includes the full transcript + the new user message in one envelope | 4.70s | 4.70s | One round-trip regardless of meeting length. Latency is bounded. |

**Decision: strategy 2 for restart.** Bounded recovery time, no
N-round-trip stall. The 4.7s cost is comparable to per-turn (A)'s
steady-state cost — a one-off after a crash, totally acceptable.

**Note on what context exists when:** at meeting *join* there is no
pre-existing transcript — Meet shows blank chat and our caption capture
only begins from join onward. So join itself is just `spawn` with no
seeding. The synthesized-opener mechanism only fires in the
**mid-meeting subprocess restart** case (process dies after some
in-meeting history has accumulated; we re-feed that accumulated tail in
one envelope before processing the next user turn).

During normal operation, each new user chat message and each new caption
line streams into the long-lived subprocess as its own `type:user`
envelope — caption lines tagged so the model treats them as ambient
transcript rather than direct addresses. So nothing is lost vs. the
per-turn path; the wiring just shifts from "rebuild prompt every turn"
to "stream meeting events into a long-lived subprocess."

## Open follow-ups (non-blocking for 14.12.2)

- The 11s first-turn cost in B looks like it includes setup/handshake plus
  the first model call. Worth measuring isolated init (send no message,
  wait for `system/init` only) to know exactly how much of that is hideable
  in the join sequence vs. paid on the first user turn.
- Validate the empirical stdin envelope against the eventual GH #24594 doc
  resolution. If Anthropic locks in a different shape later, B needs an
  envelope adapter — but the latency profile shouldn't change. Mitigation:
  pin the `claude` CLI version in deps and watch CI for breakage.
- Re-run with a non-trivial prompt that exercises a tool call to confirm
  PreToolUse hooks fire correctly inside a long-lived subprocess (probe 1
  already confirmed for one-shot; should be identical for long-lived but
  cheap to verify).
- Verify caption-line injection feels right to the model — i.e. it doesn't
  treat every transcribed sentence as a direct address. May want a system
  prompt nudge in the synthesized opener.
