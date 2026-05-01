---
name: release-notes
description: Draft release notes from a commit range — use when the user asks for "release notes", "changelog", "what shipped", or "what changed between X and Y".
mcp-required: [github]
---

# Release notes

When the user asks what changed between two points on a repo's history —
two tags, two SHAs, main vs a release branch — draft a grouped changelog
they can post to a release, a Slack channel, or a PRD.

## Before you write

Pull the real commit list with the GitHub MCP: `list_commits` scoped to the
two endpoints (ref=<head> and since=<base> if using dates, or the
owner/repo/sha range the user gave). Cross-reference with `list_pull_requests`
(state=closed, merged into the range) to get PR numbers + titles. If either
endpoint is missing or ambiguous, ask — do not guess the range.

## Shape

Emit the notes as plain text with these sections. Use the headers the user's
team uses if you can tell from recent releases; otherwise default to this.
Omit any section with no entries.

```
## Release <tag or "between <base> and <head>"> — <date>

### Features
- <one-line description> (#<PR number>)

### Fixes
- <one-line description> (#<PR number>)

### Changes
- <one-line description> (#<PR number>)    # breaking or behavior-visible but not a feature or a bug

### Internal
- <one-line description> (#<PR number>)    # refactors, test-only, build/CI
```

## Rules

- **Group by intent, not by author.** A reader wants to know what changed,
  not who changed it. If commit titles don't disambiguate, open the PR
  (`pull_request_read`) and summarize from the description.
- **One line per PR.** A release note is a tweet, not a diff summary. If
  the PR bundles multiple changes, list the most user-visible one and
  let the PR link carry the rest.
- **PR numbers as hashtags, not URLs.** `(#1234)` renders as a link in
  GitHub, Slack, and most Markdown. Only use full URLs if the user
  explicitly asks.
- **Drop the noise.** Merge commits, version bumps, "fix typo", "update
  changelog" — skip unless the user says "include everything". Release
  notes are for readers, not for audit.
- **Breaking changes go under Changes with "**BREAKING**" up front.** A
  single breaking change can eat the release's adoption story; make it
  impossible to miss.
- **Cite the range.** Lead with the commit range or tag pair so the
  reader can reproduce the list. "From v1.4.0..v1.5.0" is a contract.
- **If the range has nothing interesting, say so.** "Only dependency
  bumps and a test flake fix since v1.4.0 — no user-visible changes"
  is the right answer. Do not pad.
- **One message.** If long, lead with Features + Fixes + Changes —
  that's the release surface. Move Internal to a follow-up prefixed
  "(cont.)".
