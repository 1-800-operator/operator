# Security — threat model and residual risks

This document describes Operator's threat model, the mitigations already
shipped, and the risks that remain documented rather than fixed. It is the
single source of truth the README and `SECURITY.md` cross-link to.

Reporting contact and SLA live in `SECURITY.md` at the repo root.

## What Operator is

A chat-based AI meeting participant. It CDP-attaches to a dedicated dial
Chrome window running a Google Meet, opens the chat panel, watches for
messages addressed to it via the `@claude` trigger phrase, and forwards each
one to a long-lived interactive `claude` subprocess — one per meeting,
driven over a PTY — that owns its own tool loop. Claude's reply is relayed
back into meeting chat by tailing the Claude Code transcript.

Everything runs locally on the operator's machine; there is no Operator-side
server. The inner-claude subprocess inherits its MCP servers and skills from
the user's own `~/.claude/` hierarchy.

**The single most important thing to understand before running Operator is
in [§ Operator runs Claude with all permissions bypassed](#operator-runs-claude-with-all-permissions-bypassed)
below. Read that section.**

## Trust boundaries

In rough order of how trusted each input is:

| Input | Trust | Why |
|---|---|---|
| `~/.operator/.env` | **Trusted** | Local secrets file the user controls and populates themselves. |
| The user's `~/.claude/` hierarchy (`settings.json`, registered MCP servers, `skills/`, hooks) | **Trusted** | The user's own Claude Code config. Operator does not write to it. Note: inner-claude inherits all of it. |
| The operator-plugin hook scripts | **Trusted** | Shipped in the operator-plugin repo, installed by the user via the plugin marketplace. |
| User-installed MCP server binaries | **Semi-trusted** | Subprocesses the user chose to wire into their own `~/.claude`. Their *outputs* are untrusted (see below). |
| Inner-claude's replies | **Semi-trusted** | Claude's own output. It is relayed verbatim to meeting chat, but it can be *steered* by any of the untrusted inputs below — and it runs tools with permissions fully bypassed. |
| Google Meet chat messages | **Untrusted** | Any meeting participant can send one, and any `@claude` message is forwarded to inner-claude as a user turn. |
| Google Meet captions | **Untrusted** | Any speaker's words land here; inner-claude can pull them in via the bundled transcript MCP. |
| MCP tool results | **Untrusted** | A compromised or adversarial MCP server could return instructions masquerading as data. |
| Participant display names | **Untrusted** | Attacker-controlled; they ride along with each chat message and caption. |
| Foreign Stop-hook feedback | **Untrusted** | A Stop hook in the user's own project- or user-level `.claude/settings.json` can run `decision=block` and inject a redirect into inner-claude mid-meeting. Observable, not preventable — see residual risk R2. |

## Operator's default: Claude runs with all permissions bypassed

**This is Operator's default mode. If you are not comfortable with
what this section describes, use the yolo-off alternative documented
in the next section, or don't run Operator at all.**

By default, Operator spawns its inner-claude subprocess with
`--dangerously-skip-permissions`, on every meeting. The meeting flow
needs tools to run without per-call approval prompts (those prompts
would appear in a TUI nobody is watching, and the meeting would
stall), and Operator has no permission layer of its own to gate them
with.

The alternative is `/operator:dial-guarded` (see "Yolo-off mode"
below) which spawns with Claude Code's normal permission rules and
bridges each ask to meeting chat. The rest of this section describes
the *default* path.

### What "all permissions bypassed" actually means

`--dangerously-skip-permissions` disables Claude Code's entire permission
layer for that subprocess. Concretely:

- **No approval prompts.** The inner-claude can read/write/delete files, run
  any shell command, and call any MCP tool with no confirmation, ever.
- **Your `permissions.allow` list does nothing.** It is moot — everything is
  allowed anyway.
- **Your `permissions.deny` list does nothing.** This is the surprising and
  important part: a `"deny": ["Bash(rm:*)", "Read(./secrets/**)"]` in your
  `~/.claude/settings.json` or project `.claude/settings.json` **does not
  block anything** in a `--dangerously-skip-permissions` session. The deny
  list requires the permission layer to be active; this flag turns the whole
  layer off.
- **Your `permissions.ask` list does nothing**, for the same reason.
- **`PreToolUse` hooks do not gate the session.** A `PreToolUse` hook that
  returns a deny decision is not consulted.

The blast radius is the full authority of the OS user account Operator runs
as: every file that user can read or write, every credential in that user's
home directory, every network call that user can make, and every MCP server
registered in that user's `~/.claude`.

### Can a user dial this back? Honestly: not via configuration *in this mode*

In yolo-on (the default), there is **no allowlist, denylist, or
settings.json knob** a user can set to constrain a
`--dangerously-skip-permissions` subprocess. The flag is genuinely
all-or-nothing — within this mode. Switching to `/operator:dial-guarded`
re-activates Claude Code's permission rules (so `permissions.allow` /
`deny` from `~/.claude/settings.json` start mattering again, and the
hook bridges anything not pre-categorised to chat); see the "Yolo-off
mode" section below.

