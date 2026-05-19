# Launch Audit Plan

Five focused audits to run before flipping the repo public, scoped to the
concerns that matter most for an OSS local-CLI tool that drives a paid
LLM, reads meeting content, and ships to user laptops.

Each audit runs **one component at a time** — pick an audit, pick a
component, run a fresh session against it. Findings get appended to
`docs/launch-audit-findings.md` (create on first finding) with a header
of the form `## <audit> · <component>`.

> Not the same as `docs/pre-launch-audit.md`. That doc applied a 4-lens
> sweep (security / edge case / PR review / AI slop) per-component and
> is largely closed out for the Tier 1 hot path. This plan is the
> launch-gate pass: narrower, OSS-public-facing, 5 specific lenses.

---

## The 5 audits

### Audit 1 — Security

OSS launch raises the stakes: anyone can read the code, anyone can file
a CVE, and the user trusts operator with their meeting audio, chat
history, and a CDP-attached Chrome that holds their Google session.

**In scope:**

- **Prompt injection** — meeting chat + captions + participant names
  flow into the inner-claude turn. Any of these can be hostile. Look
  for: unfiltered passthrough into the LLM, places where a participant
  could spoof an instruction that gets executed as a tool call, the
  `@claude` trigger being abuseable for unintended dispatch.
- **OAuth / session handling** — the dial Chrome profile carries the
  user's Google session cookies indefinitely. Profile dir perms,
  exposure on shared hosts, what gets written to disk, whether anything
  leaks the cookie store via logs or debug dumps.
- **CDP attack surface** — operator opens a debug-port Chrome. Anyone
  with localhost access can speak CDP to it. Look for port binding,
  authentication, lifetime, and whether we ever bind anything beyond
  127.0.0.1.
- **Subprocess & shell** — every `subprocess.Popen`, every
  `page.evaluate(...)`, every spawn of `claude` / Swift helper /
  install scripts. Look for string interpolation of untrusted input
  into command lines or JS payloads.
- **TCC / disclaiming** — `_disclaimed_spawn` is exactly the kind of
  surface a reviewer will scrutinize. Is the disclaim scope correct?
  Can a hostile payload abuse it to escape the bundle's identity?
- **Path traversal** — meeting slugs, profile names, anything that
  becomes a filesystem path. Confirm sanitization.
- **Hooks** — the `PreToolUse` hook receives tool input from
  inner-claude (untrusted). Confirm we don't `eval` it, log it raw
  beyond what's needed, or let it influence the classifier prompt in
  injection-friendly ways.

**Out of scope:** anything requiring an attacker who already has shell
access to the user's machine. Operator is a local CLI; threat model
stops at "someone else can read/influence operator's I/O".

---

### Audit 2 — Edge cases

What did we ship fast and not exercise in QA?

**In scope (the "likely-enough-to-matter" filter):**

- Concurrency / threading races (multiple ticks in flight,
  shutdown-vs-send-queue, observer install vs first DOM mutation).
