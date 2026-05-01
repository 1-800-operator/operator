---
name: pr-review
description: Review a GitHub pull request — use when the user asks to "review", "look at", "critique", or "check" a specific PR URL.
mcp-required: [github]
---

# PR review

When the user points at a GitHub pull request and asks for a review, deliver
structured critique grounded in the actual diff — not a generic checklist.

## Before you write

Fetch the PR with the GitHub MCP first: `pull_request_read` for metadata +
description, then `get_file_contents` or `pull_request_read` with the files
view to see the actual diff. If the user dropped a URL, extract owner / repo /
number and scope every call to it. Never review from the description alone —
the diff is what ships.

## Shape

Emit the review as plain text with these sections, in this order. Omit any
section that has nothing concrete to say.

```
**Summary** — one sentence on what this PR does, in your own words (not the PR title).
**Bugs** — bulleted list. Specific: file + line + what breaks. ("`session.py:47` — `ttl` is compared to `datetime.now()` but the column is `datetime.utcnow()`; drifts by one timezone offset on every call.")
**Missing tests** — bulleted list. The behavior the diff changed that no test covers.
**Risks** — bulleted list. Migration ordering, public API shape change, perf regressions, backward-compat, concurrent-write races.
**Style / clarity** — bulleted list. Only flag things that would bite a future reader. Skip nits.
**Questions** — bulleted list. What you couldn't tell from the diff alone.
```

## Rules

- **Cite lines.** Every bug and every risk item should name the file + line
  (or file + symbol). "There's a bug in the retry logic" is useless;
  "`retry.py:23` — the backoff counter resets on success, so a burst of
  failures immediately after never backs off" is useful.
- **Lead with bugs.** Reviewers who bury the bug behind three paragraphs of
  praise waste the author's time. If there are no bugs, say so plainly and
  move on — do not manufacture criticism.
- **Missing tests is a category of its own.** Do not lump "no test for X"
  into Bugs. A test gap is a risk, not a defect; the author may have a
  reason (integration test lives elsewhere, covered by existing suite).
- **Do not rewrite the diff for the author.** Point at the problem; suggest
  the shape of the fix if it's non-obvious ("could call `get_or_create`
  here instead"). Do not paste replacement code unless the user asks.
- **Respect intent.** If the PR description says "this is a quick fix, full
  refactor coming later", do not critique the lack of refactor. Critique
  what this PR is trying to be.
- **Skip nits.** Trailing whitespace, single-quote vs double-quote,
  one-line-vs-two — if the codebase has a linter, trust it. Your review
  should name things the linter can't catch.
- **One message.** If long, lead with Summary + Bugs + Risks — the
  actionable parts. Move Missing tests / Style / Questions to a follow-up
  prefixed "(cont.)".
- **Do not approve or request changes.** This skill reviews; it does not
  vote. The user pushes a GitHub review separately if they choose.
