# Security — threat model and residual risks

This document describes Brainchild's threat model, the mitigations already
shipped, and the risks that remain documented rather than fixed. It is the
single source of truth the README and `.gitignore` cross-link to.

Reporting contact and SLA live in `SECURITY.md` at the repo root.

## What Brainchild is

A chat-based AI meeting participant. It joins Google Meet with a persistent
Chrome profile, reads the chat panel, and replies via an LLM with tool access
(Linear, GitHub, and user-supplied MCP servers). Everything runs locally on
the brainchild's machine; there is no Brainchild-side server.

## Trust boundaries

In rough order of how trusted each input is:

| Input | Trust | Why |
|---|---|---|
| Agent config (`agents/<name>/config.yaml`) | **Trusted** | Author's own file. MCP commands, ground-rules, skills live here. |
| `.env` at repo root | **Trusted** | Local file the author controls. |
| User-installed MCP server binaries | **Semi-trusted** | Subprocesses the author chose to wire up. Their outputs are untrusted. |
| LLM responses | **Semi-trusted** | Constrained by prompt + tool confirmation, but can be steered by injected content from less-trusted inputs. |
| Google Meet chat messages | **Untrusted** | Any meeting participant can send. |
| Google Meet captions | **Untrusted** | Any speaker's words land here. |
| MCP tool results | **Untrusted** | A compromised or adversarial MCP server could return instructions masquerading as data. |
| Participant display names | **Untrusted** | Attacker-controlled, flows into the prompt as an attribute value. |

## Never commit these

The same list appears in `README.md` and `.gitignore`; this section is the
canonical one. If you see any of these in `git status` untracked, something
is wrong — **do not `git add .` blindly**.

- `.env` — API keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GITHUB_TOKEN`, …)
- `credentials.json` — Google OAuth client secrets
- `token.json` — Google OAuth access/refresh tokens
- `auth_state.json` — Playwright storage state (Google session cookies)
- `browser_profile/` — persistent Chrome profile (Google session cookies)

All of the above are in `.gitignore` as of session 137. The directory
`browser_profile/` is also `chmod 0o700` on both macOS and Linux adapters so
other users on a shared host can't list its contents.

## What's been hardened

The pre-launch hardening pass (Phase 15.6.1, April 2026) audited the six
trust-boundary surfaces above and shipped the following fixes. Each is cited
to a commit so you can read the diff yourself.

### Delimiter wrappers for captions + tool results — commit `8d29b23`

All caption content enters the prompt inside `<spoken speaker="Alice">…</spoken>`
and all MCP tool results inside `<tool_result tool="github__get_file_contents">…</tool_result>`.
A zero-width-space neutralizer inserts a ZWSP inside any literal `</spoken>`
or `</tool_result>` the untrusted content tries to emit, so an attacker
cannot close the block early and smuggle plain-text instructions after it.
`SAFETY_RULES` in the system prompt tells the model that anything inside
delimiter blocks is untrusted data.

Speaker names are stripped of `[<>"'&]` before becoming attribute values;
tool names are allowlisted to `[\w.:-]{1,64}`. Without that, a crafted Google
Meet display name could have broken out of the `speaker="…"` attribute and
injected raw prompt text.

Covers: tool-result injection (M1), caption injection (H3).

### `auth_state.json` in `.gitignore` — commit `912a163`

`auth_state.json` is the Playwright storage state file. Before this commit,
only `browser_profile/` was ignored, so any automation that regenerated
`auth_state.json` and ran `git add .` would have silently committed
logged-in Google session cookies. `CLAUDE.md` told contributors "never
commit it" but `.gitignore` did not enforce the rule mechanically. Now it
does. The "never commit these" list is inline in both `.gitignore` and
`README.md` for grep-ability.

### Redact MCP tool arguments at DEBUG — commit `912a163`

`mcp_client.py` used to `log.debug(f"MCP tool arguments: {json.dumps(arguments)}")`
which dumped full values (repo paths, PR bodies, issue titles, pasted
snippets) into `/tmp/brainchild.log`. The new `_summarize_tool_args()` emits
`key=type[len]` only (e.g. `{path=str[23], team=str[3]}`). Full JSON dump is
opt-in via `BRAINCHILD_LOG_TOOL_ARGS=1` (strict `== "1"` equality; fails
closed on `"true"` or empty).

### Strip unsafe env keys from MCP config — commit `be9bf8c`

`config.py` now has an unsafe-env-key blocklist applied at config load:
`PATH`, `PYTHONPATH`, `PYTHONHOME`, `IFS`, plus prefixes `LD_` and `DYLD_`.
Case-insensitive: `path`, `Path`, `ld_preload` all caught. Dropped keys log
as `WARNING` with the server name so a mistaken config line is loud.

Without this, a config line like `env: { PATH: /tmp/attacker }` would have
redirected binary lookup; `LD_PRELOAD` / `DYLD_INSERT_LIBRARIES` would have
injected a shared library into every MCP invocation.

**Known gaps (non-Python runtimes — residual risk):** `NODE_OPTIONS`,
`RUBYOPT`, `PERL5LIB`, `CLASSPATH`, and `HOME` are analogous loader-injection
vectors for Node, Ruby, Perl, Java, and any tool that resolves config via
`$HOME`. Not currently in the blocklist because the shipped MCP servers are
Python. Will extend the blocklist when a non-Python MCP server ships in the
default agent set.

### Confirmation prompt shows all args with truncation — commit `be9bf8c`

`_request_confirmation()` in `chat_runner.py` now renders every argument
(previously capped at 5 — a malicious LLM could have hidden a 6th
destructive arg past the cap). Long values get head…tail truncation
(`CONFIRM_ARG_MAX=160`, head 70, tail 50); the user sees both ends so a
trailing malicious instruction can't hide in the middle of a long string.
When truncation fires, the message appends `(Full values in
/tmp/brainchild.log.)` and the full `args={args!r}` is logged at INFO.
`repr()` neutralizes Unicode bidi-override and control characters.

### Path hygiene pass — commit `ebb6b29`

`config.relativize_home(p)` swaps `$HOME` prefix for `~`. Used in:

- `agents/engineer/claude_code.py` — the `[workdir: …]` /
  `[repo: …]` footer in claude-code tool-results that flows into meeting chat.
  Before this fix, a claude-code reply carried `/Users/jojo/Desktop/brainchild/…`
  into meeting chat, leaking the machine username and directory layout to
  every participant.
- `connectors/linux_adapter.py` — two log lines that printed the absolute
  `auth_state.json` path.
- `connectors/linux_adapter.py` + `connectors/macos_adapter.py` —
  `BROWSER_PROFILE_DIR` is `chmod 0o700` after creation so other users on a
  shared host can't list login patterns. Wrapped in try/except so a
  perm-less filesystem doesn't crash startup.

A partial-prefix guard (`home + os.sep`) prevents `/home/jojofoo/…` from
being mis-relativized when the user's home is `/home/jojo`.

## Residual risks — documented, not fixed

These are known weaknesses where the right mitigation is an operational
recommendation rather than code.

### H1 — Tool confirmation accepts any participant's reply

`ChatRunner._pending_tool_call` does not check which participant sent the
affirmative. Anyone in the meeting can confirm a pending tool call the bot
asked the user about.

**Recommendation:** turn on Google Meet's **"host manages chat"** (Host
Controls → Chat) in any meeting where untrusted participants may join. That
keeps the affirmative channel between the bot and you. For 1-on-1s with
known counterparts, this is a non-issue.