- State-machine gaps (what if `leave()` runs before `join()` returns?
  what if Chrome dies mid-tick? what if claude PTY EOF's mid-reply?).
- Failure paths that throw but get swallowed silently (look for `except
  Exception: pass` and `log.debug` of caught errors — separate from
  audit 1, this is about *behavior* not *security*).
- "Two participants doing the same thing simultaneously" — chat send,
  permreq answer, hangup.
- Empty / huge / unicode-mangled inputs to anything user-facing
  (participant names, chat messages, captions).
- Resource cleanup on the unhappy path — fd leaks, tempdirs,
  subprocesses, PTY masters.

**Out of scope:** disk-full, network-down, kernel panics, anything
requiring deliberately bad-faith user behavior on their own machine.

---

### Audit 3 — Hardcoded ceilings, timeouts, magic numbers

Goal: produce a single consolidated table so the user knows every
load-bearing constant in the system and where it lives.

**In scope:**

- Every `time.sleep`, `asyncio.wait_for`, `timeout=`, `wait_seconds=`.
- Every cap: `MAX_TOKENS`, history length cap, byte ceiling on MCP
  responses, deque maxlen, queue maxsize.
- Every interval: poll period, debounce, grace period, heartbeat
  cadence.
- Every retry count / backoff.
- Every hardcoded port, path, slug pattern.

**Output format:** one row per constant — `name | value | location |
what it bounds | why this value`. Mark which are in `config.py`
(centralized, easy to tune) vs scattered.

---

### Audit 4 — Hook conversion opportunities

We pivoted to using Claude Code hooks (`PreToolUse`) for the
permission-bridge flow and it landed clean. Question: is any pre-pivot
code doing things that would be **materially** better as a hook?

**In scope:**

- Polling loops that watch transcript / log files where a `Stop` or
  `SessionEnd` hook would replace the poll.
- Anything where operator wraps the claude PTY to observe events —
  could a `PostToolUse` / `Stop` hook give cleaner signal?
- Trigger-narration / progress narration paths (currently
  claude-self-narrated via the briefing) — would a hook give us
  structured `tool_use_id` correlation we're currently inferring?

**Out of scope:** anywhere we'd only shave a few lines, anything
outside the inner-claude tool loop (install, MCP, audio, doctor —
hooks don't apply), anywhere conversion would *re-couple* us to
inner-claude internals we deliberately decoupled.

**Output:** short list — *only* the materially better swaps.

---

### Audit 5 — Hardcoded secrets / credentials

Verify nothing committed to the repo is secret, including in git
history.

**In scope:**

- Current tree: API keys, OAuth client secrets, tokens, signed cert
  fingerprints that should be private, anything in `.env`-shaped
  files, anything in tests/fixtures that looks like a real key.
- Git history (`git log -p`): same scan, but historical — if a key
  was ever committed and later removed, it's still in history.
- Dial profile / cookie material — confirm `~/.operator/` paths
  are git-ignored and nothing's snuck in.
- Build artifacts / signed helper — confirm we don't ship Apple
  signing material in the repo.
- CI / workflow files — secrets pulled from env (good) vs inlined
  (bad).

**Output:** clean / findings list. Any finding here is launch-blocking.

---

## Component breakdown

Eight components. Pick one per session.

| # | Component | Files |
|---|---|---|
| 1 | **CLI entry & lifecycle** | `src/_1_800_operator/__main__.py`, `src/_1_800_operator/config.py`, `dial.pid` handling, shutdown teardown |
| 2 | **Dial Chrome connector** | `src/_1_800_operator/connectors/attach_adapter.py`, `connectors/session.py`, `connectors/chat_dom_js.py`, `connectors/base.py` |
| 3 | **Chat runner & trigger logic** | `src/_1_800_operator/pipeline/chat_runner.py`, `pipeline/classifier.py`, `pipeline/confirmation.py` |
| 4 | **LLM provider & PTY** | `src/_1_800_operator/pipeline/llm.py`, `pipeline/providers/claude_cli.py`, `pipeline/providers/base.py`, `pipeline/_disclaimed_spawn.py`, `bridges/claude.py` |
| 5 | **Audio pipeline** | `src/_1_800_operator/pipeline/audio.py`, `pipeline/aec_cleaner.py`, `pipeline/transcript.py`, `src/_1_800_operator/swift/` (Swift helper interface only — Swift code itself audited as part of the install component) |
| 6 | **Meeting record & bundled MCP** | `src/_1_800_operator/pipeline/meeting_record.py`, `src/_1_800_operator/mcp_servers/record_server.py` |
| 7 | **Hooks** | `PreToolUse` permission-bridge hook script + any other hook scripts under `operator-plugin/` |
| 8 | **Install / packaging / setup** | `install.sh`, `scripts/build_signed_helper.sh`, Swift bundle source under `src/_1_800_operator/swift/`, `pipeline/doctor.py`, `pipeline/update_check.py`, plugin marketplace files |

**Cross-cutting notes:**

- Audit 3 (hardcoded numbers) and Audit 5 (secrets) are inherently
  cross-cutting but still worth walking component-by-component for
  consistency. Each session appends to its audit's section; at the
  end you'll have a consolidated table.
- Audit 4 (hook conversion) only applies to components 1–4. Skip for
  5–8.

---

## Suggested order

Audit-first, starting with security:

1. Audit 1 (security) across components 1 → 8
2. Audit 5 (secrets) across components 1 → 8 — fast, mostly grep-based
3. Audit 2 (edge cases) across components 1 → 6
4. Audit 3 (hardcoded numbers) across components 1 → 8 — table output
5. Audit 4 (hook opportunities) across components 1 → 4 — short list

Rationale: security and secrets are highest-stakes and benefit from
fresh-eyes momentum; numbers and hook-opportunities are smaller and
can land later.

---

## How to run an async session

Paste this prompt into a fresh session:

> Run **Audit \<N> (\<name>)** from `docs/launch-audit-plan.md` on
> **Component \<M> (\<name>)**. Use the audit's "In scope" list as
> your checklist. Append findings to `docs/launch-audit-findings.md`
> under a `## Audit \<N> · Component \<M>` header. If clean, say so
> under that header — don't skip. Don't fix anything; this is a
> findings-only pass.

The findings file gets one section per audit×component cell. Fixes
happen in a follow-up session per finding, not during the audit.

---

## Status matrix

Check off as cells complete. `—` = N/A (audit doesn't apply to that
component).

| Component | A1 sec | A2 edge | A3 nums | A4 hooks | A5 secrets |
|---|:---:|:---:|:---:|:---:|:---:|
| 1. CLI entry & lifecycle       | ☐ | ☐ | ☐ | ☐ | ☐ |
| 2. Dial Chrome connector       | ☐ | ☐ | ☐ | ☐ | ☐ |
| 3. Chat runner & trigger logic | ☐ | ☐ | ☐ | ☐ | ☐ |
| 4. LLM provider & PTY          | ☐ | ☐ | ☐ | ☐ | ☐ |
| 5. Audio pipeline              | ☐ | ☐ | ☐ | — | ☐ |
| 6. Meeting record & MCP        | ☐ | ☐ | ☐ | — | ☐ |
| 7. Hooks                       | ☐ | — | ☐ | — | ☐ |
| 8. Install / packaging         | ☐ | — | ☐ | — | ☐ |