The *only* configuration lever that affects it at all is
`permissions.disableBypassPermissionsMode`, and that:
- must live in **managed settings** (organization / MDM-level policy), not
  user or project `settings.json`; and
- does not "constrain" the session — it **bans the flag outright**, which
  means `operator dial` would simply fail to spawn inner-claude.

There is no middle setting. The one residual guardrail Claude Code keeps even
in this mode is a hardcoded circuit-breaker that still prompts on
`rm -rf /` and `rm -rf ~` — that cannot be relied on as a general safety net.

### Your real levers are operational, not configuration

If full, unprompted tool access is more than you want to grant, the controls
available to you are operational:

- **Run Operator under a dedicated, least-privilege OS account** — a user
  with no access to your personal files, no production credentials, a
  scratch home directory, and only the MCP servers you deliberately want the
  bot to have. This is the recommended posture.
- **Curate the inner-claude's `~/.claude`.** Whatever MCP servers and skills
  are registered for that OS user *are* the bot's capabilities. Register
  only what you want a meeting participant to be able to invoke.
- **Treat the `@claude` trigger as the real gate.** Operator is "speak when
  spoken to" — it acts only on messages containing the trigger phrase. Don't
  `@claude` it toward anything you would not run yourself, unprompted, on
  that machine.
- **Control who can `@claude` it.** Any participant in the meeting can
  address the bot. In meetings with untrusted attendees, turn on Google
  Meet's **Host Controls → "Host manages chat"** so untrusted participants
  cannot drive the bot.
- **If none of that is an acceptable trade, do not run Operator.** That is a
  legitimate outcome — the tool is not for every threat model.

## Yolo-off mode: what `/operator:dial-guarded` actually does

Operator ships a second slash command, **`/operator:dial-guarded`** (and
`operator dial-guarded claude <url>` from the terminal), for users who
want to gate the inner-claude's tool calls without dropping into
yolo-on. It is the **only** in-product alternative to the all-permissions
default; pick the one that matches your trust model for the meeting.

### How it works

1. Inner-claude is spawned with **`--permission-mode default`**, not
   `--dangerously-skip-permissions`. Claude Code's normal permission
   resolution applies. Tools that the user has pre-allowed in
   `~/.claude/settings.json` run silently as usual; tools that are
   pre-denied are blocked natively; everything else triggers a
   `PermissionRequest` event.
2. The operator-plugin's `PermissionRequest` hook intercepts that event
   and writes a request record into the meeting's session dir.
3. Operator posts a question into meeting chat: *"Claude wants to use
   `Bash` to run `npm install` — reply *yes* or *no*."*
4. Operator watches chat for the **first non-self reply from any
   participant** (the documented H1 tradeoff — see residual risks below).
5. The participant's verbatim reply is handed to a **separate
   long-lived classifier claude** running alongside the meeting brain
   (one extra `--dangerously-skip-permissions` subprocess per meeting).
   The classifier interprets the reply as YES or NO via one tiny ~2-3s
   turn — no operator-side word-bag matching. *"sure"*, *"sounds
   good"*, *"👍"*, *"sí adelante"*, *"nah, skip it"* are all interpreted
   in context.
6. The classifier's verdict resolves the hook: allow → tool runs; deny
   → tool blocked, claude is told the participant's words and
   typically narrates the refusal.

