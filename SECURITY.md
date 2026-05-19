# Security

Thanks for taking the time to report a security issue in Operator.

## Reporting a vulnerability

Email **shapirojojo@gmail.com** with:

- A description of the issue and its impact.
- Steps to reproduce — ideally a chat transcript, the `@claude` message
  involved, or a minimal sequence of `operator` commands.
- The commit hash or release you tested against.

Please **do not** open a public GitHub issue for security-sensitive reports.
Use a GitHub Security Advisory (Security → Advisories → New draft advisory)
if you prefer GitHub's flow over email.

## Response SLA

- **Acknowledgement** within 72 hours.
- **Triage and initial assessment** within 7 days.
- **Fix or mitigation plan** within 30 days for high/critical issues; lower
  severity may be batched with a regular release.

If I go longer than 72 hours without acknowledging, assume the email was
missed and nudge the same address.

## Recognition

Reporters who follow coordinated disclosure are credited by name (or handle,
your preference) in the release notes and GitHub Security Advisory that ships
the fix. No bug bounty — this is a solo open-source project.

## Data locality — what we do and don't see

Operator is a local tool. The substance of this matters for security:

- **No Operator-side server.** There is no service we run that your traffic
  flows through. No account, no API key issued by us, no telemetry, no
  remote storage.
- **Your data stays on your machine.** Meeting chat, captions, transcripts,
  and the dedicated dial Chrome profile all live under `~/.operator/` (file
  mode `600` / dir mode `700`) and `/tmp/operator.log`. None of it ever
  leaves your machine via anything operator does.
- **The LLM calls are yours.** Operator drives your existing `claude` CLI
  using your existing Claude subscription. Anthropic's data-handling
  policies for Claude Code apply unchanged; we are not in that loop.
- **Local code, local supply chain.** The CLI is `uv tool install`'d from
  source, the plugin is your `~/.claude/plugins/` install, the bundled MCP
  server runs in-process. No remote-control surface ships in either.

That framing is the right lens for everything below: the permissions
operator asks for are broad, but the access they unlock stays inside the
boundary of your machine.

## Threat model and hardening

`docs/security.md` is the canonical threat model — trust boundaries, the
permission posture, the mitigations in place, and the documented residual
risks (R1–R5). **Read it before filing a report.** The issue you are seeing
may be a known, documented tradeoff rather than a defect — see the next
section.

## Known design tradeoffs — please read before filing

Operator asks for a lot of capability so the bot can be genuinely useful in
a meeting — read files, run shell commands, call MCP tools. The tradeoffs
below are the cost of that capability. They are **intended design**, fully
described in `docs/security.md`, and **not** treated as vulnerabilities.
Listed here explicitly so a report isn't spent rediscovering a tradeoff
we already ship knowingly — and so the scope of the ask is plain.

The mitigating frame, as above: everything these permissions touch stays
on your machine. There is no Operator-side server to exfiltrate to. The
risk surface is local-machine compromise via prompt-injection on a
fully-permissioned local agent, not data leaving your control.

- **The meeting brain runs with all permissions bypassed.** Operator spawns
  its inner-`claude` subprocess with `--dangerously-skip-permissions`,
  unconditionally. It can read/write/delete files, run shell commands, and
  call any registered MCP tool with no approval prompt — and no `permissions`
  config (`allow` / `deny` / `ask`) constrains it. See docs/security.md →
  "Operator runs Claude with all permissions bypassed."
- **Untrusted meeting input reaches that subprocess.** Any participant's
  `@claude` chat message, meeting captions, MCP tool results, and
  foreign-hook feedback are forwarded to — or reachable by — the
  fully-permissioned inner-claude. A prompt-injection payload in any of those
  channels acting on the agent, *including data exfiltration or destructive
  commands*, is documented residual risk R1. Operator deliberately does not
  add an operator-side injection filter or tool-approval gate; the
  mitigations are operational (least-privilege OS account, curated MCP set,
  Meet host controls). A working injection demo against this surface is a
  *demonstration of R1*, not a new finding — but see the caveat below.
- **Any meeting participant can trigger the bot.** There is no per-sender
  allowlist on the `@claude` trigger (residual risk R3).
- **Foreign Stop hooks can redirect the bot mid-meeting** (residual risk R2).
- **The installer widens your Claude Code allow list and registers a
  user-scope MCP server** — four additive `permissions.allow` entries and the
  bundled transcript MCP. Intended, additive, idempotent; documented in
  docs/security.md → "What Operator changes in your `~/.claude` config."

**The caveat:** this list is *not* a blanket disclaimer. If you find a way to
make Operator violate a protection it actually *claims* — see "In scope"
below — that is a real vulnerability and we want to hear about it, even if it
routes through one of the surfaces above.

## Scope

### In scope

- Code in this repository — the `operator` CLI, connectors, pipeline, and the
  bundled transcript MCP server.
- The companion `operator-plugin` repo — the `/operator:*` slash commands and
  the hook scripts.
- `install.sh`, including its `~/.claude/settings.json` allow-list merge and
  user-scope MCP registration.

Concretely, we want to hear about:

- A way to make Operator leak something it claims to protect — e.g.
  `ANTHROPIC_API_KEY` reaching the inner-claude environment despite the
  documented unconditional strip, or chat/caption content leaving the local
  machine.
- The installer's `settings.json` merge behaving destructively,
  non-idempotently, or writing entries beyond the four documented ones.
- A meeting participant making **Operator itself** (as distinct from
  inner-claude) act on attacker-controlled input — e.g. bypassing the
  `@claude` trigger gate, or breaking out of the chat/caption delimiters into
  operator's own control flow.
- File-permission regressions on `~/.operator/` artifacts — the `umask` /
  `chmod` hardening failing to make new files `0o600` / `0o700`.
- The dial singleton lock being bypassable in a way that stacks multiple
  operators on one meeting.
- Anything in `docs/security.md` that is simply *wrong* — a protection the
  doc claims that does not actually hold.

### Out of scope

- The documented design tradeoffs in the section above.
- Issues in upstream dependencies — report those to the dependency owner.
  Operator's own pinned versions are tracked via `pip-audit`.
- Google Meet itself, or Meet's chat / participant / host controls.
- Third-party MCP servers the user has wired into their own `~/.claude`.
- Anything the user's own `~/.claude` configuration causes — Operator
  inherits the user's MCP servers, skills, and hooks and does not police
  them.

## Hardening in place

The current local-artifact hardening is documented in full in
`docs/security.md` → "What's been hardened" (the `umask 0o077` file-mode
discipline, `relativize_home()` path hygiene, and the `ANTHROPIC_API_KEY`
spawn-env strip). That document is the single source of truth; this file
does not duplicate it.
