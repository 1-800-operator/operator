"""
MCP server that delegates coding tasks to Claude Code via the `claude` CLI.

Two tools:
  - delegate_to_claude_code(task, repo_path)
  - land_worktree_changes(repo_path)

First delegation against a repo runs `claude -p <task> --worktree` in that
repo, records the resulting session_id + worktree path. Subsequent
delegations against the same repo run `claude -p <task> --resume <id>` in
the worktree, so Claude Code reuses the sandbox and the conversation.
Different repos get different sessions — the user can flip between repos
in one chat.

land_worktree_changes diffs the sandbox against the repo HEAD captured at
first delegation and applies the diff into the user's working tree.

Prerequisite: `claude` CLI installed and authenticated.
"""

import asyncio
import json
import os
import shutil
import re
import subprocess
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

server = Server("claude_code")

# Single rolling side-log of the inner claude CLI's stream-json output. The
# operator ChatRunner tails this file during a delegate call to surface
# inner tool_use events ("Reading X.py...", "Editing Y.py...") in meeting
# chat. Truncated at the start of every delegate so the tailer only sees
# the current run. Path is fixed (not session-keyed) because Anthropic tool
# use is serialized — only one delegate runs at a time.
STREAM_LOG_PATH = Path.home() / ".operator" / "debug" / "claude-stream.jsonl"

# repo_path (canonical) -> {"session_id": str, "worktree_path": str, "base_sha": str}
_SESSIONS: dict[str, dict] = {}


# Matches the inner `claude` CLI's auto-appended "[workdir: `...`]\n[repo: `...`]"
# trailer (backticks around values; sometimes two-space markdown line breaks
# before the newline). Anchored to end-of-string with optional trailing whitespace
# so we never strip something that looks like the trailer mid-document.
_CLAUDE_WORKTREE_TRAILER_RE = re.compile(
    r"\n+\[workdir:\s*`[^`]+`\][ \t]*\n\[repo:\s*`[^`]+`\][ \t]*\Z"
)