### What yolo-off does **not** do

- **No "allow always" / persistent-allow path.** Every uncategorised
  tool asks every time. If you want friction-free, that's `/operator:dial`.
- **No mutation of your `settings.json`.** Yolo-off respects your
  pre-existing `permissions.allow` / `permissions.deny` rules but
  never writes to them.
- **No sandboxing.** When a participant approves a tool, that tool
  runs with the OS user's full authority — same as yolo-on. Yolo-off
  gates *per-ask*; it does not *contain*.

### Cost and latency

- **Cost: $0 marginal.** The classifier sidecar runs on the same
  Claude subscription pool as the main meeting brain (interactive
  PTY-driven, not `claude -p`). It costs nothing per use.
- **Boot: hidden.** The classifier's ~6s spawn happens in parallel
  with the main provider's pre-warm during the meeting-join window
  (~30s of Chrome attach + lobby wait + whisper warm-up). The first
  permission ask doesn't pay an init tax.
- **Per-ask latency: 2–3s** (measured across 19 reply scenarios in
  the spike that validated this design — see
  `debug/14_24_permreq_spike/DECISION.md` for the full chain).

### Residual risks specific to yolo-off

These risks are **dormant in yolo-on** (the permission flow doesn't
fire) and **reactivated in yolo-off**.

**G1 — Any participant in the meeting can approve.** There is no
per-sender allowlist on who counts as the answerer. The first
non-self chat reply after the question is the one the classifier
sees. In meetings with untrusted participants, **turn on Google Meet's
"Host manages chat"** to keep the answer channel between you and the
bot.

**G2 — Natural-language ambiguity.** The classifier is good
(19/19 in the validation sweep, including emoji-only and non-English
approvals) but it is not infallible. The prompt instructs *"if
unsure, NO"* — so it skews toward refusal, which is the safe default
— but a confidently-ambiguous reply could in principle go either way.
A misclassified deny is recoverable (re-mention `@claude` to retry);
a misclassified allow runs the tool.

**The R1 baseline applies in both modes.** Untrusted meeting input
(chat, captions, tool results, foreign-hook feedback) still reaches
inner-claude. Yolo-off only adds a per-ask gate on tool execution; it
does not filter the input stream. A prompt-injection payload can
still steer claude into requesting a malicious tool — yolo-off then
asks the meeting whether to allow it. If a participant approves
without recognising the injection, the tool runs.

### When to pick which

- **Pick `/operator:dial` (yolo-on, default)** when you trust the
  meeting brain to act unsupervised — you're running with a curated
  MCP set, on a least-privilege OS account, and friction is the enemy.
- **Pick `/operator:dial-guarded` (yolo-off)** when you want the
  meeting brain to pause and ask before doing anything you haven't
  already approved — at the cost of ~2-3s per uncategorised tool and
  a participant having to answer in chat.

The threat model in §"Operator runs Claude with all permissions
bypassed" applies most strongly to yolo-on. Yolo-off narrows the
"runs unprompted" surface to *only* what the user pre-allowed — but
the operational guidance there (least-privilege OS account, curated
MCP set, host-manages-chat) is still the right posture for both.

## What Operator changes in your `~/.claude` config

Installing Operator is not read-only — `install.sh` modifies your Claude Code
configuration in two ways you should be aware of:

### It registers a user-scope MCP server

