"""
MCP client manager — connects to configured MCP servers, discovers tools,
and executes tool calls.

Bridges the async MCP SDK into synchronous code via a dedicated event loop
thread. Each MCP server runs as a long-lived async task (required because
the MCP SDK's stdio_client uses anyio task groups that must stay in one task).
All public methods are synchronous and safe to call from any thread.
"""
import asyncio
import json
import logging
import os
import threading

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from _1_800_operator import config
from _1_800_operator.pipeline.guardrails import is_text_file_path, log_rejection
from _1_800_operator.pipeline.oauth_cache import (
    mcp_remote_cache_dir as _mcp_remote_cache_dir,
    oauth_cache_exists as _oauth_cache_exists,
)

log = logging.getLogger(__name__)


def _summarize_tool_args(arguments: dict) -> str:
    """Return a log-safe summary of tool arguments.

    Default: keys + value types + string lengths only, no values.
    Full values are dumped only when OPERATOR_LOG_TOOL_ARGS=1 is set
    (opt-in escape hatch for debugging). Tool arguments often contain
    repo paths, PR titles, issue bodies, or pasted snippets — treat
    them as potentially sensitive.
    """
    if os.environ.get("OPERATOR_LOG_TOOL_ARGS") == "1":
        return json.dumps(arguments, default=str)
    parts = []
    for k, v in arguments.items():
        if isinstance(v, str):
            parts.append(f"{k}=str[{len(v)}]")
        elif isinstance(v, (list, tuple)):
            parts.append(f"{k}={type(v).__name__}[{len(v)}]")
        elif isinstance(v, dict):
            parts.append(f"{k}=dict[{len(v)}]")
        else:
            parts.append(f"{k}={type(v).__name__}")
    return "{" + ", ".join(parts) + "}"


class MCPToolError(Exception):
    """Raised when an MCP tool call fails."""
    pass


def disabled_server_for_tool(tool_name: str) -> str | None:
    """Return the server name if tool_name's namespaced prefix is a disabled server.

    Tool names are registered as "<server>__<tool>" (see MCPClient._tools). When
    the LLM calls a tool that isn't in _tools, we want to distinguish "the server
    is configured but disabled" from "the tool really doesn't exist" so the error
    back to the LLM carries actionable remediation for the user.
    """
    if "__" not in tool_name:
        return None
    prefix = tool_name.split("__", 1)[0]
    return prefix if prefix in config.DISABLED_MCP_SERVERS else None


# Consecutive tool-call failures per server before we disable it for the session.
RUNTIME_FAILURE_THRESHOLD = 3

# Substring signals that an MCP tool-error text is almost certainly an auth
# failure. Used by record_tool_result's sniff to upgrade a tripped server's
# kind from "runtime_failure" to "auth_failed" so the wizard/banner can
# render a re-auth prompt instead of a generic "check logs" message.
#
# Grounded in empirical capture against bundled MCPs (see
# tests/probe_auth_errors.py and docs/mcp-auth-errors.md) plus documented
# patterns for slack/sentry/salesforce/google. Whitespace-padded numeric
# codes keep the sniff from matching any random payload that happens to
# quote "401" in prose.
#
# OAuth-over-mcp-remote servers (Linear today, google-* later) do NOT
# surface auth failures as tool errors — they stall initialization waiting
# for browser authorization. The cache-path check (15.7.3) is the only
# real signal for those; this sniff is a no-op on their error text.
_AUTH_ERROR_PATTERNS = (
    " 401 ", ": 401", "(401)", "status 401",
    " 403 ", ": 403", "(403)", "status 403",
    "bad credentials",           # GitHub
    "forbidden",                 # Figma, generic HTTP
    "unauthorized",              # generic
    "invalid_auth", "not_authed", "token_expired", "token revoked",  # Slack
    "invalid token", "invalid api key", "invalid_grant",             # Sentry / OAuth / generic
    "authentication required", "authentication failed",
    "invalid_session_id",        # Salesforce
    "unauthenticated",           # Google APIs (gRPC)
)


