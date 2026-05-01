# Engineer

Engineering assistant — looks up GitHub issues and PRs, delegates coding tasks
to Claude Code, and brings your existing `~/.claude/skills/` into meetings.

## Who it's for

Engineers who want a coding partner in their meetings. Ask it to look up a PR,
check a file, or delegate a task to Claude Code — it writes code in an isolated
worktree and reports back.

## What you need

- **Anthropic API key** — for the LLM
- **GitHub personal access token** — for repo access
- **`claude` CLI** — installed and authenticated ([install guide](https://docs.anthropic.com/en/docs/claude-code))
- **GitHub MCP server binary** — `github-mcp-server` in the repo root ([releases](https://github.com/github/github-mcp-server/releases))

## Setup

```bash
# 1. Fill in your API keys
cp agents/engineer/.env.example .env
# Edit .env with your keys

# 2. Run — direct URL or auto-open meet.new
operator run engineer https://meet.google.com/xxx-yyyy-zzz
operator run engineer
```

## How delegation works

When you ask Engineer to write or modify code, it delegates the task to
Claude Code running as a subprocess. Two things to know:

**1. Work happens in a sandbox, not your working tree.** Claude Code runs
in an isolated **git worktree** — a separate checkout of your repo on its
own branch, under `.claude/worktrees/<name>/`. Files you ask Engineer to
create or edit land *there*, not in your main working copy. Engineer will
tell you the sandbox path when it reports back.

**2. You must name the repo.** On the first delegation in a chat, tell
Engineer which repo to work in by absolute path:

> *"Engineer, work in `~/code/marketing-site`. Add a hero section to
> `src/pages/index.tsx`."*

Engineer remembers this per chat. Follow-up requests ("now add tests",
"show me the diff", "revert that") reuse the same sandbox so work is
continuous. If you want to switch repos mid-chat, just name the new path:

> *"Now in `~/code/api`, fix the auth middleware."*

Engineer spins up a separate sandbox for the second repo and keeps both
alive in parallel. Multiple repos per chat is fine.

### Landing changes into your working tree

Sandbox work stays in the sandbox until you land it. After a delegation
succeeds, Engineer will offer:

> *"Want me to land these changes in your working tree?"*

Saying yes applies the sandbox's diff to the real repo. If your working
tree has diverged, the apply will fail cleanly and Engineer will surface
the conflict rather than force-merge.

### Limitations

- One repo per delegation call. *"Fix X in repo A and Y in repo B"* is
  two separate asks.
- Repos must already be cloned locally — Engineer does not clone from
  GitHub on demand (yet).
- Private-repo auth relies on whatever env (`GITHUB_TOKEN`, ssh keys)
  is already set up for your shell.

## Worktree cleanup

Every time Engineer delegates to Claude Code, the CLI creates an isolated git
worktree under `.claude/worktrees/<name>/` and a matching `worktree-<name>`
branch. **Claude Code does not auto-remove these** once the delegation
finishes, so they accumulate across meetings.

Prune them periodically:

```bash
# List active worktrees
git worktree list

# Remove a specific one (and its branch)
git worktree remove .claude/worktrees/<name>
git branch -D worktree-<name>

# Or: bulk-prune worktrees whose directories have been deleted
git worktree prune
```

## Demo

<!-- TODO: 15s GIF showing delegate_to_claude_code creating a branch mid-meeting -->

