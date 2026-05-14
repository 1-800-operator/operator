from _1_800_operator.pipeline.providers.base import (
    LLMProvider,
    ToolCall,
    ProviderResponse,
)
from _1_800_operator.pipeline.providers.claude_cli import ClaudeCLIProvider


def build_provider(resume_session_id=None, session_dir=None):
    """Build the LLM provider — claude is operator v1's only brain.

    Spawns long-lived interactive `claude --dangerously-skip-permissions`
    over a PTY and reads its replies via the operator-plugin's hook
    scripts (Stop / PreToolUse / PostToolUseFailure / PermissionDenied /
    StopFailure). cwd mirrors `claude` itself: spawn in the user's
    invocation dir so `--resume` finds the session JSONL and the project's
    own CLAUDE.md / hooks load for free.

    `resume_session_id` is the Claude Code session id to bridge into the
    meeting. When the plugin slash command runs `operator slip claude
    --resume-session ${CLAUDE_SESSION_ID} <url>`, that id arrives here
    and the spawn passes `--resume <id>` so the meeting brain inherits
    the caller's pre-meeting context. Terminal-direct invocation omits
    the flag and a fresh session is born on the first @mention.

    `session_dir` is where the plugin hook scripts write replies.jsonl /
    tools.jsonl / errors.jsonl / ready.flag. Defaults to a fresh
    `~/.operator/sessions/<uuid>/`. The provider exports OPERATOR_SESSION_DIR
    into the inner-claude env so the hook scripts (which run as
    subprocesses of claude) can find it.
    """
    import os
    return ClaudeCLIProvider(
        cwd=os.getcwd(),
        resume_session_id=resume_session_id,
        session_dir=session_dir,
    )


__all__ = [
    "LLMProvider",
    "ToolCall",
    "ProviderResponse",
    "ClaudeCLIProvider",
    "build_provider",
]
