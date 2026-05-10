from _1_800_operator.pipeline.providers.base import (
    LLMProvider,
    ToolCall,
    ProviderResponse,
)
from _1_800_operator.pipeline.providers.claude_cli import ClaudeCLIProvider


def build_provider(resume_session_id=None):
    """Build the LLM provider — claude is operator v1's only brain.

    The `claude` CLI subprocess runs under the user's Claude Max subscription
    and reads its own ~/.claude/ hierarchy (CLAUDE.md, skills, MCPs, settings)
    natively. cwd mirrors `claude` itself: spawn in the user's invocation dir
    so "this codebase" resolves without an `ls $HOME` round-trip.

    `resume_session_id` is the Claude Code session ID to bridge into this
    meeting. When the plugin slash command runs `operator slip claude
    --resume-session ${CLAUDE_SESSION_ID} <url>`, that id arrives here and
    the very first @mention spawns with `--resume <id>` so the meeting
    brain inherits the user's pre-meeting context. Terminal-direct
    invocation omits the flag and a fresh session is born on first
    @mention.
    """
    import os
    return ClaudeCLIProvider(cwd=os.getcwd(), resume_session_id=resume_session_id)


__all__ = [
    "LLMProvider",
    "ToolCall",
    "ProviderResponse",
    "ClaudeCLIProvider",
    "build_provider",
]
