# Operator

> Operator brings Claude into your Google Meet. One command to install, one to join.

Operator is an open-source tool that drops Claude into a Google Meet as a
chat participant. It watches the meeting chat panel, and when someone types
`@claude …`, it hands that message to a Claude Code subprocess running on
your machine — which can read files, run commands, file tickets, open PRs,
and anything else your Claude Code setup can do — then relays the reply back
into the meeting chat in real time.

Everything runs locally. There is no Operator-side server, no account, and
no API key of its own — Operator drives the `claude` CLI you already have.

## Requirements

- **macOS** (dial mode is macOS-only for now).
- **Google Chrome** installed (Operator drives a real Chrome window for Meet).
- **Claude Code CLI** installed and logged in (`claude login`). Operator uses
  it as the meeting's brain, on your existing Claude subscription.
- Python 3.10+ (the installer provisions one via `uv` if your system Python
  is older).

## Install

```bash
curl -fsSL https://1-800-operator.com/install | sh
```

That one command bootstraps [`uv`](https://github.com/astral-sh/uv) if
missing, installs the `operator` CLI, registers Operator's bundled
transcript MCP server, installs the `operator` Claude Code plugin (the
`/operator:*` slash commands), and seeds `~/.operator/.env`. It does not
modify your shell rc files and is safe to re-run.

Prefer to read the script first? Download it, `less` it, then run it — same
outcome. Or skip the shell pipeline entirely and install via `uv` directly:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh          # uv, via its own installer
uv tool install git+https://github.com/1-800-operator/operator.git
```

If you take the `uv`-only path, re-run the full `install.sh` afterward (or
register the transcript MCP and plugin by hand) so the slash commands and
caption tools are wired up.

When it's done, verify everything is ready:

```bash
operator doctor
```

## Use it

**From Claude Code (recommended).** In any Claude Code session, run:

```
/operator:dial <meet-url>            # default: every tool runs unprompted
/operator:dial-guarded <meet-url>    # ask the meeting before each uncategorised tool
```

Either bridges your *current* Claude Code session into the meeting — the
meeting brain inherits the context you've already built up in that session.
The two differ in how the bot's tool calls are gated; see "Permissions &
safety" below for the tradeoff. Other slash commands:
`/operator:status`, `/operator:hangup`, `/operator:doctor`.

**From a terminal.** You can also attach directly, without bridging a
session:

```bash
operator dial claude <meet-url>           # join a meeting (yolo on)
operator dial-guarded claude <meet-url>   # join with permission asks bridged to chat
operator status                           # is Operator in a meeting?
operator hangup                           # leave the meeting
operator doctor                           # diagnostic check
```

A fresh Claude Code session is born on the first `@claude` mention. Pass
`--resume-session <id>` to bridge a specific session instead.

The first time you run dial mode, a dedicated Chrome window opens — sign into
Google in it once, and the session persists for future meetings. This window
is Operator-owned and separate from your everyday Chrome.

## How it works

1. Operator opens a dedicated "dial" Chrome window, joins the meeting URL,
   and opens the chat panel.
2. It watches chat for messages containing `@claude`. Operator is
   "speak when spoken to" — it only acts on the trigger phrase. Once you
   mention `@claude`, you have **90 seconds** of follow-up without
   re-mentioning — the bot stays in conversation with you and treats
   your next message as a continuation. The window slides forward on
   every reply; a different sender has to `@claude` to take the floor.
   Rapid corrections within ~2 seconds collapse to your latest message,
   so a typo + fix sends one prompt, not two.
3. Each forwarded message is handed to a long-lived interactive `claude`
   subprocess (one per meeting) that owns its own tool loop.
4. Claude's reply streams back into the meeting chat, prefixed with
   `[🤖 Claude]` so the room can tell the bot's messages from yours.
5. When everyone else has left, Operator leaves automatically.

## Permissions & safety — read this before you run it

Operator ships **two modes**. Pick the one that matches the level of
control you want over the meeting brain's tool use.

### Default: `/operator:dial` (yolo on)

The Claude subprocess is spawned with `--dangerously-skip-permissions`.
The meeting flow needs tools to run without per-call approval prompts
(there is no TUI for anyone to approve them in), and Operator has no
permission layer of its own.

What that means concretely:

- The Claude subprocess can read/write/delete files, run shell
  commands, and call any MCP tool **with no confirmation**.
- Your `permissions.allow` / `permissions.deny` / `permissions.ask`
  rules in `~/.claude/settings.json` **have no effect** on it. The
  flag is all-or-nothing in this mode.
- Untrusted meeting input (any participant's `@claude` message,
  captions, MCP tool results) reaches a Claude that can act on it
  without a prompt.

If that trade isn't acceptable for your environment, your levers are
operational: run Operator under a dedicated, least-privilege OS
account; curate which MCP servers are registered for that account;
use Google Meet's "host manages chat" control; and only `@claude` it
toward things you'd run yourself. Or use the alternative mode below.

### Alternative: `/operator:dial-guarded` (yolo off)

Same product, but the Claude subprocess is spawned with normal Claude
Code permission rules instead. Tools the user has pre-allowed in
`~/.claude/settings.json` still run silently; pre-denied tools are
blocked; **everything else triggers a yes/no question into meeting
chat**. The bot waits for a participant to reply, a separate
classifier subprocess interprets their words ("sure" / "nah" / "👍" /
etc.) as YES or NO, and the tool runs (or doesn't) accordingly.

Tradeoffs honest:

- ~2-3 seconds of friction per uncategorised tool call (the time to
  post the question, get a reply, and run the classification).
- Anyone in the meeting can answer the question. In meetings with
  untrusted participants, turn on Google Meet's **"host manages
  chat"**.
- No "always allow this" path within yolo-off — every uncategorised
  tool asks every time. If a category of tools should always run
  unprompted, add it to your `~/.claude/settings.json`
  `permissions.allow` (or use yolo-on for the whole meeting).
- Costs nothing per use (subscription pool — no `claude -p` calls).

Pick yolo-off when you want the bot to pause and ask before doing
anything you haven't already approved. Pick yolo-on when friction is
the enemy and the operational mitigations cover your threat model.

### The full picture

Trust boundaries, residual risks, and the operational guidance behind
both modes live in [`docs/security.md`](./docs/security.md). Read it
before running either mode.

### macOS permissions you'll see

The first time you run `/operator:dial` on a fresh machine, expect three
macOS prompts. All are one-time, per-bundle, click-Allow-and-done:

- **Screen Recording + Microphone** for `operator-audio-capture.app` —
  the signed helper that captures meeting audio (your mic + the other
  participants' system audio). `install.sh` warms these up at install
  time so you grant them once, upfront.
- **"<app> would like to access data from other apps"** for whichever
  app you ran `/operator:dial` *from* (Claude Code Desktop, Cursor,
  Terminal, etc.) — this is macOS's App Management permission, asked
  because operator launches the dedicated dial Chrome window via
  `open -na "Google Chrome"`. Per-parent: you'll see it once for each
  app you ever run `/operator:dial` from.

## Privacy & logs

Operator writes a diagnostic log to **`/tmp/operator.log`** on every run,
containing the Meet URL, chat messages (with sender names), captions, and
tool-call metadata. It never leaves your machine, but it's plain text in a
shared directory — macOS typically clears `/tmp` on reboot. Delete it
manually if a meeting was sensitive.

Chat and caption history also lands in `~/.operator/history/<slug>.jsonl` —
the durable record the bot replays from. Same sensitivity profile. Files
created under `~/.operator/` are mode `600` / `700` by default.

### Shared session store

The meeting brain is a normal Claude Code session, so it's persisted in
**the same place as your regular Claude Code work**:

```
~/.claude/projects/<encoded-working-dir>/<session-id>.jsonl
```

Two things to know:

1. Meeting sessions and your coding sessions live side by side. The
   `claude --resume` picker lists them mixed together — don't pick the wrong
   one.
2. The folder grows over time, with the same retention semantics as your
   regular Claude Code work. Prune it however you already do.

**Bonus:** because these are normal sessions, you can pick a meeting back up
in your terminal afterward — `claude --resume` from the same directory you
launched Operator from, and you're talking to the same brain that just left
the call, with full context.

### Billing protection

If `ANTHROPIC_API_KEY` is set in your environment, Claude Code will bill your
metered API account instead of your subscription — silently. Operator strips
`ANTHROPIC_API_KEY` from every Claude subprocess it spawns, unconditionally,
so a globally-set key can't leak in and redirect billing. You don't need to
do anything to enable this.

### Never commit these

Operator keeps all of its state under `~/.operator/`, outside any repo, so
nothing sensitive is born inside a checkout. The files that hold secrets or
logged-in Google session state and must stay local:

- `~/.operator/.env` — any API keys / tokens you put there.
- `~/.operator/dial_profile/` — the dedicated Chrome profile (Google session
  cookies). **Use a dedicated Google account for the bot, not your personal
  one.**

The repo's `.gitignore` defensively lists these and a few legacy names. If
one ever shows up untracked in `git status`, something has gone wrong — don't
`git add .` blindly. See [`docs/security.md`](./docs/security.md) for the
full picture.

## Uninstall

```bash
claude plugin uninstall operator             # remove the slash commands
claude mcp remove transcript --scope user    # remove the caption tools
uv tool uninstall 1-800-operator             # remove the CLI
rm -rf ~/.operator                           # remove history, profile, .env
```

## More

- [`CLAUDE.md`](./CLAUDE.md) — architecture, commands, configuration layout.
- [`docs/security.md`](./docs/security.md) — threat model and residual risks.
- [`SECURITY.md`](./SECURITY.md) — reporting a vulnerability.

## Help & community

- **Bug?** [File an issue](https://github.com/1-800-operator/operator/issues/new/choose)
  using the bug-report template.
- **Question, idea, or built something cool?**
  [GitHub Discussions](https://github.com/1-800-operator/operator/discussions).
- **Security vulnerability?** Don't open a public issue — see
  [`SECURITY.md`](./SECURITY.md).
- See [`SUPPORT.md`](./SUPPORT.md) for the full routing guide.
</content>
