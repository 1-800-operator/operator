"""
Codex MCP-server LLM provider.

Wraps the bundled `codex mcp-server` (running as a normal MCP server in
operator's MCPClient) as an LLMProvider. Each meeting maintains a single
codex session via threadId — the first complete() call invokes the `codex`
tool with the user prompt; subsequent calls invoke `codex-reply` with the
stored threadId. This makes Codex the agent's brain (analogous to the
claude track's `claude -p` subprocess), but we don't manage a subprocess
directly — MCPClient owns the lifecycle.

The provider does NOT own any subprocess, MCP transport, or async loop.
It just holds:
  - A reference to the MCPClient (late-bound; chat_runner sets it after
    MCPClient.connect_all() succeeds).
  - The current thread_id (None on first turn).
  - The system prompt to pass on the first call (developer-instructions,
    which Codex stores per-thread; subsequent turns reuse stored prompt).

Subscription-vs-API billing: enforced upstream (env-clear of
OPENAI_API_KEY in the codex agent's mcp_servers.codex.env block + the
preflight `codex login status` check). The provider trusts that.

Architecturally different from openai/anthropic providers (no model field,
no tool schemas — Codex owns its own tool surface) but parallels claude_cli
in spirit (CLI runs the agentic loop, we transport text in/out).
"""
import logging

from _1_800_operator.pipeline.providers.base import (
    LLMProvider,
    ProviderResponse,
)

log = logging.getLogger(__name__)


# Default knobs for the codex tool call. The agent config supplies
# overrides; we use these when the agent leaves them blank. on-request
# (Codex's model judges escalation) is far less noisy than untrusted
# (Codex elicits every non-allowlisted command) for chat-driven use.
DEFAULT_APPROVAL_POLICY = "on-request"
DEFAULT_SANDBOX = "read-only"

# Tool name codex MCP server registers. Operator namespaces it as
# `codex__codex` (`<server>__<tool>`). The reply tool is `codex-reply`,
# namespaced as `codex__codex-reply`. Hyphen survives operator's `__`
# split (verified phase-0).
_TOOL_NAME_START = "codex__codex"
_TOOL_NAME_REPLY = "codex__codex-reply"

# Fragments codex uses when a thread vanishes (subprocess restart, server
# crash). Lowercase, substring-match. On hit we clear the threadId and
# fall back to a fresh `codex` invocation so the meeting recovers.
_DEAD_THREAD_HINTS = (
    "thread not found",
    "session not connected",
    "session disconnected",
    "unknown threadid",
)


