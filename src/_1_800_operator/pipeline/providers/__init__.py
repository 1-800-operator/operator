from _1_800_operator.pipeline.providers.base import (
    LLMProvider,
    ContextOverflowError,
    ToolCall,
    ProviderResponse,
)
from _1_800_operator.pipeline.providers.openai import OpenAIProvider
from _1_800_operator.pipeline.providers.anthropic import AnthropicProvider
from _1_800_operator.pipeline.providers.claude_cli import ClaudeCLIProvider


def build_provider():
    """Build the LLMProvider selected by config.LLM_PROVIDER.

    Called by the app-level entry points (__main__, runner, docker entrypoint)
    so the choice of backend lives in one place.
    """
    from _1_800_operator import config
    name = config.LLM_PROVIDER
    if name == "openai":
        from openai import OpenAI
        if not config.OPENAI_API_KEY:
            raise RuntimeError(
                "llm.provider is 'openai' but OPENAI_API_KEY is not set in .env"
            )
        return OpenAIProvider(OpenAI(api_key=config.OPENAI_API_KEY))
    if name == "anthropic":
        from anthropic import Anthropic
        if not config.ANTHROPIC_API_KEY:
            raise RuntimeError(
                "llm.provider is 'anthropic' but ANTHROPIC_API_KEY is not set in .env"
            )
        return AnthropicProvider(Anthropic(api_key=config.ANTHROPIC_API_KEY))
    if name == "claude_cli":
        # Track A: claude IS the LLM, run via the `claude` CLI subprocess
        # under the user's Claude Max subscription. SYSTEM_PROMPT
        # (framework + user system_prompt) is appended to claude's
        # default system prompt via --append-system-prompt at spawn time.
        # permission_handler is wired in step 5c — for now claude follows
        # its native ~/.claude/settings.json permission rules.
        # cwd mirrors `claude` itself: spawn in the user's invocation dir
        # so "this codebase" resolves without an `ls $HOME` round-trip.
        import os
        return ClaudeCLIProvider(
            append_system_prompt=config.SYSTEM_PROMPT or None,
            cwd=os.getcwd(),
        )
    raise ValueError(
        f"unknown llm.provider: {name!r} (expected 'openai', 'anthropic', or 'claude_cli')"
    )


__all__ = [
    "LLMProvider",
    "ContextOverflowError",
    "ToolCall",
    "ProviderResponse",
    "OpenAIProvider",
    "AnthropicProvider",
    "ClaudeCLIProvider",
    "build_provider",
]