### H2 — Affirmative match is a word-bag

The confirmation parser accepts `{yes, ok, sure, yep, yeah, approve,
confirmed}`. "ok, but first explain" will auto-confirm because `ok` is in
the set. This is deliberate — natural-language confirm UX trades precision
for friction.

**Recommendation:** when you're confirming a destructive tool call, reply
with a plain `no` if you want to cancel, not a hedge. Hedged replies are
treated as approvals. Same Meet "host manages chat" recommendation as H1
narrows who can confirm on your behalf.

### L1 — No allowlist on MCP `command`

Any agent config can specify any executable as an MCP server command. A
compromised config or an untrusted contributed agent could launch arbitrary
code. The right mitigation is governance around who contributes agents, not
a runtime allowlist (which would block legitimate use cases).

**Recommendation:** treat `agents/<name>/config.yaml` contributions like any
other code — review the MCP `command` / `args` / `env` before merging.
Phase 15.6.2 adds `agents/CONTRIBUTING.md` + CODEOWNERS to formalize this.

### L2 — Google session cookies on disk

`browser_profile/` and `auth_state.json` hold logged-in Google session
cookies. Anyone with local read access to those files can impersonate the
Google account in a browser.

**Blast radius:** full access to the Google account the bot is signed into.
Use a dedicated Google account for the bot, not your personal one. The
filesystem-perm hardening (`browser_profile/` `0o700`, `auth_state.json` +
`google_account.json` `0o600`, `debug/` `0o700`, plus `os.umask(0o077)` at
process start so any new file/dir under `~/.brainchild/` is born private —
session 173, commit `9a6d02e`) stops other users on a shared host and the
disk-mounted-elsewhere case (stolen laptop without FileVault, leaked
backup). It does not stop malware running as the bot's own user — that
threat model would need real encryption-at-rest (macOS Keychain / Linux
Secret Service), which is the v2 conversation. Note: `~/.brainchild/history/`
is still world-readable and contains every meeting's chat JSONL — pre-prod
acceptable, but harden before launch.

### L3 — Chat and captions logged in clear

`/tmp/brainchild.log` contains every chat message and (when captions are on)
every spoken word. It never leaves your machine. macOS typically clears
`/tmp` on reboot; Linux may not. Delete it manually if the meeting content
was sensitive. See the README "Privacy & logs" section for the fuller note.

### L4 — `claude-code` default permission mode

The `claude-code` MCP server (bundled in `agents/engineer/`) runs with
`bypassPermissions` by default, which in a worktree means a delegated task
could e.g. `curl | sh`. The trade-off is that with stricter permission modes
the delegated sub-agent keeps asking for approvals inside the worktree, and
those prompts never reach the meeting chat, so delegation stalls.

**Recommendation:** if you're delegating work that touches anything you
wouldn't run yourself blindly, set `CLAUDE_CODE_PERMISSION_MODE=acceptEdits` in
your `.env`. The trade-off is slower delegation and some stalled tasks.

## Pointers

- `README.md` — "Privacy & logs" and "Never commit these" sections.
- `.gitignore` — enforces the "never commit" list mechanically.
- `SECURITY.md` — reporting contact + SLA.
- `docs/roadmap.md` — Phase 15.6 block for the hardening history.
