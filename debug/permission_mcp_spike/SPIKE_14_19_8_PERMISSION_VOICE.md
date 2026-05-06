# Spike — Permission prompt voice for Phase 14.19.8

**Date:** 2026-05-05 (session 196)
**Question:** how do we get the bot's voice into the permission prompt
without a full rewrite of the PreToolUse bridge?
**Verdict:** mech 1.5 — system-prompt steering. Plumbing unchanged.

## Three mechanisms considered

1. **Mech 1** — return `permissionDecision: "ask"` with a synthesized
   bot-voice reason via the existing PreToolUse bridge. Hope `claude -p`
   surfaces the reason somewhere user-visible.
2. **Mech 2** — bypass the permission protocol entirely; the LLM
   authors a proposal in its reply stream, we stall the tool call,
   capture the next user message as approval/deny, resume.
3. **Mech 1.5** — keep the existing `allow`/`deny` bridge, but steer
   the inner-claude model via system prompt to ALWAYS narrate the
   action it's about to take in a chat reply BEFORE invoking the tool.
   The narration streams to chat first, then the templated card lands.
   Recovers the pre-pivot UX (bot voice in a separate message right
   before the card).

## Probe 9 — what does `claude -p` do with `ask` headlessly?

Returned `ask` with a reason on the first Write call.

**Result:** claude-CLI did NOT abort, hang, or silently deny. Instead
it injected the reason into the model as a `tool_result` event with
`is_error=True`, and the model emitted assistant text:

> The write was blocked with: "Want me to create that file? (probe9 — hook synthesized ask reason)"

That text streams out via `text_delta` events the same way any normal
reply would, so it WOULD reach Meet chat through our existing pipeline
without further plumbing.

**But** — the reason arrives wrapped in claude's own framing
(`"The write was blocked with: …"`), which is system-y, not bot-voice.
Stripping it would require fragile prompt post-processing. The tool
also can't be resumed in place: a `yes` reply would have to start a
fresh turn and our handler would have to remember which tool was
approved so the next call doesn't re-ask.

**Mech 1 verdict:** technically viable, UX-compromised by the
"blocked with:" prefix, plus state-management complexity for resumption.

## Probe 10 — does system-prompt steering reliably produce pre-tool voice?

Three trials with different tools (Write, Read, Bash). System prompt
included:

> CRITICAL TOOL UX RULE: before EVERY tool_use, you MUST first emit a
> short chat reply (one or two sentences) describing the action you're
> about to take. Phrase it conversationally, in your own voice.

Bridge auto-allowed every call (we want clean execution to observe the
reply pattern, not deny-side branching).

**Result — 3/3 trials emitted clean bot-voice text BEFORE the tool_use:**

| Trial | Tool  | Pre-tool text                            |
|-------|-------|-------------------------------------------|
| 1     | Write | `"I'll create that file now."`           |
| 2     | Read  | `"I'll read /etc/hosts now."`            |
| 3     | Bash  | `"I'll run that echo command now."`      |

**Mech 1.5 verdict:** viable, no plumbing changes needed.

## Why mech 1.5 wins for 14.19.8

1. **Plumbing unchanged.** The PreToolUse bridge stays exactly as
   today: `allow` / `deny` round-trip via named pipes, `_round_trip`
   posts the templated card to chat via `runner._send`, awaits
   yes/no, returns the decision. Zero rewrite.
2. **Voice ordering is correct by construction.** Claude-CLI flushes
   text-delta events to stdout BEFORE invoking the PreToolUse hook
   (the model's content has to be complete before claude-CLI knows
   what tool was called). Operator's reader thread processes those
   text deltas and flushes them to `on_paragraph` → meeting chat
   FIRST. The permission pump thread receives the hook request and
   posts the card SECOND. So the user sees bot voice → card → answer
   in that order, deterministically.
3. **Cheap.** ~30–60 min: add the steering paragraph to the
   `_append_system_prompt` baseline in `claude_cli.py` (or to the
   bot's `system_prompt`), live-test once against a real Meet, done.
4. **Recovers the pre-pivot UX.** Same shape the user remembers from
   sessions 177-188 — bot voice in a chat message just before the
   templated confirmation. The card stays sterile; the bot's voice
   lives in the message right before it.

## Open question for implementation

Where does the steering paragraph live?

- **Option A:** in `claude_cli._append_system_prompt` baseline (next
  to the transcript-tool backstop already injected by
  `set_meeting_record_path`). Pro: every claude bot gets it for free,
  can't be turned off by a misconfigured agent. Con: framework-level
  prescription that bot authors might want to override.
- **Option B:** in the bundled `claude` agent's `system_prompt` config.
  Pro: bot author can edit it via `operator edit claude`, fits the
  existing voice-customization pattern. Con: a user who hand-edits
  their system prompt and accidentally drops this rule will lose the
  pre-tool voice with no warning.

Recommend **Option A** — load-bearing UX features should ship as code
(per `feedback_capability_in_code_over_prompt`), not as user-editable
prompt directives. Add a one-line note in `system_prompt` documenting
that the framework injects this rule so authors aren't surprised.

## Files

- `cli_probe_09_ask.py` / `probe9_*.{jsonl,txt,console.log}`
- `cli_probe_10_pre_tool_voice.py` / `probe10_*.{jsonl,console.log}`
