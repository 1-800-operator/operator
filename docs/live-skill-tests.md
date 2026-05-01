# Live tests — claude agent (track A)

*Phase 14.12.2, session 166. Rewritten for track A: claude IS the meeting LLM (no Anthropic outer / claude-code MCP inner split). The use cases that the bundled operator skills were packaging are still the things we want to test — we just exercise them via claude's native tools (Read, Grep, Glob, LS, Bash, Write, Edit, WebSearch, WebFetch) plus whatever MCPs the user has wired in `~/.claude.json` (Linear, GitHub, Sentry, etc). Each test routes destructive tool calls through meeting chat for explicit user approval (the PreToolUse hook → permission-bridge IPC → chat round-trip we built in steps 1–5c).*

## Use cases under test


| Session | Use case             | Tools claude reaches for | What we're checking                                              |
| ------- | -------------------- | ------------------------ | ---------------------------------------------------------------- |
| 0       | Track-A safety       | n/a                      | subscription auth, hook round-trip, fail-loud                    |
| 1       | codebase walkthrough | Read, Grep, Glob         | claude traces a real subsystem with `file:line` citations        |
| 1       | migration plan       | Read, Grep, Glob         | structured phased plan, 3–7 phases, no code in reply             |
| 1       | test generation      | Read, Write, Bash        | reads sibling tests, writes a real test file, runs it            |
| 1       | live coding edit     | Read, Grep, Edit, Bash   | claude makes a small repo change end-to-end via chat-gated edits |
| 2       | scope estimate       | Linear MCP, Read, Grep   | T-shirt size from a Linear ticket                                |
| 2       | PRD from discussion  | Read (meeting JSONL)     | structured PRD from caption transcript                           |
| 2       | release notes        | GitHub MCP               | grouped changelog from a commit range                            |
| 2       | PR review            | GitHub MCP, Read         | bug + risk callouts citing `file:line`                           |
| 3       | live bug triage      | Sentry MCP, Read         | five-phase triage from a real Sentry issue                       |


The point of session 0 is to gate every other test on the basics — if the hook isn't round-tripping, nothing else in this doc can succeed.

---

## One-time pre-flight (do before session 0)

### A. Verify the build is fresh

```bash
cd /Users/jojo/Desktop/operator
source venv/bin/activate
operator build       # confirm wizard runs cleanly
```

### B. Verify track-A claude config is seeded

If `~/.operator/agents/claude/config.yaml` was deleted (or never existed), the next `operator run claude` will copy the bundled track-A seed into place. Verify with:

```bash
cat ~/.operator/agents/claude/config.yaml | head -25
```

Should show `provider: "claude_cli"` and a `permissions:` block with `auto_approve: [Read, Grep, Glob, LS, WebSearch]` and `always_ask: [Bash, Write, Edit, MultiEdit, NotebookEdit, WebFetch, Task]`. The `mcp_servers: {}` line is correct — track A's MCP toggles are populated lazily by the wizard from `~/.claude.json`.

### C. Verify Claude Code is logged in (subscription auth)

```bash
claude auth status --json | jq '.'
```

Should return `{"authenticated": true, "source": "oauth"}` or similar. Track A explicitly clears `ANTHROPIC_API_KEY` from the spawn env and asserts `apiKeySource == "none"` on the first system-init event — a leaked API key will fail-loud at startup (test 0.4 covers this on purpose).

### D. Verify MCPs that the use cases need are reachable in the user's claude

Track A inherits MCP servers from `~/.claude.json` directly — no per-bot wiring in operator. Verify the ones we lean on:

```bash
claude mcp list
```

You should see at minimum: a Linear server (test 2.1), a GitHub MCP or `gh` CLI access via Bash (tests 2.3, 2.4), and Sentry (test 3.1). Missing servers don't block the doc — just skip the dependent tests.

### E. Check the log file is wired

```bash
ls -la /tmp/operator.log
tail -3 /tmp/operator.log
```

### F. Captions

Captions are on by default for the claude agent. When you join the test Meet, **click the CC button in the Meet UI** to actually start streaming captions — `prd-from-discussion` (test 2.2) only fires if there's a real transcript in the meeting record.

---

## Session 0 — Track-A safety (~10 min)

