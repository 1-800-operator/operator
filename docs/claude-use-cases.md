# Top 10 Claude-in-a-Meeting Use Cases

*Phase 15.10.3 deliverable, session 159. Doubles as the test script for Phase 14.8(e) — a brand-new user runs `operator run claude <fresh-meet-url>` against this list — and as raw material for the second hero-framing post of Phase 16.4 (launch).*

## What "meeting use case" means here

The full Claude Code use-case surface is wide: codebase navigation, debugging, refactoring, writing tests, creating PRs, building features, generating docs, running scheduled audits, piping output through Unix tools, parallel worktree sessions. Most of those assume one engineer at a terminal with their full attention.

A meeting changes the constraints:

- **Multiple humans are present.** The bot's reply needs to be readable as a chat message, not a 200-line stream.
- **Latency is felt.** Anything that takes more than ~30s without a heartbeat reads as broken.
- **The trigger has to feel natural to say out loud.** "@claude check this PR" works; "@claude please use the `pr_review` subagent with `--permission-mode plan`" does not.
- **Output should land somewhere shared.** A diff, a Linear comment, a PR review, a Drive doc — not a local-only artifact.
- **Context is collaborative.** Other people in the meeting will redirect mid-flight ("not that file, the other one"), so the bot needs to handle interjections gracefully.

The ten use cases below are the highest-frequency Claude Code workflows from the research, **filtered for meeting fit** and **weighted by how well Operator's flagship `claude` agent can serve them today** (delegation via the bundled `claude-code` MCP, plus the user's auto-imported MCPs — typically Linear, GitHub, Sentry, Calendar, Drive, sometimes Figma/Slack).

Ordering: roughly meeting-frequency × value. The first three are the ones every bug-standup / code-review / sprint-triage call will hit; the last three are higher-leverage but trigger less often.

---

## 1. Pull-request review during a code-review call

A team is walking a PR together. Someone asks the bot for a second pair of eyes — bugs, missing tests, risky edges, style.

**Trigger phrase (in chat):** `@claude review https://github.com/<org>/<repo>/pull/142`

**MCPs exercised:** `github` (`pull_request_read`, `get_file_contents`, `list_pull_request_files`).

**Pass/fail signal:** the bot replies with a short summary plus a bullet list where every bug/risk claim cites `path:line`. No claim should be untraceable to a real diff hunk.

Anthropic's own internal Claude Code Review feature is the highest-leverage use case the research surfaced — engineers marked <1% of findings as incorrect across months of production use. Meeting-shaped trigger because the team wants the analysis posted *now*, in front of everyone, not as a CI comment they'll read later.

---

## 2. Live bug triage from a Sentry alert (or stack trace pasted in chat)

Bug standup. Someone shares a Sentry link or pastes a stack trace into chat. The team wants probable cause + a proposed fix in the next 60 seconds.

**Trigger phrase:** `@claude what's blowing up here? <sentry-url-or-paste>`