def _looks_like_auth_error(error_text: str | None) -> bool:
    """True iff error_text contains a known auth-failure substring.

    Case-insensitive. Grounded-but-incomplete — see _AUTH_ERROR_PATTERNS.
    False negatives are acceptable: the fallback is the LLM relaying the
    raw error to the user verbatim, so missing a sniff downgrades UX
    (generic "server disabled" message) rather than breaking anything.
    """
    if not error_text:
        return False
    lowered = error_text.lower()
    return any(p in lowered for p in _AUTH_ERROR_PATTERNS)


def _classify_startup_failure(exc: Exception, srv_config: dict) -> dict:
    """Classify a startup exception into a structured failure record.

    Shape: {kind, fix, raw} (plus `vars` for kind="missing_creds").
    Downstream consumers (llm.inject_mcp_status, wizard status screen,
    15.7.2 chat banner) dispatch on `kind` and surface `fix` as the
    user-facing repair hint.

    Precedence: if the server's resolved env had unfilled ${VAR} refs,
    "missing_creds" wins over whatever the stdio transport happened to
    raise — the missing secret is almost always the actual root cause
    even if the binary crashes with something noisier on top.
    """
    cmd = srv_config.get("command", "?")
    missing_vars = list(srv_config.get("missing_vars") or [])

    # Unwrap anyio/asyncio ExceptionGroups — stdio_client wraps subprocess
    # failures in a TaskGroup, so the first-layer exception is useless noise.
    inner = exc
    while isinstance(inner, BaseExceptionGroup) and inner.exceptions:
        inner = inner.exceptions[0]
    raw = f"{type(inner).__name__}: {inner}"

    if missing_vars:
        return {
            "kind": "missing_creds",
            "vars": missing_vars,
            "fix": (
                f"set {', '.join(missing_vars)} in .env and re-run build — "
                f"server '{cmd}' is almost certainly crashing because of the empty credential"
            ),
            "raw": raw,
        }
    if isinstance(inner, FileNotFoundError):
        return {
            "kind": "binary_missing",
            "fix": (
                f"the command '{cmd}' was not found — check the 'command' field in "
                f"this agent's config.yaml, or ensure the binary is on PATH"
            ),
            "raw": raw,
        }
    if isinstance(inner, TimeoutError):
        return {
            "kind": "startup_timeout",
            "fix": (
                f"'{cmd}' did not respond within the startup timeout — the binary "
                f"may have crashed or is waiting for input; try running it manually"
            ),
            "raw": raw,
        }
    # Subprocess exited before MCP handshake completed (e.g. `echo` or a server
    # that crashes on startup). anyio raises this from inside stdio_client.
    msg = str(inner).lower()
    if "process exited" in msg or "broken pipe" in msg or "eof" in msg or "connection closed" in msg:
        return {
            "kind": "handshake_crash",
            "fix": (
                f"'{cmd}' exited before the MCP handshake completed — the binary "
                f"likely crashed or is not an MCP server; try running the command "
                f"manually to see its output"
            ),
            "raw": raw,
        }
    return {"kind": "unknown", "fix": raw, "raw": raw}


