# PM

Product / standup partner — files Linear tickets from spoken commitments,
drafts PRDs from discussion, and posts structured standup summaries when asked.

## Who it's for

PMs and tech leads who want a notetaker that can actually file the work.
Ask it to summarize standup, draft a PRD from what was just discussed, or
turn "I'll ship X by Friday" into a real Linear ticket with an owner.

## What you need

- **Anthropic API key** — for the LLM
- **GitHub personal access token** — for repo context (issues, PRs, code)
- **Linear account** — authenticated on first run via `mcp-remote` (browser OAuth)
- **GitHub MCP server binary** — `github-mcp-server` in the repo root ([releases](https://github.com/github/github-mcp-server/releases))
- **Captions turned on in Meet** — PM listens to the room via Meet's live
  captions. Turn them on before joining (`CC` button in the Meet toolbar).

## Setup

```bash
# 1. Fill in your API keys
cp agents/pm/.env.example .env
# Edit .env with your keys

# 2. Run — direct URL or auto-open meet.new
operator run pm https://meet.google.com/xxx-yyyy-zzz
operator run pm
```

**First run:** Linear MCP opens a browser window for OAuth. Authenticate once;
the token is cached by `mcp-remote`. If the browser doesn't open, check the
terminal for an auth URL to paste.

## Using it

- **"@operator file a ticket for that"** — PM takes the recent discussion
  and creates a Linear issue. It'll ask for missing pieces (team, priority)
  rather than guessing.
- **"@operator draft a PRD from this"** — PM emits a structured one-pager
  (problem / user / goal / scope / open questions) from the discussion.
  Uses the bundled `prd-from-discussion` skill.
- **"@operator wrap up"** or **"@operator summarize"** — PM posts a
  structured recap: decisions, action items with owners, blockers, tickets
  filed, open questions. Uses the bundled `standup-summary` skill.

## Demo

<!-- TODO: 15s GIF showing a Linear ticket materializing in chat while the speaker is still mid-sentence -->
