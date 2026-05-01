# Designer

Design-review partner — pulls up Figma frames mid-meeting and critiques
layout and hierarchy with actual opinions.

## Who it's for

Designers and design-curious PMs who want a meeting participant that can
actually open the file, not just talk about it. Ask it to "show me the login
screen", "review the carousel", or "what's off about this spacing?" — it has
access to any Figma file your personal access token can see.

## What you need

- **Anthropic API key** — for the LLM
- **Figma personal access token** — create at `figma.com/settings` →
  "Personal access tokens". The `File content: Read` scope is enough for the
  default server. Used by the Figma MCP subprocess to read your files.
- **Captions turned on in Meet** — Designer listens to the room via Meet's
  live captions so it can react when you reference a screen out loud
  ("pull up the carousel"). Turn them on before joining (`CC` button in the
  Meet toolbar).

## Setup

```bash
# 1. Fill in your API keys
cp agents/designer/.env.example .env
# Edit .env with ANTHROPIC_API_KEY and FIGMA_TOKEN

# 2. Run — direct URL or auto-open meet.new
operator run designer https://meet.google.com/xxx-yyyy-zzz
operator run designer
```

## Using it

- **"@operator pull up the login screen — figma.com/design/\<key>?node-id=…"**
  — Designer fetches the frame and summarizes what it sees (layout shape,
  dominant type, spacing, 1-2 hierarchy observations).
- **"@operator review this — figma.com/design/\<key>?node-id=…"** — runs the
  bundled `design-review-feedback` skill: structured critique with
  *what works / what breaks / questions / suggestions*, grounded in the
  actual canvas. Specific, opinionated, no flattery padding.
- **"@operator the carousel feels off — what's the first thing you'd change?"**
  — conversational gut check, 1-3 sentences with specific numbers or element
  names.

## Which Figma MCP server?

The bundle defaults to **GLips** (`figma-developer-mcp`, community) —
authenticated via your Figma personal access token, read-only. It exposes
two tools: `get_figma_data` (file/frame structure) and `download_figma_images`.
That covers every read flow above.

Two alternatives, each with tradeoffs:

- **Write operations** — if you want Designer to *modify* Figma files
  (change text, move nodes, set colors, create frames), swap in Grab's
  [`cursor-talk-to-figma-mcp`](https://github.com/grab/cursor-talk-to-figma-mcp).
  It exposes ~25 mutation tools but requires installing their Figma plugin
  and running a local WebSocket bridge (`bun socket`). It also operates only
  on the file currently open in the plugin, not an arbitrary URL — so the
  "paste a Figma link in chat" UX is lost in exchange.
- **Figma's official MCP server** (`mcp-remote https://mcp.figma.com/mcp`)
  — OAuth-authenticated, read + write, but requires the MCP client to be in
  Figma's approved catalog. Operator is not (yet) in the catalog, so
  dynamic client registration returns `403 Forbidden`. If/when Operator is
  added, this becomes the best option: no plugin, no bridge, write
  operations included.

Swap either in by editing `mcp_servers.figma` in `agents/designer/config.yaml`.

## Demo

<!-- TODO: 15s GIF showing a Figma frame appearing in chat seconds after the speaker says "let's look at the carousel" — the hero framing -->