These six gates check that the track-A architecture itself works before we lean on it for the use cases.

### Test 0.1 — Subscription-auth assertion fires

**Setup:** open a fresh Meet, run `operator run claude` against it. Ensure `ANTHROPIC_API_KEY` is unset (`unset ANTHROPIC_API_KEY` in the shell where you launched operator).

**Trigger phrase (paste into Meet chat):**

> say hi

**Expected behavior:**

- bot replies with a brief greeting
- `/tmp/operator.log` contains `ClaudeCLI subprocess ready: apiKeySource=none, session=<uuid>`

**Pass signal:** the apiKeySource log line is present. Reply itself doesn't matter.

**Say "done" when:** bot has replied OR you've confirmed it crashed.

**What I'll grep:**

```
grep "ClaudeCLI subprocess ready\|apiKeySource\|TIMING claude_cli_turn" /tmp/operator.log | tail -10
```

---

### Test 0.2 — PreToolUse allow round-trip

**Trigger phrase:**

> write a file at /tmp/track_a_test.txt with the contents "hello track A"

**Expected behavior:**

- bot posts a confirmation message: `Run Write?` followed by the `file_path` and `content` arguments and `OK?`
- you reply `yes` (or `ok`)
- bot proceeds; file lands at `/tmp/track_a_test.txt`
- bot posts a brief confirmation reply

**Pass signal:** confirmation prompt arrived BEFORE the file landed; `cat /tmp/track_a_test.txt` shows `hello track A`.

**Say "done" when:** the file is on disk OR the bot reports an error.

**What I'll grep:**

```
grep "PermissionChatHandler\|permission_handler\|PreToolUse" /tmp/operator.log | tail -20
```

---

### Test 0.3 — Auto-approve silent path

**Pre-step:**

```bash
echo "the secret word is pelican" > /tmp/track_a_read_test.txt
```

**Trigger phrase:**

> read /tmp/track_a_read_test.txt and tell me the secret word

**Expected behavior:**

- bot does NOT post a confirmation prompt for the Read
- bot replies with "pelican" (or quotes the line)

**Pass signal:** no `Run Read?` prompt in chat; the answer is correct.

**Say "done" when:** bot has answered.

**What I'll grep:**

```
grep "PermissionChatHandler: auto-approve" /tmp/operator.log | tail -10
```

---

### Test 0.4 — Always-ask deny path

**Trigger phrase:**

> write a file at /tmp/track_a_deny.txt with "this should not appear"

**When the confirmation prompt arrives, reply:**

> no, write it at /tmp/different.txt instead

**Expected behavior:**

- bot sees the deny + reason in the PreToolUse response, adjusts
- bot may post a NEW confirmation: `Run Write?` for `/tmp/different.txt`
- if you approve the second prompt, the file lands at `/tmp/different.txt`, NOT `/tmp/track_a_deny.txt`

**Pass signal:** `/tmp/track_a_deny.txt` does NOT exist after the test; `/tmp/different.txt` does (if you approved the second prompt).

**Say "done" when:** the bot's flow has terminated.

**What I'll grep:**

```
grep "user replied (treated as deny)\|permissionDecision.*deny" /tmp/operator.log | tail -10
```

---

### Test 0.5 — Mid-meeting subprocess crash recovery

**Setup:** while in the same meeting, in a separate terminal:

```bash
pgrep -fl 'claude -p --input-format stream-json' | head -5
# pick the pid that's the operator-spawned subprocess (most recent)
kill <pid>
```

**Trigger phrase (in the meeting after killing):**

> recap our last two messages

**Expected behavior:**

- bot detects the dead subprocess on the next turn
- spawns fresh, re-feeds prior turns via the synthesized opener (probe 7 strategy 2)
- replies coherently, demonstrating it remembers the prior conversation

**Pass signal:** bot's reply references content from earlier turns in this meeting (it actually received the rebuild).

**Say "done" when:** bot has replied.

**What I'll grep:**

```
grep "subprocess died mid-meeting\|attempting one restart\|synthesized opener" /tmp/operator.log | tail -10
```

---

### Test 0.6 — Subscription fail-loud (one-off)

This test intentionally tries to break the bot. Run it from a separate terminal AFTER you're done with 0.1–0.5 in the current meeting (you'll need to leave + restart).