class CodexMCPProvider(LLMProvider):
    """Codex MCP server as the meeting agent's brain.

    Late-bind for the MCP client: built before MCPClient.connect_all() runs,
    wired by chat_runner once the codex MCP server is reachable. Calling
    complete() before the wire-up raises a clear RuntimeError.
    """

    def __init__(self, *, append_developer_instructions=None,
                 approval_policy=None, sandbox=None, cwd=None):
        # Set by chat_runner._wire_codex_elicitation after MCPClient is up.
        self._mcp_client = None
        # Stored after the first successful `codex` invocation; used on every
        # subsequent turn until cleared by a thread-died fallback.
        self._thread_id: str | None = None
        # Passed as `developer-instructions` on the first codex call only.
        # Codex stores it per-thread; we don't re-send on codex-reply (codex
        # would ignore it anyway). Mid-meeting system_prompt edits don't
        # take effect until the next meeting, mirroring claude_cli's
        # spawn-time semantics.
        self._developer_instructions = append_developer_instructions or None
        self._approval_policy = approval_policy or DEFAULT_APPROVAL_POLICY
        self._sandbox = sandbox or DEFAULT_SANDBOX
        self._cwd = cwd

    def set_mcp_client(self, mcp_client):
        """Late-bind the MCP client. Called by chat_runner after connect_all()."""
        self._mcp_client = mcp_client

    def complete(self, system, messages, model=None, max_tokens=None,
                 tools=None, retry_rate_limits=True):
        """Forward the latest user message to codex and return its synthesized reply.

        `system` is consumed once, on the first turn, as
        `developer-instructions`; codex stores it per-thread. `model`,
        `max_tokens`, and `tools` are ignored — codex picks its own model
        and runs its own tool loop.

        On thread-died errors (server restart, connection drop) we clear
        the stored threadId and retry once with a fresh `codex` call so
        the meeting recovers transparently.
        """
        if self._mcp_client is None:
            raise RuntimeError(
                "CodexMCPProvider not wired — set_mcp_client() must be called "
                "before complete(). chat_runner._wire_codex_elicitation does "
                "this after MCPClient.connect_all() returns."
            )
        prompt = _last_user_text(messages)
        if not prompt:
            # Empty turn shouldn't happen (LLMClient gates this), but if it
            # does, return a no-op response rather than confusing codex.
            return ProviderResponse(text="", tool_calls=[], stop_reason="end")

        try:
            content = self._invoke(prompt, fresh=False)
        except _DeadThreadError:
            log.warning("CodexMCPProvider: thread vanished — restarting fresh")
            self._thread_id = None
            content = self._invoke(prompt, fresh=True)
        return ProviderResponse(text=content, tool_calls=[], stop_reason="end")

    def complete_stream(self, system, messages, model, max_tokens):
        # Codex MCP-server returns a synthesized final reply per call —
        # no token-streaming surface. Fall back to non-streaming behavior.
        resp = self.complete(system, messages, model, max_tokens)
        if resp.text:
            yield resp.text

    def complete_streaming(self, system, messages, model=None, max_tokens=None,
                           tools=None, on_paragraph=None, retry_rate_limits=True):
        # No incremental output from codex MCP; return the full reply.
        # Callers using on_paragraph still get the final paragraph flushed
        # if they want via the response text.
        resp = self.complete(system, messages, model, max_tokens, tools=tools,
                             retry_rate_limits=retry_rate_limits)
        if on_paragraph and resp.text:
            from _1_800_operator.pipeline.providers.base import flush_paragraphs
            flush_paragraphs(resp.text, on_paragraph, force_final=True)
        return resp

    def warmup(self, model=None):
        # Codex MCP server warmup is handled by MCPClient.connect_all
        # (tool discovery handshake). Nothing extra to do here.
        return

    # ── Internal ──────────────────────────────────────────────────────

    def _invoke(self, prompt, *, fresh):
        """Call `codex` (fresh thread) or `codex-reply` (existing thread)."""
        from _1_800_operator.pipeline.mcp_client import MCPToolError
        if fresh or self._thread_id is None:
            args = {
                "prompt": prompt,
                "approval-policy": self._approval_policy,
                "sandbox": self._sandbox,
            }
            if self._cwd:
                args["cwd"] = self._cwd
            if self._developer_instructions:
                args["developer-instructions"] = self._developer_instructions
            tool = _TOOL_NAME_START
        else:
            args = {"threadId": self._thread_id, "prompt": prompt}
            tool = _TOOL_NAME_REPLY

        try:
            raw = self._mcp_client.execute_tool(tool, args)
        except MCPToolError as e:
            if _looks_like_dead_thread(str(e)) and not fresh:
                raise _DeadThreadError() from e
            raise

        # Codex MCP returns the user-facing reply as joined text content
        # (what `raw` already holds) AND a structuredContent envelope
        # `{threadId, content}`. The threadId only lives in structured
        # content; pull it from the per-server snapshot MCPClient kept
        # after the call.
        sc = self._mcp_client.last_structured_content("codex")
        thread_id = sc.get("threadId") if isinstance(sc, dict) else None
        if thread_id:
            self._thread_id = thread_id
        return raw


class _DeadThreadError(Exception):
    """Internal signal: codex reports thread/session is gone, retry fresh."""


def _last_user_text(messages):
    for m in reversed(messages or []):
        if m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, str):
                return content
    return ""


def _looks_like_dead_thread(msg):
    msg_lower = msg.lower()
    return any(h in msg_lower for h in _DEAD_THREAD_HINTS)
