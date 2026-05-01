# Deferred live-smoke tests

Running log of behaviors that unit tests can't cover because they require a
real browser join, a real MCP server, or a real LLM provider roundtrip.
Work through this list during **Phase 14.8 clean-mac smoke** and **15.1
Linux smoke**, or whenever doing an end-to-end meeting run for any reason.

**Append, don't replace.** Each entry: phase/session that introduced it,
what to verify, what to input, and the pass criterion. Mark ✅ with the
verifying session once the test has been run successfully and the result
matches — a failure gets a 🔄 and a note of what actually happened.

---

## Open

### Phase 15.7.2 — MCP startup-failure banner (session 148)

- **Setup:** Run a bot with a deliberately-broken MCP. Easiest today:
  unset `FIGMA_TOKEN` before launching `operator designer`, or unset
  `GITHUB_TOKEN` before `operator engineer`.
- **Verify:** One compact banner posts in chat on join, *before* the
  bot's LLM-generated intro: `Heads-up — figma didn't load (missing
  FIGMA_TOKEN). Ask for details.`
- **Pass criteria:**
  - Exactly one banner, no duplicates as the chat loop iterates.
  - When the user then asks "why did figma fail?", the LLM's reply
    references the specific kind + fix from the system prompt block
    (not a generic "check the logs").
  - When all MCPs load clean, no banner posts.
- **Why deferred:** Requires a live Meet join; can't be exercised
  without the Chrome profile + LLM provider.

### Phase 15.7.3 — OAuth cache-path fail-fast + `operator auth` (session 148)

- **Setup:** Move the Linear token cache aside:
  ```
  mv ~/.mcp-auth/mcp-remote-*/fcc436b0d1e0a1ed9a2b15bbd638eb13_tokens.json /tmp/
  ```
  (Filename is `md5("https://mcp.linear.app/mcp")_tokens.json`.) Then
  launch `operator pm` and join a Meet.
- **Verify:**
  1. Bot joins in normal time — **no 30+s hang** waiting for OAuth.
  2. On-join banner fires: `Heads-up — linear didn't load (needs auth — run operator auth linear). Ask for details.`
  3. When prompted in chat ("why did linear fail?"), LLM reply relays
     the specific fix (run the auth command) rather than "check logs."
  4. Exit the bot.
- **Re-authorize step:** Run `operator auth linear`. Browser should
  open for Linear OAuth. Approve. CLI should print
  `✓ Token cached at ~/.mcp-auth/mcp-remote-<ver>/<hash>_tokens.json`
  and exit 0 within seconds of approval.
- **Post-test:** Launch `operator pm` again — no banner, Linear loads,
  Linear tool calls work.
- **Why deferred:** Needs a real Linear OAuth roundtrip, a browser, and
  a live Meet join.

### Phase 15.7.4 — wizard readiness status screen + inline OAuth (session 149)

- **Setup:** Run `operator build`. Pick a preset (or "custom"), then at
  step 2 (Tools) toggle a mix: enable `claude-code` + `linear` + one
  env-auth MCP whose token is missing from `.env` (e.g. notion with
  `NOTION_API_KEY` unset).
- **Verify:**
  1. After the MCP picker closes, a `Readiness:` block renders with
     one line per enabled MCP. Expected shape:
     - `✓ github` (when `GITHUB_TOKEN` set)
     - `⚠ linear — run \`operator auth linear\` once to authorize (...)` if cache absent; else `✓ linear`
     - `✗ notion — set NOTION_API_KEY in .env (https://www.notion.so/profile/integrations)`
     - `✓ claude-code` (when binary + login ok) or `✗ claude-code — not logged in — run \`claude auth login\``
  2. Below the table, a dim one-liner reminding the user that
     `claude-code` delegations need a git-initialized repo path (only
     when claude-code is in the report).
  3. If any `⚠` oauth_needed, an inline `Authorize <name> now? [Y/n]`
     prompt fires; answering `y` hands the terminal to mcp-remote,
     browser opens, approval seeds the token cache, wizard re-renders
     readiness with ✓ and moves on.
  4. Declining inline auth (`n`) keeps the ⚠ and proceeds to the next
     step without hanging.
  5. `Press Enter to continue` pauses before step 3 so the user can
     read the status screen.
- **Graceful degradation to verify:**
  - Temporarily rename `git` (`sudo mv /usr/bin/git /usr/bin/git.bak`),
    re-enable claude-code — row should read `✗ claude-code — git CLI
    not on PATH — install git first (https://docs.claude.com/...)`.
    Restore after.
  - With `claude` binary present but logged out (`claude auth logout`),
    row should read `✗ claude-code — not logged in — run \`claude auth
    login\``.