```bash
ANTHROPIC_API_KEY="sk-ant-fake-key-for-test" operator run claude
```

**Expected behavior:**

- operator starts joining a meeting
- on the FIRST user message, `ClaudeCLISubscriptionRequiredError` raises with the message "claude reported apiKeySource='ANTHROPIC_API_KEY'; track A requires subscription auth..."
- bot does NOT silently bill the user's API account

**Pass signal:** the explicit subscription-required error in the log; no LLM reply was generated.

**Say "done" when:** the error has been logged OR (if it doesn't fire) the bot replies normally — that's a fail.

**What I'll grep:**

```
grep "ClaudeCLISubscriptionRequiredError\|apiKeySource" /tmp/operator.log | tail -10
```

After this test, restart operator WITHOUT the bogus key for the rest of the doc.

---

## Session 1 — Engineer flow (~15–20 min)

**Setup:** open a fresh Meet, run `operator run claude` against it. Sit alone; 1-on-1 mode auto-engages.

### Test 1.1 — Codebase walkthrough

**Trigger phrase:**

> walk us through how the chat polling loop works in this codebase

**Expected behavior:**

- bot uses Read / Grep / Glob (silent — auto_approve list)
- traces from entry-point through `pipeline/chat_runner.py:_loop`, citing real `file:line` references
- replies as multiple short messages, one per hop
- ends with "questions?" or an offer to drill deeper

**Pass signal:** every cited `file:line` actually exists in the repo (spot-check 2–3); the trace lands on `pipeline/chat_runner.py:_loop`; replies posted as multiple short messages, not one wall.

**Say "done" when:** the bot has posted the closing message OR you've confirmed it's stuck.

**What I'll grep:**

```
grep "TIMING claude_cli_turn\|stream_event.*content_block_delta\|ChatRunner: new message" /tmp/operator.log | tail -40
```

Plus the JSONL meeting record for the full bot replies.

---

### Test 1.2 — Migration plan

**Trigger phrase:**

> what would it take to migrate this codebase from chat-only to also supporting Slack as a connector? give us a phased plan

**Expected behavior:**

- bot reads enough of the codebase to ground the plan (Read/Grep, silent)
- returns a structured plan: Migration / Surface area / Phases (3–7) / Risks / Sequencing / Open questions
- per phase: What / Files / Blast radius / Depends on / Rough size
- **no code in the output** — the killer rule for this use case

**Pass signal:** at least 3 phases naming actual files (`connectors/base.py`, `connectors/macos_adapter.py`, etc.); per-phase blast radius; no python snippets.

**Say "done" when:** the plan is fully posted (may span 2–3 messages).

**What I'll grep:** same as 1.1, plus a JSONL check that no `kind: chat, sender: claude` entry contains code-block fences.

---

### Test 1.3 — Test generation

**Pre-step (do this NOW before triggering):**

```bash
mkdir -p /tmp/test_fixtures
cat > /tmp/test_fixtures/email_validator.py <<'EOF'
"""Tiny fixture for live test-generation use case — session 166."""


def is_valid_email(addr: str) -> bool:
    """Return True iff `addr` looks like a syntactically valid email.

    Rules:
      - exactly one '@'
      - non-empty local part (before '@')
      - non-empty domain part (after '@')
      - domain contains at least one '.'
      - no whitespace anywhere

    Does NOT validate against a real SMTP server. Pure string check.
    """
    if not isinstance(addr, str) or not addr or any(c.isspace() for c in addr):
        return False
    if addr.count("@") != 1:
        return False
    local, _, domain = addr.partition("@")
    if not local or not domain:
        return False
    return "." in domain
EOF
```

**Trigger phrase:**

> write tests for the is_valid_email function in /tmp/test_fixtures/email_validator.py — happy path plus 4 edge cases. use pytest. then run them.

**Expected behavior:**

- bot reads the fixture (silent — Read in auto_approve)
- proposes a Write for `/tmp/test_fixtures/test_email_validator.py` — confirmation prompt arrives, you approve
- proposes a Bash run (`python -m pytest /tmp/test_fixtures/test_email_validator.py -v` or similar) — confirmation prompt arrives, you approve
- replies with the test file path + cases enumerated + the run output (all green)

**Pass signal:** test file exists with at least 5 cases (happy + 4 edges: empty string, multiple @s, no dot in domain, whitespace, non-string); pytest reports all passing.

**Say "done" when:** the run output is back in chat.

**What I'll grep:**

```
grep "PermissionChatHandler: asking user about 'Write'\|asking user about 'Bash'" /tmp/operator.log | tail -20
```

---

### Test 1.4 — Live coding edit

**Trigger phrase:**

> add a `--version` flag to the operator CLI that prints the package version. repo path is /Users/jojo/Desktop/operator. don't commit, just leave the change in the working tree.

**Expected behavior:**

- bot reads `src/_1_800_operator/__main__.py` and figures out where to add `--version` (silent — Read/Grep)
- proposes one or more Edits (or a Write) — confirmation prompts arrive, you approve each
- optionally proposes a Bash run to verify (`python -m _1_800_operator --version`) — you approve
- replies with the changed file paths + summary of the change + verification output

**Pass signal:** `git diff` in the operator repo shows a real `--version` flag in `src/_1_800_operator/__main__.py`; running `python -m _1_800_operator --version` prints something (usually `0.1.0`); no worktree was created (track A operates directly on the user's tree).

**Say "done" when:** the bot reports done OR you stop it.

**What I'll grep:**

```
grep "PermissionChatHandler: asking user about\|TIMING claude_cli_turn" /tmp/operator.log | tail -50
```

You can leave the meeting after this test. **Revert the `--version` change** with `git checkout -- src/_1_800_operator/__main__.py` after the session.

---

## Session 2 — PM/sprint flow (~15 min)

**Setup:** fresh Meet, `operator run claude`, captions ON (CC button).

### Test 2.0 — Pre-step: create a fixture Linear ticket

In your Linear workspace, create a new issue with this exact content (you'll point the bot at it in test 2.1):

- **Title:** Add `operator list-skills` subcommand to print enabled skills for the current bot
- **Description:**
  > Today users have to `operator edit <bot>` and read the YAML to know which skills are wired in. A `operator list-skills <bot>` subcommand should print the enabled skill names + which external paths are mounted, so users can confirm without opening the file.
  >
  > Acceptance: `operator list-skills claude` prints (1) bundled-library skills currently `enabled: true`, (2) `external_paths` with skill counts per path, (3) any locked-by-MCP entries from session-159 work.
- Don't actually implement it. The ticket just needs to exist.

Copy the ticket URL.

### Test 2.1 — Scope estimate

**Trigger phrase (replace `<linear-url>`):**

> scope this ticket: 

**Expected behavior:**

- bot calls a Linear MCP tool to fetch the ticket (the MCP itself may prompt for confirmation — depends on whether the tool is in claude's own native settings or comes through MCP. Treat any MCP confirmation as auto-track-A behavior, approve.)
- optionally peeks at affected files via Read/Grep (silent)
- replies: Size (XS/S/M/L/XL) + Files to touch + Blockers + Risks + Unknowns
- no code in the reply

**Pass signal:** size assigned (S or M is realistic); "Files to touch" names `src/_1_800_operator/__main__.py` + `src/_1_800_operator/pipeline/skills.py` or `config.py` (real files); at least one risk/unknown surfaced.

**Say "done" when:** estimate is posted.

**What I'll grep:**

```
grep "PermissionChatHandler: asking user about\|stream_event\|TIMING claude_cli_turn" /tmp/operator.log | tail -25
```

---

### Test 2.2 — PRD from discussion

**Pre-step:** captions still streaming (CC on). Speak out loud (microphone on) for ~90 seconds about this imaginary feature — speak naturally, with pauses:

> "OK so we want to add a stealth mode to operator. The idea is when stealth is on, the bot doesn't post an intro message when it joins, it doesn't post the failure banner, and it doesn't show its name in the participant list. The use case is sales calls where the user doesn't want the prospect to know an AI is in the room. Open question: should stealth also disable captions ingestion to be safe? And should we let users turn it on per-meeting via a CLI flag, or per-bot in config? I'm leaning per-meeting — `operator run claude --stealth <url>` — so it's an explicit decision each time. Goal would be ship a v1 of this within two weeks."

You can paraphrase, but cover: stealth concept, the three behaviors it disables, the use case, the open question, the goal.

**Trigger phrase (after speaking):**

> turn the last 90 seconds of discussion into a PRD. read the meeting record at ~/.operator/history/

**Expected behavior:**

- bot uses Read on the meeting JSONL (silent — Read auto-approved)
- emits structured PRD: Problem / User / Goal / Scope / Non-goals / Open questions / Owner
- quotes specific phrasings from your speech ("ship a v1 within two weeks", "per-meeting via a CLI flag", etc.)
- writes "Owner — not stated in discussion" if you didn't name one

**Pass signal:** PRD has at least 5 of the 7 sections; quotes are real phrases from your speech (not invented); open question about captions-ingestion-when-stealth is captured.

**Say "done" when:** PRD is posted.

**What I'll grep:**

```
grep "Read.*history\|MeetingRecord\|caption" /tmp/operator.log | tail -20
```

Plus the JSONL — I'll check the bot's PRD reply has section headers and quotes real caption text.

---

### Test 2.3 — Release notes

**Trigger phrase:**

> give us release notes for the last 10 commits on main, repo is github.com/shapirojojo/operator

**Expected behavior:**

- bot uses GitHub MCP (or `gh` via Bash — confirm if Bash) to fetch commits + PRs
- groups output: Features / Fixes / Changes / Internal
- each line cites `(#<PR-number>)` or commit SHA short prefix
- breaking-change callout pinned at top if any (probably none recent — say "no breaking changes")

**Pass signal:** at least 4–5 commits grouped by intent; PR numbers visible if they exist; recent session-165/166 work shows up (track-A pivot, claude_cli provider).

**Say "done" when:** notes are posted.

**What I'll grep:**

```
grep "stream_event\|asking user about 'Bash'\|asking user about 'WebFetch'" /tmp/operator.log | tail -25
```

---

### Test 2.4 — PR review

**Pre-step:** find a recent merged PR in `shapirojojo/operator` to point at, or open a small one if needed.

**Trigger phrase (replace `<pr-url>`):**

> review this PR: 

**Expected behavior:**

- bot uses GitHub MCP / Bash / WebFetch to read the PR + changed files
- replies with: Summary + Bugs + Missing tests + Risks + Style + Questions sections
- every Bugs/Risks claim cites `file:line`
- doesn't approve or request changes (review-only)

**Pass signal:** every cited `file:line` exists in the diff; bugs section is either empty (with explicit "no bugs") OR contains real concerns; nits skipped.

**Say "done" when:** review is posted.

**What I'll grep:**

```
grep "asking user about\|TIMING claude_cli_turn" /tmp/operator.log | tail -25
```

You can leave the meeting after this test.

---

## Session 3 — Live ops (live bug triage, ~25 min including setup)

This one has the highest setup cost. ~15 min of prep before the meeting starts.

### Test 3.0 — Pre-step: Sentry account + project + DSN + fixture trigger

**Step 1 — Sentry account:** sign up at [sentry.io](https://sentry.io) with `shapirojojo@gmail.com` if you haven't.

**Step 2 — Create a project:**

- Project type: Python
- Project name: `operator-track-a-test`
- Copy the DSN.

**Step 3 — Build the fixture trigger script:**

```bash
mkdir -p /tmp/sentry_demo
cd /tmp/sentry_demo
python -m venv venv
source venv/bin/activate
pip install sentry-sdk

cat > trigger_error.py <<'EOF'
"""Tiny fixture to populate Sentry with a real error — session 166 live test."""
import sentry_sdk
import os

DSN = os.environ.get("SENTRY_DSN")
if not DSN:
    raise SystemExit("set SENTRY_DSN env var first")

sentry_sdk.init(dsn=DSN, traces_sample_rate=0)


def parse_user_age(payload: dict) -> int:
    """Look up nested age. Buggy — assumes profile always present."""
    return int(payload["profile"]["age"])


def main():
    parse_user_age({"profile": {"age": "29"}})
    bad_payloads = [
        {"profile": {"age": "31"}},
        {"name": "alice"},
        {"profile": {"name": "bob"}},
        {"profile": {"age": "thirty"}},
    ]
    for p in bad_payloads:
        try:
            parse_user_age(p)
        except Exception as e:
            sentry_sdk.capture_exception(e)
            print(f"captured: {type(e).__name__}: {e}")
    sentry_sdk.flush(timeout=5)
    print("done — check Sentry in ~30 sec")


if __name__ == "__main__":
    main()
EOF
```

**Step 4 — Run the trigger:**

```bash
export SENTRY_DSN="<paste-dsn>"
python trigger_error.py
```

**Step 5 — Wait 30 sec, copy the issue URL** from `https://<your-org>.sentry.io/issues/`.

**Step 6 — Verify Sentry MCP is in claude's setup:**

```bash
claude mcp list | grep -i sentry
```

If it's missing, add it via Claude Code's mcp commands or the claude.ai connectors UI. Track A inherits whatever's there.

### Test 3.1 — Live bug triage

**Setup:** fresh Meet, bot joined, captions optional.

**Trigger phrase (replace `<sentry-issue-url>`):**

> prod is broken — what's going on with this Sentry issue: 

**Expected behavior:**

- bot calls the Sentry MCP to fetch the issue
- optionally Reads `/tmp/sentry_demo/trigger_error.py` to ground the cause analysis (silent — Read)
- emits five-phase triage as **separate messages**, one per phase:
  1. What's happening — symptom + event count + first/last seen + release
  2. Where it's breaking — top stack frame at `file:line` + Sentry issue URL
  3. Probable cause — one or two sentences, flagged "guess" vs "confirmed"
  4. Proposed fix — what to change + where (name the file)
  5. Want me to write the patch? — "y/n" — claude offers to make the edit (chat-gated Write/Edit)

**Pass signal:** five separate messages (not one combined wall); phase 2 cites `trigger_error.py:parse_user_age` (or your specific line); phase 3 doesn't claim certainty without evidence; phase 5 is an explicit ask, not a unilateral edit.

**Say "done" when:** all five phases are posted.

**What I'll grep:**

```
grep "asking user about\|stream_event\|TIMING claude_cli_turn" /tmp/operator.log | tail -40
```

If you optionally want to test phase 5, reply `y` — claude will propose an Edit, chat-gated. Don't go that far unless you want extra coverage; the use-case check is the five-phase shape.

---

## Per-test cadence (how we actually run this)

For each test:

1. I post the test number ("kicking off test 0.1 — subscription assertion").
2. You open the Meet (or use the open one), do any pre-step the test calls for, type the trigger phrase verbatim into Meet chat, approve confirmation prompts as the test instructs.
3. You watch the bot's reply.
4. When the reply is complete (or has errored), you tell me **"done"**.
5. I run the per-test grep against `/tmp/operator.log` + read the relevant slice of `~/.operator/history/<slug>.jsonl`.
6. I post a summary: did the right tools fire, did the chat round-trip behave, did the reply match the pass signal, any errors, any latency anomalies.
7. We move to the next test.

If a test fails, I'll flag it in-line and we decide whether to retry, skip, or stop the session to debug.

## What I'll be looking for in the logs

- **Subscription auth:** `ClaudeCLI subprocess ready: apiKeySource=none, session=<uuid>`
- **PreToolUse round-trip:** `PermissionChatHandler: asking user about '<Tool>'` and `PermissionChatHandler: reply received: '<text>'`
- **Auto-approve hits:** `PermissionChatHandler: auto-approve '<Tool>'`
- **Restart recovery:** `subprocess died mid-meeting, restarting` + `attempting one restart`
- **Per-turn latency:** `TIMING claude_cli_turn=<sec>s ttft=<sec>s first_flush=<sec>s` — flag anything over 30s without a heartbeat
- **Errors:** `ClaudeCLISubscriptionRequiredError`, `ClaudeCLIProtocolError`, `permission bridge: ...`
- **Bot's full reply:** the JSONL meeting record entry with `kind: chat` and `sender: claude`

## Backup logs to grep manually

```bash
# Live tail of the operator log
tail -f /tmp/operator.log

# Bot's own replies in this meeting (replace <slug>):
ls -t ~/.operator/history/ | head -3
tail -f ~/.operator/history/<slug>.jsonl | jq 'select(.kind == "chat" and .sender == "claude")'
```

