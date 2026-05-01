---
name: migration-plan
description: Produce a phased migration plan for a tech-debt or refactor discussion — use when the user asks "what would it take to migrate X to Y", "give us a migration plan", "how do we sequence this refactor", or "what's the blast radius of changing Z?".
mcp-required: [claude-code]
---

# Migration plan

When the team is discussing a tech-debt rip-out or a non-trivial refactor,
produce a phased plan that converts the conversation from "should we do
this?" into "how do we sequence it?". Read-only analysis — no code is
written, no worktree branches are created.

## Before you write

Delegate to `claude-code` with `--permission-mode plan` semantics: the
session uses `Read` / `Grep` / `Glob` / `Bash` (read-only) only — no
`Edit`, no `Write`. The task string must say: (a) what's being migrated
*from* and *to*, (b) the codebase root, (c) the explicit instruction to
return a phased plan with files-touched + breaking-change blast radius
per phase, (d) the explicit instruction to *not* generate code — this is
a plan, not an implementation.

If the user named a vague target ("modernize the auth layer") without
naming the destination, ask what they're migrating *to* before delegating
— "modernize" generates handwaving plans. "Move from session cookies to
JWT" generates real ones.

## Shape

Emit the plan as plain text in chat. Long but readable — this drives a
meeting conversation, so the structure matters more than brevity.

```
**Migration** — one sentence: from <X> to <Y>, in <repo>.

**Surface area** — one line on scope: how many files / modules / call sites are affected. Pulled from the analysis, not guessed.

**Phases** — numbered list of 3–7 phases. For each:

  Phase N — <name>
    What — one sentence on what this phase does.
    Files — bulleted list of the concrete files touched.
    Blast radius — what breaks for callers / consumers if this phase ships alone. "None — internal refactor", "API consumers must update <field>", "DB migration; downtime needed", etc.
    Depends on — phase numbers that must ship first (or "none — can ship anytime").
    Rough size — XS / S / M / L / XL on the team's calibration.

**Risks** — bulleted list of things that could blow the plan up. Cross-team coordination, performance regressions, data migration ordering, public API contract changes, third-party dep incompatibilities.

**Sequencing recommendation** — one paragraph on which order to ship the phases and why. Often this is "do the no-blast-radius phases first to de-risk, then the breaking ones with a deprecation window."

**Open questions** — bulleted list of things the analysis couldn't resolve. Missing requirements, ambiguous design decisions, unknown consumer surface.
```

## Rules

- **3–7 phases, not 12.** A 12-phase plan is a refactor disguised as a
  migration; either the scope is too big (recommend breaking into a
  separate "stop digging" prerequisite migration first) or the phases
  are too granular (collapse). Aim for phases that are each "a week of
  work for one engineer."
- **Blast radius per phase, not just the whole migration.** The value is
  in knowing which phases can ship safely vs. which require coordination.
  A phase with "no breaking changes" can ship anytime; a phase with
  "all API consumers must update" needs a deprecation window. The
  ordering recommendation falls out of this.
- **Cite real files.** Every phase's "Files" bullet names actual paths.
  "The auth layer" is not files; "`src/auth/session.py`,
  `src/auth/middleware.py`, `src/db/migrations/0042_session_to_jwt.sql`,
  `tests/test_auth/`" is. If the analysis can't pin a file, say so in
  Open questions — do not fabricate.
- **No code in the output.** Not even sketches, not even pseudocode.
  This is Plan Mode by design. If the user asks for a code sketch
  during the conversation, finish the plan first and offer a separate
  delegation: "I can prototype phase 1 in a worktree once we lock the
  plan — say the word."
- **Surface what would balloon the plan.** Risks bulleted explicitly,
  same shape as `scope-estimate`. The number of phases is less useful
  than the list of things that could turn 5 phases into 12.
- **Sequencing is a recommendation, not a mandate.** The team owns the
  decision. Lead the recommendation with "I'd suggest…" and end with
  "open to other orderings — what does your release schedule look like?"
- **Do not commit to phase sizes as deadlines.** Same rule as
  `scope-estimate`. T-shirt sizes are a shape, not a promise.
- **All phases in one turn, one phase per paragraph.** Separate each
  phase block with a blank line so the harness flushes each as a
  distinct Meet message — readable in chunks without you pausing across
  turns. Deliver Migration → Surface area → all phases → Risks →
  Sequencing → Open questions in this one turn. The team can interrupt
  mid-stream to redirect, but don't stop preemptively.
- **No edits, ever.** This skill never delegates work. If the user
  approves the plan and wants to start phase 1, they invoke the
  delegation use case separately. Two-step: plan, then build.