**MCPs exercised:** `sentry` (`get_issue`, `get_event`) → `claude-code` (delegate to inspect the named files in the user's repo).

**Pass/fail signal:** the bot names (a) the probable failing frame at `path:line`, (b) one hypothesis for the cause, (c) a proposed fix or "needs more data — try X". No "I couldn't find anything" without a specific reason.

This is the meeting-shaped version of the "fix bugs efficiently" workflow from the official docs, plus the `live-bug-triage` bundled skill. Five-phase delivery (symptom → frame → cause → fix → optional delegate) is already encoded in the skill — the bot will post each phase as its own message so the team can redirect between any of them.

---

## 3. Codebase walkthrough for an onboarding or architecture meeting

A new hire is on the call. Or it's an architecture review. Someone asks the bot to explain how a subsystem fits together.

**Trigger phrase:** `@claude walk us through how authentication works in this codebase` or `@claude what touches our payment flow?`

**MCPs exercised:** `claude-code` (delegate runs `claude -p` against the user's local working directory with `Read`/`Grep`/`Glob` — works whether or not the repo is GitHub-hosted; no worktree edits, read-only).

**Pass/fail signal:** reply names the entry point at `path:line`, traces 2–4 hops through the call graph with file citations, and ends with the primary data structures involved. The Anthropic Product Engineering team's #1 reported use was reducing onboarding load on senior engineers — so "this matches what a senior engineer would have walked the new hire through" is the bar.

---

## 4. Live coding delegation — implement a small feature, return a branch

The team has just sketched a small feature in conversation ("we should add a `--dry-run` flag to the export script"). Hand it off so the work happens in parallel while the meeting continues.

**Trigger phrase:** `@claude can you add a --dry-run flag to scripts/export.py and open a PR?`

**MCPs exercised:** `claude-code` (core — delegate via worktree against the user's local repo, returns branch name + diff summary). `github` (optional — only if the user asks the bot to *also* open a PR after the worktree returns; otherwise the delegated session can `gh pr create` itself, or the user pushes manually).

**Pass/fail signal:** within ~3–10 min the bot returns a worktree branch name, a one-paragraph summary of the diff, and either a PR link or "ready to land — say `@claude land it` to push." The team can keep talking; the work is async.

This is the workflow Anthropic's RL Engineering team relies on for small-to-medium features. Meeting-shaped because the kickoff happens during the call but the work doesn't block it. Note: the bot delegates against the user's local working directory in whatever state the user pulled it to — it does not `git pull origin main` for them. Pull before delegating if you want the latest base.

---

## 5. Sprint triage / T-shirt scope estimation from a Linear ticket

Sprint planning. Walking the backlog ticket-by-ticket. The bot prices each one before it lands in the sprint.

**Trigger phrase:** `@claude how big is INGEST-42?` or `@claude scope this ticket: <linear-url>`

**MCPs exercised:** `linear` (`get_issue`) → `claude-code` (delegate to inspect the files the ticket implies).

**Pass/fail signal:** reply gives a T-shirt size (XS/S/M/L/XL) plus three lines: concrete files to touch, the biggest risk, and any unknowns the ticket doesn't resolve. No code in the reply — this is for a planning conversation, not an implementation one. Matches the bundled `scope-estimate` skill exactly.

---

## 6. Refactor / migration plan from a tech-debt discussion

Architecture meeting on a tech-debt item. "What would it take to migrate from REST to GraphQL?" The team wants a phased plan, not a six-hour live refactor.

**Trigger phrase:** `@claude give us a migration plan for moving auth from sessions to JWT`

**MCPs exercised:** `claude-code` (delegate with `--permission-mode plan` — read-only analysis, no edits).

**Pass/fail signal:** the bot returns a numbered plan (3–7 phases), each phase with the files it would touch and the breaking-change blast radius. No code generated — this is Plan Mode output, meant to drive the conversation.

The official docs flag Plan Mode as "perfect for exploring codebases, planning complex changes, or reviewing code safely" — this is the meeting-shaped version of that workflow.

---

## 7. Test generation for a function the team just wrote

Pair-programming call. The team finishes a function and someone says "we need tests for this." Hand it off rather than break flow.

**Trigger phrase:** `@claude write tests for the new validate_payload function in src/api/handlers.py`

**MCPs exercised:** `claude-code` (delegate, lands a test file in a worktree).

**Pass/fail signal:** the bot returns a test file with at least one happy-path case + 2–3 edge-case tests (boundaries, error inputs, empty values). The style should match the project's existing test files — matching framework, matching assertion idiom. The Product Design and Inference teams at Anthropic both flag this as a primary use.

---

## 8. Design handoff spec from a Figma frame during a design review

*Conditional — fires only for users with Figma + the Figma MCP wired into their Claude Code setup. Within the design-review-with-engineers subset of meetings, this is *the* primary use; across the full claude-agent user base it's narrower than #1–#5.*

Designer + dev meeting. The designer drops a Figma URL. The bot generates the engineer-facing spec — layout, spacing, typography, colors, states, assets, open questions.

**Trigger phrase:** `@claude turn this Figma frame into a handoff spec: <figma-url>`

**MCPs exercised:** `figma` (read frame structure + tokens) — and matches the bundled `design-handoff-spec` skill on the designer agent. On the claude agent, this fires when the user has the Figma MCP wired into their Claude Code setup.

**Pass/fail signal:** spec posted with all six sections (layout / spacing / typography / colors / states + interactions / assets) and an "open questions" tail. Every numeric claim — sizes, hex codes, spacing — must be traceable to the Figma frame. WCAG AA contrast failures flagged inline.

---

## 9. PRD draft from the meeting discussion itself

Stakeholder discussion. PM + eng + design talking through a feature. At the end, "draft a PRD from what we just talked about."

**Trigger phrase:** `@claude turn the last 20 minutes into a PRD` (works because Operator's caption pipeline is on by default for the claude agent, so the bot has been listening).

**MCPs exercised:** `linear` (file as ticket draft) or `drive` (save as Doc); `gmail` (notify stakeholders, optional).

**Pass/fail signal:** PRD posted (or linked) with: problem statement, goals, non-goals, proposed approach, acceptance criteria, open questions. Every claim should be traceable to something said in the call — if the bot invents a goal nobody mentioned, that's a fail.

This one stretches Claude Code's typical surface (it leans on captions, which is Operator-specific) but matches a real meeting workflow stakeholders ask for repeatedly.

---

## 10. Release notes / changelog from a commit range

Release review meeting or end-of-sprint demo. "What shipped this week?"

**Trigger phrase:** `@claude give us release notes from main since last Friday` or `@claude release notes from v1.4.0 to HEAD`

**MCPs exercised:** `github` (`list_commits`, `list_pull_requests`).

**Pass/fail signal:** grouped changelog (Features / Fixes / Changes / Internal), each line citing `#<PR-number>` for traceability, breaking-change callout pinned at the top if any. Matches the bundled `release-notes` skill exactly.

---

## What didn't make the cut (and why)

- **Documentation generation (JSDoc / docstrings).** High-frequency in solo work, but not a meeting trigger — nobody calls a meeting to add docstrings. Better fit for `operator edit` + Claude Code locally.
- **Resume previous session / `--continue`.** Pure CLI ergonomics, no meeting analogue.
- **Run on a schedule (Routines / GitHub Actions).** Cron-shaped, not meeting-shaped. The Operator equivalent is the scheduled-agent layer, separate from the in-meeting bot.
- **Pipe in / pipe out (Unix utility mode).** Solo-at-terminal use case; useless when the trigger is voice-or-chat in a meeting.
- **Image analysis / "what does this screenshot show?"** Plausible meeting use case (someone screen-shares an error), but Operator's chat panel doesn't currently surface images to the LLM, so this is gated on Phase 17+ work. Park.
- **Custom subagent invocation (`/agents`).** Power-user feature; abstract too much for a meeting trigger. The functionality folds into use cases #1, #4, #6 implicitly when the user has those subagents configured.
- **Worktree management (`--worktree`).** Use case #4 already covers the meeting-relevant slice (delegate-and-land); manual worktree juggling is solo-CLI work.

---

## How to use this list

**As the Phase 14.8(e) test script.** Run `operator run claude <fresh-meet-url>` on a clean second mac. For each of the ten cases above, post the trigger phrase verbatim (or a faithful rephrase) into chat. Mark pass / fail per the signal column. Cases that depend on user-specific MCPs (e.g. #8 Figma) skip if the user doesn't have that MCP wired in — note as N/A, not fail.

**As launch-campaign material (Phase 16.4 second hero post).** The header framing — "Claude Code in your Google Meet" — is already locked. This list is the proof points: ten concrete, named meeting moments where the bot earns its seat. Pick the three highest-fit to your demo audience and lead with them.

---

## Sources

- [How Anthropic teams use Claude Code — claude.com blog](https://claude.com/blog/how-anthropic-teams-use-claude-code) — primary reference for team-by-team workflow patterns (Product Engineering, Product Design, Security, Inference, Data Infrastructure, Growth, Legal teams)
- [Common workflows — Claude Code docs](https://code.claude.com/docs/en/common-workflows) — official taxonomy of supported workflows (codebase exploration, bug fix, refactor, tests, PRs, Plan Mode, subagents, scheduled tasks)
- [Code Review for Claude Code — claude.com blog](https://claude.com/blog/code-review) — context for use case #1 (multi-agent PR review with <1% false-positive rate in Anthropic's own testing)
- [Claude Code 2026: The Daily Operating System Top Developers Actually Use — Towards AI](https://medium.com/@richardhightower/claude-code-2026-the-daily-operating-system-top-developers-actually-use-d393a2a5186d) — community signal on which 2026 workflows actually stuck
- [Effective Claude Code Workflows in 2026 — Sean Moran, Medium](https://medium.com/@sean.j.moran/effective-claude-code-workflows-in-2026-what-changed-and-what-works-now-c93ebc6f8f50) — corroboration on Plan Mode + structured execution as primary 2026 patterns
- [How Anthropic teams use Claude Code (PDF report)](https://www-cdn.anthropic.com/58284b19e702b49db9302d5b6f135ad8871e7658.pdf) — long-form team-by-team breakdown
