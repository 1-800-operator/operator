---
name: design-handoff-spec
description: Produce an engineer-facing handoff spec from a Figma frame — use when the user asks for a "handoff", "spec", "engineering notes", or "dev-ready" version of a design.
mcp-required: [figma]
---

# Design handoff spec

When the user asks for a handoff spec on a Figma frame or flow, produce a
structured, engineer-facing document that captures what the engineer needs
to implement it — grounded in the canvas, not in the designer's head.

## Before you write

Fetch the frame with the Figma MCP (`get_figma_data`) before writing a single
number. Extract `fileKey` and `nodeId` from the URL the user shares. If the
user mentions a flow or multi-frame handoff, ask which frames to include
before fetching — whole-file fetches are enormous and usually not what they
mean. If you cannot fetch, say so and ask for a screenshot or a different
node-id; do not estimate tokens or spacing from memory.

## Shape

Emit the spec as plain text with these sections, in this order. Omit any
section with nothing concrete to say — padding a handoff invites the
engineer to stop reading.

```
**Frame** — name + Figma URL + one-line purpose.

**Layout** — overall structure. Vertical stack? Grid? Columns? Container width? Responsive breakpoint if the frame has variants.

**Spacing** — concrete numbers. Section gaps, padding, margins. Use the canvas, not round guesses.

**Typography** — per text style: size / weight / line-height / letter-spacing / color. Name the style if the file uses named tokens (e.g. "heading/xl").

**Colors** — hex codes (or token names if the file uses them) per role: primary, surface, text, border, state colors. Flag any that fail WCAG AA contrast against the background they sit on.

**States** — per interactive element: default / hover / active / focus / disabled / loading / error. If the frame doesn't show a state, mark it "not specified" — do not invent.

**Interactions** — what happens on tap/click/focus. Modal opens? Route change? Inline edit? Animation timing if shown.

**Assets** — images, icons, illustrations that need export. Format + size per asset. Call out if the frame uses a shared icon set vs one-offs.

**Open questions** — things the frame leaves ambiguous that the engineer needs answered before building.
```

## Rules

- **Numbers, not adjectives.** "Generous spacing" is not a spec. "24px
  between sections, 16px between cards, 8px inside a card" is.
- **Cite the canvas.** Every number or color comes from the fetched frame.
  If the Figma API returns a thin payload, say so — do not fabricate.
- **Token names beat raw values.** If the file uses `color/surface/primary`
  or `space/md`, name the token and include the resolved value in parens:
  `color/surface/primary (#F7F7F7)`. Lets the engineer wire the token,
  not the hardcoded value.
- **Flag contrast failures.** WCAG AA requires 4.5:1 for body text,
  3.0:1 for large text / UI. Calling it out in the handoff is cheaper
  than discovering it in QA.
- **States matter.** A handoff without interactive states is a half-handoff.
  If only the default is shown, say "hover / focus not specified — confirm
  with designer" as an open question, not an invention.
- **Assets with intent.** Don't list every image — list the ones that need
  export and what format (SVG for icons, WebP/PNG for imagery, @1x/@2x for
  raster). If the icons are in a shared library, say so and link the
  library frame.
- **One message is usually too short.** This is a spec. If it's long, split
  by major section ("Layout + Spacing first, then Typography + Colors,
  then States + Interactions") — do not truncate a handoff to fit one
  message.
- **Do not write CSS.** Handoffs give engineers the inputs; engineers
  write the code. If the user wants the CSS, they'll ask — delegate to
  `claude-code` if available.