- **Post-test:** Finish the wizard, launch the bot, confirm runtime
  still works end-to-end (the 15.7.4.5 runtime pre-flight will make
  this tighter in the next phase).
- **Why deferred:** The wizard is interactive TUI — can't unit-test the
  rich/Prompt rendering without a real terminal; the inline OAuth arm
  needs a live browser popup.

### Phase 15.7.4 — claude-code git-init graceful failure (session 149)

- **Setup:** Launch `operator engineer` (or any bot with claude-code
  enabled) into a Meet. In chat, ask the bot to delegate a task against
  a folder that is NOT a git repo — e.g. `/tmp/not-a-repo` (create it
  first: `mkdir /tmp/not-a-repo`).
- **Verify:** The bot's chat reply should relay (paraphrased) that the
  path isn't a git repo, include the path, and instruct the user to
  run `git init` in that folder or point at a folder that's already a
  git repo. No stack trace, no generic "tool failed" message.
- **Why deferred:** Needs a live Meet + claude-code subprocess.

### Phase 15.7.4.5 — runtime MCP pre-flight (session 149)

- **Setup A (happy path):** With all enabled MCPs ready (GitHub token
  set, Linear OAuth cache fresh), run `operator pm`. Expected: no
  pre-flight output at all — the command proceeds straight to
  "Launching Chrome…".
- **Setup B (OAuth missing):** Move the Linear token aside
  (`mv ~/.mcp-auth/mcp-remote-*/fcc436b0d1e0a1ed9a2b15bbd638eb13_tokens.json /tmp/`)
  and run `operator pm`. Expected:
  1. Pre-flight block prints `⚠ linear — run \`operator auth linear\` once to authorize (...)`.
  2. Prompt: `linear: authorize now? (browser popup; runs \`operator auth linear\`) [y/N]`.
  3. Pressing Enter (default N) → "continuing" → bot boots with linear runtime-disabled + 15.7.3 mid-meeting banner fires as usual.
  4. Alternative: answer `y` → mcp-remote takes over terminal, browser
     opens, approval seeds the cache, pre-flight prints `✓ linear authorized`,
     then bot boots with Linear enabled.
- **Setup C (missing env var):** Temporarily unset `GITHUB_TOKEN` and
  run `operator engineer`. Expected:
  1. `✗ github — set GITHUB_TOKEN in .env (https://github.com/settings/tokens)`.
  2. Prompt: `github: continue without it? [Y/n]`.
  3. Default Y → bot boots with github runtime-disabled.
  4. `n` → prints `Aborting. Set GITHUB_TOKEN in .env and re-run.` and
     exits with code 2 (no browser launched).
- **Setup D (--no-preflight):** With broken state from Setup B or C
  still in place, run `operator pm --no-preflight`. Expected: zero
  pre-flight output, bot boots immediately and the 15.7.3 banner (or
  15.7.1 missing_env surfacing) handles it mid-meeting.
- **Why deferred:** Needs a real terminal for interactive prompts +
  a real OAuth browser round-trip for the y-path of Setup B.

### Phase 15.7.5 — new-MCP scaffolding (session 149)

Five new MCP blocks (calendar, playwright, salesforce, sentry, slack) were
added as scaffolds to all three bundled agents. Each block declares
`auth`, `credentials_url`, `env`, `read_tools`, `confirm_tools`, and
`hints`, but none have been booted against a real server yet. Pressure-
test matrix — one run per MCP to confirm tools appear in
`get_openai_tools()` and one read tool executes cleanly end-to-end.

- **calendar (`@cocal/google-calendar-mcp`):**
  - Setup: download `credentials.json` from Google Cloud Console; set
    `GOOGLE_OAUTH_CREDENTIALS=/abs/path/to/credentials.json` in `.env`;
    enable on PM via `operator build`.
  - Verify: first run pops OAuth consent page in browser; `list-calendars`
    returns at least the user's primary calendar; `list-events` with a
    1-week window returns real events.
  - **Pin the `@latest` version** in `agents/*/config.yaml` once a known-
    working version is identified.

- **playwright (`@playwright/mcp@0.0.70`):**
  - Setup: enable on any agent; no env vars needed.
  - Verify: `browser_snapshot` of `https://example.com` returns structured
    DOM; persistent profile dir appears at
    `~/Library/Caches/ms-playwright/mcp-*-profile`; `browser_navigate`
    confirm-gates in chat.

- **salesforce (`@salesforce/mcp`):**
  - Setup: `brew install salesforce/cli/sf` (or npm `sf`); `sf org login
    web`; enable on any agent.
  - Verify: `list_all_orgs` returns the logged-in org; `run_soql_query`
    with `SELECT Id, Name FROM Account LIMIT 3` returns rows.
  - **Flag:** the wizard's env-var check reports `✓` because `env: {}`,
    but the real auth lives in sf's local cache. If `sf` isn't logged in,
    tools still spawn but calls fail. If this is an ongoing UX gap, add
    a prereq probe analogous to claude-code's (15.7.4) in a follow-up.
  - **Pin the `@latest` version** in `agents/*/config.yaml` once a known-
    working version is identified.

