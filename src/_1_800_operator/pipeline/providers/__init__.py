from _1_800_operator.pipeline.providers.base import (
    LLMProvider,
    ToolCall,
    ProviderResponse,
)
from _1_800_operator.pipeline.providers.claude_cli import ClaudeCLIProvider


def build_provider():
    """Build the LLM provider — claude is operator v1's only brain.

    The `claude` CLI subprocess runs under the user's Claude Max subscription
    and reads its own ~/.claude/ hierarchy (CLAUDE.md, skills, MCPs, settings)
    natively. cwd mirrors `claude` itself: spawn in the user's invocation dir
    so "this codebase" resolves without an `ls $HOME` round-trip.
    """
    import os
    return ClaudeCLIProvider(cwd=os.getcwd())


__all__ = [
    "LLMProvider",
    "ToolCall",
    "ProviderResponse",
    "ClaudeCLIProvider",
    "build_provider",
]
