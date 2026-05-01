---
name: codebase-walkthrough
description: Walk the team through how a subsystem works — use when the user asks "how does X work in this codebase?", "trace the X flow", "walk us through Y", or new-hire-on-the-call onboarding asks.
mcp-required: [claude-code]
---

# Codebase walkthrough

When the user asks how a subsystem fits together — usually during onboarding,
an architecture review, or before someone changes load-bearing code — walk
them from entry point through the call graph to the data structures, citing
real `file:line` for every hop. The bar is "this matches what a senior
engineer would have walked the new hire through."

## Before you write

Delegate to `claude-code` against the user's local working directory. The
delegated session does the read work — `Glob`, `Grep`, `Read` — across
whatever working tree the user has on disk; this is read-only, no edits, no
worktree branch needed. If the user named a repo path, pass it; otherwise
ask which repo to walk before starting.

If the team named a *concept* ("how does auth work") rather than a file,
start with `Grep` for the concept's vocabulary (route prefixes, function
names, env vars) and let the search anchor the entry point. Don't guess
file paths from names — the codebase's actual conventions matter.

## Shape

Emit the full walkthrough in a single turn. Format each hop as its own
paragraph, separated by a blank line — the harness streams paragraphs as
separate Meet messages, so the team gets readable chunks without you
pausing. Don't stop and wait for the user to ask for the next hop;
deliver entry point → all hops → data → external calls → "Questions?"
close in this one turn.

```
**Entry point** — `<file:line>` — one sentence on what triggers this code path (HTTP route, CLI command, scheduled job, event handler).

`<file:line>` — one sentence on what this layer does (validate input, fetch user, dispatch event). Name the call site that got us here.

`<file:line>` — same shape. Continue for 2–4 total hops.

**Data** — the primary structures touched along the way (DB tables, in-memory caches, message queues). Name each at `<file:line>` where it's defined.

**External calls** — any third-party APIs, MCP tools, or services this path hits. One line each.

**Questions?** — close with an offer to drill into any hop in more depth.
```

## Rules

- **Cite real lines.** Every hop names `file:line`. "There's a function in
  the auth module that handles this" is useless; "`src/auth/session.py:47`
  — `Session.refresh()` — refreshes the access token if expiry is within
  60s" is useful. If you can't pin a line, drill until you can or say so.
- **One hop per paragraph, all hops in one turn.** Separate hops with a
  blank line so the harness flushes each as a distinct Meet message —
  readable in chunks, but never paused mid-walkthrough. The user asked for
  the whole walkthrough; give them the whole walkthrough. Pause only at
  the "Questions?" close. The team can still interrupt mid-stream to
  redirect ("skip hop 3, we don't use that path") — but don't stop
  preemptively.
- **2–4 hops, not 12.** The goal is the shape of the flow, not a complete
  trace. If the path has 12 hops, summarize the middle: "hops 3–8 are the
  per-event handlers in `events/handlers/*.py` — same pattern as hop 2,
  one file per event type." Then resume detail at the convergence point.
- **Concept names beat layer names.** "The auth module" is vague; "the
  session refresh path" or "the OAuth callback handler" anchors. Use the
  team's vocabulary — pick it up from comments, docstrings, and file
  names.
- **Flag what you didn't read.** If the trace branches and you only
  followed one fork, say so: "this also dispatches to the audit log path
  in `audit/`, not traced here — ask if you want it." Honest scope is
  more useful than fake completeness.
- **No edits.** This is read-only by design. If the user asks for a fix
  mid-walkthrough, finish the walkthrough first, then offer to delegate
  the change separately. A walkthrough interrupted by edits is two
  half-jobs.
- **End with an offer to go deeper.** "Want me to drill into hop 2's
  retry logic?" turns a one-shot into an interactive session — which is
  what onboarding actually needs.