- **sentry (hosted remote MCP via `mcp-remote@0.1.38`):**
  - Setup: enable on Engineer (already default-enabled); run `operator
    auth sentry` (same code path as `operator auth linear`); approve
    in browser.
  - Verify: token cache appears at
    `~/.mcp-auth/mcp-remote-*/{md5("https://mcp.sentry.dev/mcp")}_tokens.json`;
    `find_organizations` returns at least one org; `search_issues` with
    an `is:unresolved` filter returns real issues.

- **slack (`@modelcontextprotocol/server-slack`):**
  - Setup: create a Slack app at https://api.slack.com/apps, install to
    workspace, copy Bot User OAuth Token into `.env` as
    `SLACK_BOT_TOKEN=xoxb-...`; set `SLACK_TEAM_ID=T0...`; enable on
    any agent.
  - Verify: `slack_list_channels` returns real channels (not empty);
    `slack_get_channel_history` on a known channel returns the latest
    messages.
  - **Flag:** upstream reference server was archived May 2025. If it
    breaks, swap to `@zencoderai/slack-mcp-server` (drop-in, same env
    vars and tool names) in `agents/*/config.yaml`.
  - **Pin the `@latest` version** in `agents/*/config.yaml` once a known-
    working version is identified.

- **Per-MCP blanket pass criterion:** on boot the bot's startup log
  lists the server (`MCPClient connected <name>`); tools appear in the
  first LLM turn's tool schema; at least one read tool returns a real
  payload; no `startup_failures` entry for the server in the runtime
  status. If auth flakes out later, verify 15.7.1's error classifier
  trips the server into `runtime_failures` with the right `kind`.
- **Why deferred:** Each requires real credentials, network roundtrips,
  and in some cases a browser OAuth dance.

### Phase 15.9 — `claude` bundled agent (session 151)

- **Wizard preset live pick:**
  - Run `operator build`, pick `claude` from the fighter-select gallery
    on a machine where Claude Code is installed + logged in.
  - Verify: step 1 shows the agent, selection proceeds (not blocked),
    step 2 shows auto-imported MCPs pre-ticked in the picker (expect
    `claude-ai-*` entries matching whatever `claude mcp list` returns),
    step 3 surfaces the "Auto-imported from ~/.claude/skills/:" line
    with real skill names, step 4 offers the CLAUDE.md-append prompt
    if `~/.claude/CLAUDE.md` exists.
  - Env placeholder verify: after step 2 completes, check
    `~/.operator/.env` has commented `# VAR=` lines for any env vars
    the imported MCPs reference (mostly a no-op — hosted mcp-remote
    imports reference none).

- **CLI first-run with real `~/.claude.json`:**
  - Delete `~/.operator/agents/claude/` (or fresh install) and run
    `operator run claude`. Expect: stderr prints
    `[claude] auto-imported N MCP(s): ...` on first run, followed by
    normal boot. Re-run `operator run claude` — expect silent boot
    (marker `_claude_import_done: true` short-circuits the re-import).
  - Verify config write: `~/.operator/agents/claude/config.yaml` now
    has both the bundled `claude-code` block AND the imported hosted
    MCP blocks; marker present.
  - Known limitation: PyYAML round-trip drops comments from config.yaml
    on first write. Confirm the file still parses and the agent boots.

- **Hard-fail when `claude` CLI uninstalled:**
  - Temporarily rename `$(which claude)` or run on a machine without
    Claude Code. Run `operator run claude`. Expect: exit code 2,
    stderr block starting "The `claude` agent requires the Claude Code
    CLI." with install link. No browser launch, no config write.
  - Cross-check: `operator run pm` on the same machine boots normally
    (hard-fail is scoped to `claude` agent only).

- **`claude mcp list` format drift canary:**
  - If a future Claude Code release changes `claude mcp list` output
    shape, the regex parser in `claude_code_import.py` silently returns
    zero hosted MCPs. Symptom: wizard / first-run imports 0 hosted MCPs
    even when `claude mcp list` in a terminal shows them. Check
    `_CLAUDE_MCP_LIST_RE` against the new format and update.

- **Why deferred:** Live tests require a real Claude Code install +
  login + account-level connector setup (claude.ai Gmail/Drive/etc.),
  which varies per user. Unit tests (32/32 in `test_claude_code_import.py`)
  cover every code path via mocked subprocess + tempdir fixtures.

## Closed

*(none yet)*