class MCPClient:
    """Manages connections to one or more MCP servers (stdio transport).

    Usage:
        client = MCPClient()
        client.connect_all()          # starts servers, discovers tools
        tools = client.get_openai_tools()  # OpenAI function-calling format
        result = client.execute_tool("linear__create_issue", {...})
        client.shutdown()
    """

    def __init__(self):
        # server_name -> _ServerHandle
        self._servers: dict[str, _ServerHandle] = {}
        # namespaced_tool_name -> { server_name, mcp_tool }
        self._tools: dict[str, dict] = {}
        # server_name -> structured failure record (populated by connect_all).
        # Shape: {kind, fix, raw, [vars]} — see _classify_startup_failure.
        # Renamed from `failed_servers` in 15.7.1 so consumers that still
        # expect `dict[str, str]` break loudly instead of stringifying a dict.
        self.startup_failures: dict[str, dict] = {}
        # server_name -> consecutive tool-call failures since last success
        self._consecutive_errors: dict[str, int] = {}
        # server_name set — any sub-threshold failure on this server matched
        # an auth-error substring. Checked at trip time so the runtime_failures
        # kind reflects the auth signal even if the final call to trip it
        # didn't itself carry auth text. Cleared on successful tool call.
        self._auth_error_seen: set[str] = set()
        # server_name -> structured runtime failure (populated when a server
        # trips the runtime threshold). Shape: {kind, reason}. `kind` is
        # "runtime_failure" by default; 15.7.1d's auth sniff upgrades it to
        # "auth_failed" when the tool-error text matches.
        self.runtime_failures: dict[str, dict] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None

    def connect_all(self) -> list[str]:
        """Start all configured MCP servers and discover their tools.

        Returns list of discovered tool names. Logs and skips servers
        that fail to start; structured failure records land in
        self.startup_failures for downstream UI surfaces.

        Servers with auth="oauth" get a cache-path pre-check before any
        subprocess is spawned — missing token cache → kind="oauth_needed"
        and a skip, so OAuth can never hang meeting join waiting for a
        browser popup. User runs `operator auth <name>` once to seed
        the cache.
        """
        self._start_loop()
        tool_names = []
        for name, srv_config in config.MCP_SERVERS.items():
            if srv_config.get("auth") == "oauth":
                auth_url = srv_config.get("auth_url", "")
                if not _oauth_cache_exists(auth_url):
                    self.startup_failures[name] = {
                        "kind": "oauth_needed",
                        "fix": f"run `operator auth {name}` once to authorize — token is cached after",
                        "auth_url": auth_url,
                        "raw": "oauth cache missing",
                    }
                    log.warning(
                        f"MCP server '{name}' skipped — oauth cache absent for {auth_url!r}; "
                        f"run `operator auth {name}`"
                    )
                    continue
            try:
                tools = self._connect_server(name, srv_config)
                tool_names.extend(tools)
                log.info(f"MCP server '{name}' connected — {len(tools)} tools")
            except Exception as e:
                info = _classify_startup_failure(e, srv_config)
                self.startup_failures[name] = info
                log.error(
                    f"MCP USER CONFIG: server '{name}' failed to start "
                    f"[{info['kind']}] — {info['fix']}"
                )
        return tool_names

    def get_openai_tools(self) -> list[dict]:
        """Return all discovered tools in OpenAI function-calling format.

        Tools from servers in runtime_failures are omitted so the LLM stops
        trying them once a server has been tripped mid-session.
        """
        result = []
        for tool_name, info in self._tools.items():
            if info["server_name"] in self.runtime_failures:
                continue
            mcp_tool = info["mcp_tool"]
            result.append({
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": mcp_tool.description or "",
                    "parameters": mcp_tool.inputSchema,
                },
            })
        return result

    def record_tool_result(
        self,
        server_name: str,
        success: bool,
        error_text: str | None = None,
    ) -> bool:
        """Track per-server tool-call outcomes; trip the server at N failures.

        Called from execute_tool (both branches) and from chat_runner's timeout
        path (which never re-enters execute_tool). Returns True iff *this call*
        just tripped the server into the disabled state — the caller uses that
        signal to announce once in chat and reinject MCP status.

        When a tripping failure's error_text matches a known auth pattern,
        the runtime_failures entry is tagged kind="auth_failed" (with the
        matched error snippet preserved). The wizard status screen and chat
        banner dispatch on kind to render re-auth prompts vs generic "check
        logs" copy. If error_text is None or no pattern matches, we fall
        back to kind="runtime_failure" and trust the LLM to relay the raw
        error to the user verbatim on the triggering turn.

        Note: tracks auth signal even on sub-threshold failures so a server
        that trips on its Nth call still surfaces as auth_failed (not
        runtime_failure) if any of those N calls looked like auth.
        """
        if success:
            self._consecutive_errors[server_name] = 0
            self._auth_error_seen.discard(server_name)
            return False
        if server_name in self.runtime_failures:
            return False  # already disabled, don't re-announce
        if _looks_like_auth_error(error_text):
            self._auth_error_seen.add(server_name)
        count = self._consecutive_errors.get(server_name, 0) + 1
        self._consecutive_errors[server_name] = count
        if count >= RUNTIME_FAILURE_THRESHOLD:
            if server_name in self._auth_error_seen:
                reason = (
                    f"{count} consecutive tool-call failures this session, "
                    f"at least one matched an auth-error pattern (401/403/unauthorized/…)"
                )
                self.runtime_failures[server_name] = {"kind": "auth_failed", "reason": reason}
            else:
                reason = f"{count} consecutive tool-call failures this session"
                self.runtime_failures[server_name] = {"kind": "runtime_failure", "reason": reason}
            log.error(f"MCP server '{server_name}' disabled — {reason}")
            return True
        log.warning(f"MCP server '{server_name}' failure {count}/{RUNTIME_FAILURE_THRESHOLD}")
        return False

    def execute_tool(self, tool_name: str, arguments: dict) -> str:
        """Execute a tool call and return the result as a string.

        tool_name is the namespaced name like "linear__create_issue".
        Raises MCPToolError on failure.
        """
        if tool_name not in self._tools:
            disabled = disabled_server_for_tool(tool_name)
            if disabled:
                raise MCPToolError(
                    f"Tool '{tool_name}' unavailable — the '{disabled}' MCP server is "
                    f"disabled in this agent's config. Tell the user to enable it via "
                    f"`operator build` or by setting `enabled: true` under "
                    f"mcp_servers.{disabled} in ~/.operator/agents/<name>/config.yaml."
                )
            raise MCPToolError(f"Unknown tool: {tool_name}")

        info = self._tools[tool_name]
        server_name = info["server_name"]

        if server_name in self.runtime_failures:
            raise MCPToolError(
                f"Server '{server_name}' has been disabled for this session after repeated failures. "
                f"This tool is unavailable. Do not retry; tell the user."
            )

        original_name = tool_name.split("__", 1)[1] if "__" in tool_name else tool_name
        handle = self._servers[server_name]

        # Strip 'limit' the LLM injects unprompted on Linear list calls —
        # the model ignores prompt hints, so enforce it here.
        if server_name == "linear" and "limit" in arguments:
            log.info(f"MCP stripping unprompted limit={arguments['limit']} from {tool_name}")
            arguments = {k: v for k, v in arguments.items() if k != "limit"}

        # Block binary file reads before the MCP call fires.
        # Works for any server exposing get_file_contents, not just GitHub.
        if original_name == "get_file_contents" and "path" in arguments:
            if not is_text_file_path(arguments["path"]):
                reason = f"Blocked: '{arguments['path']}' has a non-text file extension — only text files are allowed"
                log_rejection(tool_name, arguments, reason, "pre-execution")
                raise MCPToolError(reason)

        effective_timeout = self._effective_timeout_for(tool_name)
        log.info(f"MCP executing tool={tool_name} server={server_name} timeout={effective_timeout}s")
        log.debug(f"MCP tool arguments: {_summarize_tool_args(arguments)}")

        try:
            result = handle.call_tool(original_name, arguments, timeout=effective_timeout)
        except MCPToolError:
            raise
        except Exception as e:
            log.error(f"MCP tool execution failed: {e}")
            raise MCPToolError(f"Tool '{tool_name}' failed: {e}") from e

        return result

    def _effective_timeout_for(self, tool_name: str) -> int:
        """Resolve the effective per-call timeout: explicit override → ship default → global fallback.

        Kept separate from the public `tool_timeout_for` (which only reports
        the explicit override and returns None when absent — callers rely on
        that to distinguish 'user set this' from 'using defaults').
        """
        override = self.tool_timeout_for(tool_name)
        if override is not None:
            return override
        server_name = self.server_for_tool(tool_name)
        if server_name and server_name in config.DEFAULT_TOOL_TIMEOUTS:
            return config.DEFAULT_TOOL_TIMEOUTS[server_name]
        return config.TOOL_TIMEOUT_SECONDS

    def server_for_tool(self, tool_name: str) -> str | None:
        """Resolve a namespaced tool name to its server name, or None if unknown."""
        info = self._tools.get(tool_name)
        return info["server_name"] if info else None

    def tool_timeout_for(self, tool_name: str) -> int | None:
        """Return per-server tool_timeout_seconds override, or None to use the global default.

        Lets a slow MCP (e.g. claude-code running a multi-minute task)
        carry its own timeout in the bot's config.yaml without bumping the
        global default for quick read tools.
        """
        server_name = self.server_for_tool(tool_name)
        if not server_name:
            return None
        return config.MCP_SERVERS.get(server_name, {}).get("tool_timeout_seconds")

    def resolve_github_user(self) -> str | None:
        """Call github__get_me to resolve the authenticated GitHub login.

        Returns the login string (e.g. 'dufis1') or None if unavailable.
        """
        tool_name = "github__get_me"
        if tool_name not in self._tools:
            return None
        try:
            result = self.execute_tool(tool_name, {})
            # Result is JSON text with a "login" field
            import json as _json
            data = _json.loads(result)
            login = data.get("login")
            if login:
                log.info(f"MCP resolved GitHub user: {login}")
            return login
        except Exception as e:
            log.warning(f"MCP resolve_github_user failed: {e}")
            return None

    def shutdown(self):
        """Disconnect all servers and stop the event loop thread."""
        if not self._loop:
            return
        for name, handle in list(self._servers.items()):
            try:
                handle.stop()
            except Exception as e:
                log.warning(f"MCP server '{name}' cleanup error: {e}")
        self._servers.clear()
        self._tools.clear()
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._loop_thread:
            self._loop_thread.join(timeout=5)
        self._loop = None
        self._loop_thread = None
        log.info("MCP client shutdown complete")

    # ── Internal ──────────────────────────────────────────────────────

    def _start_loop(self):
        """Start a dedicated asyncio event loop in a daemon thread."""
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name="mcp-event-loop",
        )
        self._loop_thread.start()

    def _connect_server(self, name, srv_config):
        """Start a server task and wait for tool discovery to complete."""
        handle = _ServerHandle(name, srv_config, self._loop)
        handle.start()
        self._servers[name] = handle

        discovered = []
        for tool in handle.tools:
            namespaced = f"{name}__{tool.name}"
            self._tools[namespaced] = {
                "server_name": name,
                "mcp_tool": tool,
            }
            discovered.append(namespaced)
        return discovered


