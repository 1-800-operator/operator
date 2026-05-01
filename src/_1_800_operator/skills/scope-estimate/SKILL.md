---
name: scope-estimate
description: Estimate the scope of a proposed change — use when the user asks "how big is this?", for a "scope", "estimate", "sizing", or "T-shirt size" on a ticket, spec, or spoken ask.
---

# Scope estimate

When the user asks how big a proposed change is — from a ticket, a spec, or a
spoken ask — produce a rough scope estimate the team can act on in the meeting.
Do not write code. Do not file anything. Just the estimate.

## Before you write

Anchor on real code before estimating. If the user named a ticket or a PRD,
fetch it first (Linear MCP or the URL they dropped). If the affected code
lives in a repo the user named, use the GitHub MCP to list the likely
touchpoints — don't invent file paths. If you can't see the code, say so
rather than estimating in the dark.

## Shape

Emit the estimate as plain text with these sections, in this order. Omit any
section that has nothing concrete to say.

```
**Size** — XS / S / M / L / XL. One line on what that maps to (e.g. "S = half a day, one file touched, no migration").
**Files to touch** — bulleted list of the concrete files or modules. Guess conservatively; say "probably" when unsure.
**Blockers** — bulleted list of things that could balloon the estimate. Missing API, unclear requirement, flaky dep, pending design.
**Risks** — bulleted list of what could go sideways. DB migration, public API change, cross-team coordination, load-bearing test gap.
**Unknowns** — bulleted list of questions to answer before starting. If the estimate hinges on the answer, say so.
```

## Rules

- **Size calibrated to your team.** XS = under an hour. S = half a day. M = a
  day or two. L = a week. XL = "we should break this up." These are rough
  but do not use days or hours unless the user asks — the letter anchors
  conversation.
- **Name files, not layers.** "A change to the auth layer" is not an
  estimate. "`src/auth/session.py` + `tests/test_session.py` + a migration
  in `migrations/0042_...`" is.
- **Surface what would balloon it.** The value of an estimate is not the
  number — it's the list of things that would make the number wrong. Lead
  with blockers and risks before the user tests the size against their
  calendar.
- **Do not commit to the estimate as a deadline.** It's a shape, not a
  promise. Say "rough" or "ballpark" when surfacing it.
- **If the ask is genuinely under-specified, say XL + "need scope reduction".**
  "This is a week+ because the requirement covers three systems" is a
  useful answer. Padding an S to hedge is not.
- **One message.** If it runs long, lead with Size + Files + Blockers —
  those are the actionable parts. Move Risks + Unknowns to a follow-up
  prefixed "(cont.)".
- **No code.** This skill sizes; it does not implement. If the user wants
  an implementation, they'll ask — then delegate with `claude-code`.