def _canon(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def _rel_home(path: str) -> str:
    """Render `path` with $HOME replaced by `~`. Keeps the absolute user
    directory out of the footer that's shipped to the LLM → meeting chat."""
    if not path:
        return path
    home = os.path.expanduser("~")
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def _is_git_repo(path: str) -> bool:
    r = subprocess.run(
        ["git", "-C", path, "rev-parse", "--git-dir"],
        capture_output=True,
    )
    return r.returncode == 0


def _head_sha(path: str) -> str:
    r = subprocess.run(
        ["git", "-C", path, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def _list_worktrees(repo_path: str) -> set[str]:
    r = subprocess.run(
        ["git", "-C", repo_path, "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
    )
    paths = set()
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            paths.add(_canon(line.removeprefix("worktree ").strip()))
    return paths


def _validate_repo(repo_path_raw: str):
    """Return (canon_path, None) on success, (None, error_text) on failure."""
    if not repo_path_raw:
        return None, (
            "Error: repo_path is required. Pass the absolute path to a local "
            "git repo — e.g. repo_path='/Users/you/code/marketing-site'. Ask the "
            "user which repo to work in if they haven't named one."
        )
    path = _canon(repo_path_raw)
    if not os.path.isdir(path):
        return None, f"Error: repo_path does not exist or is not a directory: {path}"
    if not _is_git_repo(path):
        return None, (
            f"Error: repo_path is not a git repo (no .git found): {path}. "
            f"Run `git init` in that folder (or point at a folder that's "
            f"already a git repo) and retry."
        )
    return path, None


@server.list_tools()
async def list_tools():
    return [
        types.Tool(
            name="delegate_to_claude_code",
            description=(
                "Delegate a coding task to Claude Code. Runs in an isolated "
                "git worktree under the named repo. First call in a chat for "
                "a given repo_path spins up a fresh sandbox; follow-up calls "
                "for the same repo_path reuse the same sandbox and "
                "conversation. Changes stay in the sandbox until the user "
                "runs land_worktree_changes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": (
                            "The coding task. Be specific: include file paths, "
                            "function names, or repo context."
                        ),
                    },
                    "repo_path": {
                        "type": "string",
                        "description": (
                            "Absolute path to a local git repo — e.g. "
                            "'/Users/you/code/marketing-site'. Required. On "
                            "the first delegation in a chat, ask the user "
                            "which repo to work in; never guess. Reuse the "
                            "same repo_path for follow-ups; switch only when "
                            "the user names a different one."
                        ),
                    },
                },
                "required": ["task", "repo_path"],
            },
        ),
        types.Tool(
            name="land_worktree_changes",
            description=(
                "Apply the sandbox's pending changes into the user's working "
                "tree. Runs against the active delegate session for the given "
                "repo_path. Fails cleanly (reports the conflict) if the user's "
                "tree has diverged."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_path": {
                        "type": "string",
                        "description": (
                            "Absolute path to the git repo — must match the "
                            "repo_path used in a prior delegate_to_claude_code "
                            "call this chat."
                        ),
                    },
                },
                "required": ["repo_path"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "delegate_to_claude_code":
        return await _delegate(arguments)
    if name == "land_worktree_changes":
        return await _land(arguments)
    raise ValueError(f"Unknown tool: {name}")


async def _delegate(arguments: dict):
    task = arguments.get("task", "").strip()
    if not task:
        return [types.TextContent(type="text", text="Error: task cannot be empty.")]

    repo_path, err = _validate_repo(arguments.get("repo_path", "").strip())
    if err:
        return [types.TextContent(type="text", text=err)]

    claude_path = shutil.which("claude")
    if not claude_path:
        return [
            types.TextContent(
                type="text",
                text=(
                    "Error: `claude` CLI not found on PATH. "
                    "Install it: https://docs.anthropic.com/en/docs/claude-code"
                ),
            )
        ]

    # bypassPermissions = Claude Code auto-approves every tool (incl. Bash). Safe
    # default here because the Operator layer already confirm-gates each delegate
    # call, and --worktree isolates file changes. acceptEdits blocks Bash.
    permission_mode = os.environ.get("CLAUDE_CODE_PERMISSION_MODE", "bypassPermissions")

    # Opportunistic cleanup of stale entries before we potentially add a new one.
    subprocess.run(
        ["git", "-C", repo_path, "worktree", "prune"],
        capture_output=True,
    )

    session = _SESSIONS.get(repo_path)
    resume_attempted = False

    if session:
        cwd = session["worktree_path"]
        cmd = [
            claude_path, "-p", task,
            "--resume", session["session_id"],
            "--permission-mode", permission_mode,
            "--output-format", "stream-json", "--verbose",
        ]
        resume_attempted = True
        worktrees_before = None
    else:
        cwd = repo_path
        cmd = [
            claude_path, "-p", task,
            "--worktree",
            "--permission-mode", permission_mode,
            "--output-format", "stream-json", "--verbose",
        ]
        worktrees_before = _list_worktrees(repo_path)

    data, err = await _run_claude(cmd, cwd)

    # Fall back to a fresh worktree if the resume session is no longer valid
    # (e.g. user manually deleted the worktree dir between calls).
    if err and resume_attempted and "No conversation found" in err:
        _SESSIONS.pop(repo_path, None)
        cwd = repo_path
        cmd = [
            claude_path, "-p", task,
            "--worktree",
            "--permission-mode", permission_mode,
            "--output-format", "stream-json", "--verbose",
        ]
        worktrees_before = _list_worktrees(repo_path)
        data, err = await _run_claude(cmd, cwd)

    if err:
        return [types.TextContent(type="text", text=err)]

    if data.get("is_error"):
        return [
            types.TextContent(
                type="text",
                text=f"Claude Code reported an error:\n{data.get('result', '')}",
            )
        ]

    new_session_id = data.get("session_id", "")

    if worktrees_before is not None and new_session_id:
        worktrees_after = _list_worktrees(repo_path)
        new_trees = worktrees_after - worktrees_before
        if new_trees:
            _SESSIONS[repo_path] = {
                "session_id": new_session_id,
                "worktree_path": next(iter(new_trees)),
                "base_sha": _head_sha(repo_path),
            }
    elif session and new_session_id:
        # Resume returned — session_id is the same, just refresh in case
        session["session_id"] = new_session_id

    result = data.get("result", "")
    # The inner `claude` CLI appends its own "[workdir: `/tmp/...`]\n[repo:
    # `name`]" trailer (with backticks, sometimes trailing two-space markdown
    # line breaks) when run with --worktree. Strip it — same reasoning as the
    # wrapper-side footer we removed: the LLM doesn't need it (worktree routing
    # lives in _SESSIONS keyed by repo_path), and the streaming paragraph-flush
    # would leak it as its own noise message in chat.
    result = _CLAUDE_WORKTREE_TRAILER_RE.sub("", result).rstrip()
    return [types.TextContent(type="text", text=result)]


async def _run_claude(cmd: list[str], cwd: str):
    """Return (parsed_result_event_or_None, error_text_or_None).

    Spawns claude in stream-json mode and reads stdout line-by-line. Every
    line is appended to STREAM_LOG_PATH (truncated at the start of this
    call) so the operator ChatRunner can tail it for live progress. The
    final `{"type":"result", ...}` event is extracted and returned with the
    same shape the old `--output-format json` path produced (top-level
    keys: `session_id`, `result`, `is_error`).
    """
    STREAM_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Truncate so the tailer only sees this call's events.
    STREAM_LOG_PATH.write_text("")

    try:
        # Explicitly close stdin: otherwise the inner `claude` CLI inherits the
        # MCP server's stdin (the JSON-RPC stream from operator's parent), its
        # startup stdin-probe sees that traffic, waits 3s, then bails with code 1
        # ("no stdin data received in 3s, proceeding without it"). DEVNULL gives
        # the probe an immediate EOF so it skips that path entirely.
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return None, f"Error launching claude CLI: {exc}"

    final_event = None
    non_json_lines: list[str] = []

    async def _drain_stdout():
        nonlocal final_event
        with STREAM_LOG_PATH.open("a") as side_log:
            async for raw_line in proc.stdout:
                line = raw_line.decode(errors="replace").rstrip("\n")
                if not line:
                    continue
                side_log.write(line + "\n")
                side_log.flush()  # tailer reads this file as it grows
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    # `claude --resume <bad-id>` and similar paths print plain
                    # text on stdout instead of JSON. Capture for the error path.
                    non_json_lines.append(line)
                    continue
                if event.get("type") == "result":
                    final_event = event

    drain_task = asyncio.create_task(_drain_stdout())
    stderr_bytes = await proc.stderr.read()
    await proc.wait()
    await drain_task

    raw_err = stderr_bytes.decode(errors="replace").strip()

    if proc.returncode != 0:
        combined = raw_err or "\n".join(non_json_lines) or "(no error output)"
        return None, f"Claude Code exited with code {proc.returncode}.\n{combined}"

    if final_event is None:
        # No result event arrived but the process exited 0 — fall back to any
        # plain-text stdout (e.g. resume-id-not-found message) or stderr.
        fallback = "\n".join(non_json_lines) or raw_err
        return None, fallback[:2000] or "Claude Code returned no result event."

    return final_event, None


async def _land(arguments: dict):
    repo_path, err = _validate_repo(arguments.get("repo_path", "").strip())
    if err:
        return [types.TextContent(type="text", text=err)]

    session = _SESSIONS.get(repo_path)
    if not session:
        return [
            types.TextContent(
                type="text",
                text=(
                    f"No active delegate session for {repo_path}. Run "
                    f"delegate_to_claude_code against this repo first."
                ),
            )
        ]

    worktree = session["worktree_path"]
    base_sha = session["base_sha"]

    if not os.path.isdir(worktree):
        _SESSIONS.pop(repo_path, None)
        return [
            types.TextContent(
                type="text",
                text=f"Sandbox at {worktree} no longer exists — session cleared.",
            )
        ]

    # Stage everything in the worktree so untracked files (Claude Code often
    # leaves new files untracked) land in the diff. Safe: the worktree is a
    # throwaway sandbox, staging it has no visible effect elsewhere.
    subprocess.run(
        ["git", "-C", worktree, "add", "-A"],
        capture_output=True,
    )

    # `git diff --cached <base_sha>` inside the worktree: staged tree vs the
    # repo's HEAD at the moment the worktree was created.
    diff_proc = subprocess.run(
        ["git", "-C", worktree, "diff", "--cached", base_sha],
        capture_output=True,
    )
    if diff_proc.returncode != 0:
        return [
            types.TextContent(
                type="text",
                text=(
                    f"Could not diff sandbox against base {base_sha[:8]}:\n"
                    f"{diff_proc.stderr.decode(errors='replace')}"
                ),
            )
        ]

    diff = diff_proc.stdout
    if not diff.strip():
        return [
            types.TextContent(
                type="text",
                text="No changes to land — sandbox matches the repo's base commit.",
            )
        ]

    apply_proc = subprocess.run(
        ["git", "-C", repo_path, "apply", "--index", "-"],
        input=diff,
        capture_output=True,
    )
    if apply_proc.returncode != 0:
        # Retry without --index in case the repo's index is dirty
        apply_proc = subprocess.run(
            ["git", "-C", repo_path, "apply", "-"],
            input=diff,
            capture_output=True,
        )

    if apply_proc.returncode != 0:
        return [
            types.TextContent(
                type="text",
                text=(
                    "Could not apply sandbox changes — your working tree has "
                    "likely diverged. Details:\n"
                    f"{apply_proc.stderr.decode(errors='replace')}"
                ),
            )
        ]

    # Count what landed
    stat = subprocess.run(
        ["git", "-C", repo_path, "diff", "--stat", "--cached"],
        capture_output=True,
        text=True,
    )
    summary = stat.stdout.strip() or "applied (no summary available)"

    return [
        types.TextContent(
            type="text",
            text=(
                f"Landed sandbox changes into {repo_path}.\n\n{summary}\n\n"
                f"Review with `git -C {repo_path} status` and commit when ready."
            ),
        )
    ]


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