class _ServerHandle:
    """Manages a single MCP server as a long-lived async task.

    The stdio_client context manager and ClientSession live inside one
    async task for the server's entire lifetime. External callers
    communicate via thread-safe request/response futures.
    """

    def __init__(self, name, srv_config, loop):
        self.name = name
        self._srv_config = srv_config
        self._loop = loop
        self.tools = []  # populated after start()
        self._ready = threading.Event()
        self._error: Exception | None = None
        self._shutdown_event: asyncio.Event | None = None
        self._task: asyncio.Task | None = None

    def start(self, timeout=30):
        """Start the server task and block until tools are discovered."""
        future = asyncio.run_coroutine_threadsafe(self._run(), self._loop)
        # Wait for the server to be ready (or fail)
        if not self._ready.wait(timeout=timeout):
            raise TimeoutError(f"MCP server '{self.name}' did not start within {timeout}s")
        if self._error:
            raise self._error

    def call_tool(self, tool_name, arguments, timeout):
        """Execute a tool call (thread-safe, blocks until result or timeout).

        `timeout` is the single source of truth for this call — resolved upstream
        from the per-server override, ship-level default, or global fallback.
        On timeout, cancels the underlying asyncio task (best-effort; the MCP
        subprocess may still finish in the background) and raises MCPToolError
        with a message the LLM can relay to the user.
        """
        import concurrent.futures as _cf
        future = asyncio.run_coroutine_threadsafe(
            self._execute_tool(tool_name, arguments), self._loop
        )
        try:
            return future.result(timeout=timeout)
        except _cf.TimeoutError:
            future.cancel()
            raise MCPToolError(
                f"Tool '{tool_name}' timed out after {timeout}s on MCP server "
                f"'{self.name}'. Tell the user the tool ran past its configured "
                f"timeout — suggest a narrower request or, if this kind of task "
                f"is expected to take longer, raising `tool_timeout_seconds` "
                f"under mcp_servers.{self.name} in ~/.operator/agents/<name>/config.yaml."
            )
        except MCPToolError:
            raise
        except Exception as e:
            raise MCPToolError(f"Tool '{tool_name}' failed: {e}") from e

    def stop(self, timeout=6):
        """Signal the server task to shut down and wait for graceful cleanup."""
        if self._shutdown_event:
            self._loop.call_soon_threadsafe(self._shutdown_event.set)
        if self._task:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._wait_then_cancel(), self._loop
                ).result(timeout=timeout)
            except Exception:
                pass

    async def _wait_then_cancel(self):
        """Wait for the task to finish; cancel only as a last resort.

        stdio_client's cleanup sequence (close stdin, wait 2s, SIGTERM,
        wait 2s, SIGKILL) is bounded to ~4s. We wait 5s for it to
        complete naturally. Cancelling immediately would inject
        CancelledError into that cleanup, leaving the subprocess alive.
        """
        if not self._task or self._task.done():
            return
        try:
            async with asyncio.timeout(5):
                await self._task
        except asyncio.TimeoutError:
            log.warning(f"MCP server '{self.name}': graceful shutdown timed out, force-cancelling")
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        except (asyncio.CancelledError, Exception):
            pass

    async def _run(self):
        """Long-lived task: connect, discover tools, serve requests, shutdown."""
        self._task = asyncio.current_task()
        self._shutdown_event = asyncio.Event()
        self._session = None

        try:
            params = StdioServerParameters(
                command=self._srv_config["command"],
                args=self._srv_config["args"],
                env={**os.environ, **self._srv_config["env"]},
            )
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    self._session = session
                    tools_result = await session.list_tools()
                    self.tools = tools_result.tools
                    log.info(f"MCP server '{self.name}': {len(self.tools)} tools discovered")
                    self._ready.set()

                    # Serve until shutdown
                    await self._shutdown_event.wait()

        except Exception as e:
            self._error = e
            self._ready.set()  # unblock start()
            log.error(f"MCP server '{self.name}' task error: {e}")

    async def _execute_tool(self, tool_name, arguments):
        """Execute a tool call on the session (must run on the event loop)."""
        if not self._session:
            raise MCPToolError(f"Server '{self.name}' not connected")
        result = await self._session.call_tool(tool_name, arguments)
        if result.isError:
            error_text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
            log.error(f"MCP tool returned error: {error_text}")
            raise MCPToolError(f"Tool error: {error_text}")
        parts = []
        for c in result.content:
            if hasattr(c, "text"):
                parts.append(c.text)
            elif hasattr(c, "resource") and hasattr(c.resource, "text"):
                parts.append(c.resource.text)
            else:
                log.warning(f"MCP tool result has unhandled content: type={type(c).__name__}")
        result_text = "\n".join(parts)
        log.info(f"MCP tool result length={len(result_text)}")
        log.debug(f"MCP tool result: {result_text[:500]}")
        return result_text
