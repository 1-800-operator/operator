---
name: prd-from-discussion
description: Draft a structured PRD from meeting discussion — use when the user asks for a "PRD", "spec", or "one-pager" from what's been said.
---

# PRD from discussion

When the user asks for a PRD, spec, or one-pager based on the meeting
discussion so far, produce a structured draft that captures what was actually
said — not what a generic PRD template demands.

## Shape

Emit the PRD as plain text with these sections, in this order. Omit any
section that has nothing to say — do not pad with filler.

```
**Problem** — one sentence. What's broken or missing for the user.
**User** — who feels the problem. Role, context, or persona mentioned in the discussion.
**Goal** — what success looks like. Measurable where possible.
**Scope** — bulleted list of what's in.
**Non-goals** — bulleted list of what's explicitly out (only if the discussion ruled things out).
**Open questions** — bulleted list of things the discussion raised but didn't resolve.
**Owner / next step** — who's driving, what they're doing next.
```

## Rules

- **Quote the discussion** where a phrasing is specific or load-bearing
  ("we said the SLA is 99.5%, not 99.9%"). Don't paraphrase away precision.
- **Flag missing sections explicitly.** If the discussion never named a user
  or never stated a goal, write `Goal — not stated in discussion` rather than
  inventing one. The missing pieces are part of the signal.
- **Keep it to one turn, one section per paragraph.** Separate each PRD
  section with a blank line so the harness flushes them as distinct
  Meet messages — readable without overwhelming the chat panel. Don't
  say "continuing…" or split across turns; deliver the whole PRD
  (Problem → User → Goal → Scope → Non-goals → Open questions →
  Owner / next step) in one go.
- **Do not file a Linear ticket as a side effect.** This skill only drafts.
  If the user wants it filed, they'll ask — use `save_issue` then.
- **Cite owners by name only if the discussion named them.** Do not assign
  based on speaker unless the speaker committed ("I'll own this").