The bundled transcript MCP server (`search_captions` / `list_captions` /
`list_speakers`, backed by the live meeting JSONL) is registered
**user-scope** via `claude mcp add transcript --scope user`. That means it is
attached to *every* Claude Code session on your machine afterward — terminal
or meeting — not just meetings. It only ever reads the meeting record for the
currently-active meeting (or nothing, if there isn't one).

### It adds four entries to `permissions.allow`

So the `/operator:*` plugin skills work without silent-failing in the Claude
Code desktop app, the installer merges the following into the `allow` list of
your `~/.claude/settings.json`:

| Entry | What it allows | Why |
|---|---|---|
| `Bash(operator:*)` | Every `operator` CLI subcommand (`dial`, `status`, `hangup`, `doctor`, `recap`) to run without an approval prompt. | The desktop app silent-fails un-allowlisted `!` blocks inside plugin skills — without this, the slash commands appear broken. |
| `Bash(claude plugin marketplace update:*)` | The `/operator:update` skill to refresh the plugin marketplace. | Same desktop-app silent-fail reason. |
| `Bash(claude plugin update operator:*)` | The `/operator:update` skill to upgrade the operator plugin. | Same. |
| `mcp__transcript__*` | Every tool on the bundled transcript MCP server to run without a prompt. | So inner-claude never hits a permission prompt mid-meeting when it reads captions. |

What to know about this:

- **It is a real, persistent widening of your Claude Code allow list.**
  `Bash(operator:*)` in particular means *any* `operator` subcommand auto-runs
  in *all* your Claude Code sessions, not just meeting ones, until you remove
  it. The entry is scoped to the `operator` command, but it is not scoped to
  meetings.
- The merge is **additive and idempotent** — it appends only the entries
  above if they are missing, never removes or rewrites your existing rules,
  and is a no-op on re-run.
- It is applied **once, at install time**, by `install.sh` — not on every
  meeting. Operator's runtime never touches `settings.json`.
- **To undo it:** remove those four lines from
  `~/.claude/settings.json` → `permissions.allow` by hand. The
  [uninstall steps in the README](../README.md#uninstall) remove the plugin,
  MCP server, and CLI, but do **not** prune the allow-list entries — that is a
  manual edit.

Note the asymmetry with the section above: these `allow` entries are
config Operator *adds*, but they only have force in your *own* Claude Code
sessions. They do **not** constrain — and are not even consulted by — the
inner-claude meeting subprocess, which runs with `--dangerously-skip-permissions`
and ignores the entire `permissions` block.

## Local artifacts and "never commit" hygiene

Operator keeps all of its user-scoped state under `~/.operator/`, **outside
the repository tree**, so there is no Operator-managed secret living inside a
checkout that a stray `git add .` could capture. The artifacts are:

- `~/.operator/.env` — the secrets file (any API keys / tokens the user puts
  there). Operator never writes to it.
- `~/.operator/dial_profile/` — the dedicated Chrome profile for dial mode.
  Holds logged-in Google session cookies — see residual risk R4.
- `~/.operator/history/<slug>.jsonl` — append-only meeting record (chat +
  captions). See residual risk R5.
- `~/.operator/sessions/<id>/` — per-meeting hook state (`replies.jsonl`,
  `ready.flag`, `metadata.json`).
- `~/.operator/debug/` — screenshots + HTML dumps from failure paths.
- `~/.operator/.current_meeting` — marker file pointing the bundled
  transcript MCP at the active meeting's JSONL.

The repo-root `.gitignore` still defensively lists `.env`, `credentials.json`,
`token.json`, `auth_state.json`, and `browser_profile/` — those entries cover
pre-migration checkouts and misconfigured environments only; in a current
install nothing sensitive is born inside the repo tree. If you ever see one
of those files show up untracked in `git status`, something has gone wrong —
do not `git add .` blindly.

## What's been hardened

### Local file-mode hygiene — `os.umask(0o077)` + explicit chmod

`__main__.py` sets `os.umask(0o077)` at process start, so every file and
directory Operator creates under `~/.operator/` is born `0o600` / `0o700` —
not readable by other users on a shared host. `meeting_record.py` also does
an explicit retroactive `chmod` on the meeting JSONL to cover legacy files
created before the umask fix.

This stops the other-users-on-a-shared-host case and the
disk-mounted-elsewhere case (stolen laptop without FileVault, leaked
backup). It does **not** stop malware running as the bot's own OS user — that
threat model would need real encryption-at-rest (macOS Keychain / Linux
Secret Service), which is a v2 conversation.

### Path hygiene — `config.relativize_home()`

`config.relativize_home(p)` swaps a `$HOME` prefix for `~` before any path
is logged or surfaced. A partial-prefix guard (`home + os.sep`) prevents
`/home/jojofoo/…` from being mis-relativized when the user's home is
`/home/jojo`. This keeps the machine username and absolute directory layout
out of logs.

### Billing-leak protection — `ANTHROPIC_API_KEY` stripped at spawn

Every inner-claude subprocess is spawned with `ANTHROPIC_API_KEY` explicitly
removed from its environment, so a globally-set key cannot silently redirect
billing from the user's Claude subscription to a metered API account. This
is a billing-integrity protection, not a confidentiality one, but it lives
here because it is an unconditional spawn-env mutation. See the README
"Billing protection" section for the fuller note.

## Residual risks — documented, not fixed

These are known weaknesses where the right mitigation is an operational
recommendation rather than code.

### R1 — Untrusted meeting input reaches a fully-permissioned Claude

This is the headline residual risk and it follows directly from the
architecture. Any `@claude` chat message is forwarded verbatim to inner-claude
as a user turn; captions, MCP tool results, participant display names, and
foreign-hook feedback are all reachable by that same subprocess — and that
subprocess runs with permissions fully bypassed (see the dedicated section
above). A prompt-injection payload in any of those channels lands on a Claude
that can act on it without an approval prompt.

Operator deliberately does **not** add an operator-side confirmation flow,
prompt-delimiter wrapper, or tool-call approval gate. Earlier architectures
had some of these; they were removed in the Phase 14.22 pivot because
Operator no longer constructs the prompt or runs the tool loop — inner-claude
owns both. Re-introducing a safety wrapper Operator cannot actually enforce
would be security theater.

**Recommendation:** the operational controls in the bypassed-permissions
section *are* the mitigation — least-privilege OS account, curated
`~/.claude`, "host manages chat", and judicious use of the trigger phrase.
Treat anything a meeting participant can say as attacker-controlled input to
a capable agent.

### R2 — Foreign Stop hooks can redirect inner-claude mid-meeting

`claude --resume` is cwd-scoped, so Operator must spawn inner-claude in the
user's project directory. A side effect: that directory's (and the user's
global) `.claude/settings.json` **hooks fire inside meetings**. A foreign
Stop hook that runs `decision=block` injects a `"Stop hook feedback: …"`
turn, which can redirect the bot.

Operator *detects* this — it surfaces a "a hook outside this meeting
interrupted my last turn" notice to the room — but it does **not** prevent
it, and it will not mutate the user's config to disable their hooks. Worse,
under `--dangerously-skip-permissions` a foreign hook whose `reason` reads as
a benign instruction could get acted on (observed during development: a hook
caused inner-claude to edit the user's global `~/.claude/settings.json`).

**Recommendation:** know what Stop hooks are configured in both your global
`~/.claude/settings.json` and the project directory you launch Operator from.
If you don't control those hooks, don't launch Operator from that directory.

### R3 — Any participant can address the bot

Operator acts on any chat message containing the `@claude` trigger,
regardless of sender. There is no per-sender allowlist.

**Recommendation:** in any meeting where untrusted participants may join,
turn on Google Meet's **Host Controls → "Host manages chat"**. For 1-on-1s
with known counterparts this is a non-issue.

### R4 — Google session cookies on disk

`~/.operator/dial_profile/` holds the logged-in Google session for the
account the bot signs in as. Anyone with local read access to that directory
can impersonate that Google account in a browser.

**Blast radius:** full access to that Google account. **Use a dedicated
Google account for the bot, not your personal one.** The `umask 0o077` /
explicit-chmod hardening stops other users on a shared host and the
disk-mounted-elsewhere case; it does not stop malware running as the bot's
own OS user.

### R5 — Meeting content logged in clear

`/tmp/operator.log` contains every chat message and (when captions are on)
every spoken word. `~/.operator/history/<slug>.jsonl` is the durable version
of the same. Neither leaves your machine, but both are plain text. macOS
typically clears `/tmp` on reboot; Linux may not. Delete them manually if
the meeting content was sensitive. See the README "Privacy & logs" section.

## Pointers

- `README.md` — "Privacy & logs" and "Never commit these" sections.
- `.gitignore` — defensive enforcement of the "never commit" list.
- `SECURITY.md` — reporting contact + SLA.
- `CLAUDE.md` — "Tool Permissions" and the Key Data Flow notes on the
  naked-spawn invariant.
- `docs/roadmap.md` — phase history, including the Phase 14.22 PTY pivot.
</content>
</invoke>
