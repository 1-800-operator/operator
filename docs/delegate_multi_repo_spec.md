# Spec — Delegate Multi-Repo & Continuity

*Phase 15.5.6 follow-up · drafted session 126 · not yet implemented.*

## Problem

Live run of `operator try engineer` in session 126 surfaced three related issues in `agents/engineer/delegate_to_claude_code.py`:

1. **Ephemeral, invisible worktrees.** Every delegate call runs `claude -p <task> --worktree`, which creates a fresh git worktree under `.claude/worktrees/<random-name>/`. Engineer reports "confirmed working" but the file lives in a throwaway tree the user never sees.
2. **No continuity between calls.** Second delegate ("prove it / show the file") spawns a *new* worktree with no knowledge of the first. In the live run the LLM silently pivoted from "show contents of scratch/hello_op.py" to "create scratch/hello_op.py then run it" because the file didn't exist in the new sandbox.
3. **Hard-pinned to Operator's own repo.** Worktrees branch off whatever repo Operator was launched from — i.e. `/Users/jojo/Desktop/operator/` — which is almost never the repo the user wants to work in.

Consequence: Engineer is effectively a demo today. It cannot be used to work on a real project.

## Solution model — per-repo continuity

Engineer tracks a small mapping `{repo_path → claude_session_id}` in the long-lived delegate MCP server process. Every delegate call names a `repo_path`. First call against a repo spawns a new worktree and records the session id. Subsequent calls against the same repo pass `--resume <session_id>` so Claude Code reuses the same worktree. Different repo → different entry in the map → different worktree. User flips freely between repos in one chat.

This drops out of seeing that **continuity is per-repo, not per-session**.

## Changes

### 1. `delegate_to_claude_code.py`

- Add `repo_path: str` to the tool input schema, required. Must be an absolute path to an existing git repo.
- Validate: path exists, is a dir, contains `.git` (or is inside a git worktree). Return a clear error if not.
- Module-level `_SESSIONS: dict[str, str] = {}` mapping `repo_path → session_id`.
- On call:
  - `cwd = repo_path`
  - If `repo_path in _SESSIONS`: append `--resume _SESSIONS[repo_path]` to the command.
  - Else: first call for this repo; run with `--worktree` as today.
- Parse the `session_id` from Claude Code's JSON output and store it.
- Return result **with the worktree path surfaced** (parsed from Claude Code's output — it prints `cwd` in the final JSON, or we can derive via `git -C <repo_path> worktree list --porcelain`).

**Open question (blocking):** does `claude -p --resume <id>` reuse the original `--worktree`, or does `--worktree` on a resumed session spawn a fresh one? Verify by: `claude -p "echo hi" --worktree --output-format json` → capture `session_id` → `claude -p "pwd" --resume <id>` and check if cwd matches. If resume doesn't reuse the worktree, fallback: manage worktrees ourselves with `git -C <repo> worktree add` + invoke `claude -p` with cwd set to the worktree dir (no `--worktree` flag). Slightly more plumbing but decouples us from Claude Code's resume semantics.

### 2. Tool result shape

Every delegate result appends a machine-parseable footer:

```
[workdir: /Users/jojo/code/marketing-site/.git/worktrees/<name>]
[session: <session_id>]
[Completed in 5.4s, cost $0.069]
```

System prompt is updated so Engineer mentions the workdir to the user when reporting results.

### 3. Engineer system prompt

Add to `agents/engineer/config.yaml`:

- Every delegate call requires `repo_path`. On first delegation of a chat, if the user hasn't named a repo, ask: "Which repo should I work in? (absolute path)" Don't guess, don't default to Operator's cwd.
- Remember the most recently named repo; reuse it as the default unless the user names a different one.
- When reporting results, tell the user the workdir path so they know the changes are in a sandbox, not their working tree.

### 4. Land-changes flow

New tool in the delegate MCP: `land_worktree_changes(repo_path: str)`. Behavior:

- Runs `git -C <worktree> diff main` (or whatever the parent branch was at worktree creation).
- Applies that diff to the host repo via `git -C <repo_path> apply`.
- Confirmation-gated (it's a write).
- Returns the list of files touched.

Engineer's prompt is updated to offer landing after a successful delegate: *"Want me to land these changes into your working tree?"*

Open question: clean-merge vs dirty-apply. `git apply` is the simplest first cut — works when the user's tree hasn't diverged from the worktree's starting point. If the user has made their own local edits during the call, apply fails cleanly and we report the conflict instead of merging. Merge support is a later add.

### 5. Auto-prune on startup

In `__main__.py`, when the `engineer` agent is selected, shell out to `git -C . worktree prune` before starting the MCP servers. Cheap. For more aggressive cleanup (removing trees whose branches have been landed), land a small helper that lists worktrees older than N days and removes them — but only if no uncommitted work inside them. Day-N threshold configurable.

## Non-goals

- **Cross-repo in a single call.** One delegation = one `repo_path`. "Fix X in repo A and Y in repo B" is two calls.
- **Clone-on-demand from GitHub.** Deferred. User points at repos they already have locally.
- **Multi-worktree per repo.** One sticky worktree per repo per process lifetime. If user wants a fresh sandbox, restart Operator or extend with an explicit "fresh sandbox" signal later.
- **Auth across repos.** Claude Code inherits env from the delegate subprocess — GitHub PAT etc. already flows through. Private repos behind separate auth are out of scope for v1.

## Test plan

Smoke-test via `operator try engineer`:

1. First delegate in Operator's own repo: `"write scratch/hello_op.py"`. Confirm file lands in a worktree, Engineer reports the workdir path.
2. Second delegate same session: `"add a comment to scratch/hello_op.py"`. Confirm it edits the *same* file in the *same* worktree (not a new one).
3. Third delegate at a different repo: `"in ~/code/other-repo, list the top-level files"`. Confirm new worktree under that repo, no interference with the first session.
4. Back to first repo: `"show scratch/hello_op.py"`. Confirm reuse of the first session/worktree.
5. `"land those changes"`: confirmation prompt, then `scratch/hello_op.py` appears in the real working tree of the first repo.
6. Restart Operator: `git worktree list` shows the stale trees pruned.

## README updates

`agents/engineer/README.md` gets a new "How delegation works" section — draft text in this PR's README patch. Key points for the user:

- Claude Code runs in an isolated worktree, not your working tree.
- You must name the repo (absolute path) on first delegation in a chat.
- Engineer remembers per-repo sessions so follow-ups (edit, verify, extend) reuse the same sandbox.
- Changes stay in the sandbox until you say "land them" — which applies the diff to your working tree.
- Multiple repos in one chat: name each one when you switch.

## Rollout

One PR, one commit. Phase-tag as `15.5.7`. Estimated effort: ~3h (delegate tool + session map + system-prompt + land tool + prune), ~1h live smoke + docs.
