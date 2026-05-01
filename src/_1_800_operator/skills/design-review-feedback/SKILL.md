---
name: design-review-feedback
description: Deliver structured design critique on a Figma frame or screen — use when the user asks for a "review", "critique", "feedback", or "design pass" on a specific design.
mcp-required: [figma]
---

# Design review feedback

When the user asks for design feedback on a specific Figma frame, screen, or
flow, deliver a structured critique grounded in what's actually on the canvas
— not a generic checklist.

## Before you write

If the user hasn't pointed you at a specific Figma URL or frame, ask which
one. Do not critique from memory or imagination. If you have access to the
file, fetch the frame with the Figma MCP first so the critique is anchored to
real layout, type, and spacing — not assumptions.

## Shape

Emit the critique as plain text with these sections, in this order. Omit any
section that has nothing concrete to say — empty headers are noise.

```
**What works** — 1-3 bullets. Specific, not flattery. ("The vertical rhythm in the pricing column reads well — 24px between rows is the right beat.")
**What breaks** — 1-3 bullets. The thing that would make a user stumble. Cite the element by name where possible. ("Primary CTA loses against the hero image — contrast ratio looks under 3:1.")
**Questions** — bullets. What you couldn't tell from the frame alone. ("Is the carousel auto-advancing? If so, the dot indicators need a paused state.")
**Suggestions** — bullets. Concrete moves, not vibes. ("Bump the product card padding from 12 to 16. Drop the third badge — three competing labels dilute the offer.")
```

## Rules

- **Be specific.** "Spacing feels off" is useless. "The 8px gap between
  card and CTA crowds the CTA — try 16px" is useful. Always reach for a
  number, an element name, or a direct comparison.
- **No flattery padding.** Don't open "What works" with generic warmth
  ("Love the energy here!"). Lead with the specific thing that's working.
  If nothing's working, drop the section.
- **Don't hallucinate elements.** If the frame doesn't have a CTA, don't
  critique a CTA. If you only fetched one frame and the user mentions a
  flow, say so and ask for the rest of the frames.
- **Cite the canvas, not the spec.** Critique what's on screen, not what
  the brief promised. If the spec said "16px gap" and the frame shows 8px,
  that's a "what breaks" item — not a "questions" item.
- **One message.** If the critique runs long, lead with What breaks +
  Suggestions — those are the actionable parts. Move What works and
  Questions to a follow-up message prefixed "(cont.)".
- **No score, no rating.** This isn't a grade. Leave numerical ratings
  ("7/10", "B+") out — they invite arguments and don't help the designer.
- **Don't editorialize the designer.** Critique the design, not the
  decision-maker. "This needs more whitespace" — not "you didn't leave
  enough whitespace."
