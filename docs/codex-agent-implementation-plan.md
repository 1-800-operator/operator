# Codex agent — implementation plan

Drafted session 179 (2026-05-01) after the codex spike (`debug/codex_spike/SPIKE.md`) confirmed Path B (Codex-as-MCP) viable with full chat-confirmation parity via inbound MCP `elicitation/create`. **Phase 0 probes complete (`debug/codex_spike/PHASE_0_FINDINGS.md`); plan adjustments below are folded in.**

**Scope:** ship a `codex` agent parallel to the `claude` agent. Mirror the claude track exactly except where Codex's native capabilities save substantial code or deliver substantial UX wins.

**Aligned deviations (locked):**
1. No `codex_cli.py` provider — Codex plumbed via `codex mcp-server` instead. Saves ~600–900 LOC.
2. Confirmation via inbound MCP `elicitation/create` (forced — only mechanism Codex offers).
3. `approved_execpolicy_amendment` for "approve and remember within thread" — **user-driven only** (triggered by user-explicit "yes always" in chat). Per phase-0 finding, scope is exact-argv match.
4. No routing-LLM-on-top — chat → `codex(prompt=...)` directly, threadId tracked in provider.
5. **No `permissions.auto_approve` / `always_ask` lists in v1** (phase-0 finding). Codex's internal safe-allowlist filters read-class commands before they reach our handler; the only types that elicit are `unknown` (write/exec/network), so per-type matching is dead code. Permissions block reduces to `default_approval_policy: on-request` + `default_sandbox: read-only`. v2 may add fnmatch over `codex_command` argv if usage justifies.

**Estimated total:** ~930–1,170 LOC across ~17 files, 5 dependency-ordered phases. Roughly 2 sessions of work. (Down from initial estimate of 1,140–1,400 after phase-0 simplifications.)

---

## Open-question decisions (locked, do not redesign)

### Q1 — `CodexMCPProvider` (new `LLMProvider`), not chat_runner short-circuit

Every existing `LLM_PROVIDER != "claude_cli"` guard already gates "non-OpenAI/Anthropic CLI-shaped provider." Adding `"codex_mcp"` to those tuples is cheaper than widening `chat_runner`. Provider holds `threadId`, calls into the existing `MCPClient` for `codex(prompt=...)` / `codex-reply(threadId=..., prompt=...)`. ~100–140 LOC.

The provider does NOT own the codex MCP subprocess. The subprocess is a normal `mcp_servers:` entry started by `MCPClient.connect_all()`. The provider only holds the threadId and calls into the existing MCP client. Lifecycle (start/stop, restart on crash) stays on the same code path as every other MCP server.

### Q2 — Phase ordering

**0 (probe) → 1 (elicitation handler) → 2 (provider + handler) → 3 (agent files) → 4 (preflight + main wiring) → 5 (tests).** Phases 1, 2, 3 mergeable independently behind the activation gate (no `agents/codex/config.yaml` on disk until phase 3 lands). Phase 4 is the activation cut. Phase 5 parallel with 4.

### Q3 — Elicitation handler location and signature

Inbound `elicitation/create` handling lives in `_ServerHandle._run`, between `await session.initialize()` and `await self._shutdown_event.wait()`. Per-server registration:

```python
elicitation_handler(server_name: str, params: dict) -> dict
# returns one of:
#   {"decision": "approved"}
#   {"decision": "abort"}
#   {"decision": {"approved_execpolicy_amendment": {...}}}
```

A codex-aware `CodexElicitationChatHandler` (new file `pipeline/codex_elicitation_handler.py`, ~150 LOC) mirrors `permission_chat_handler.py:PermissionChatHandler` shape.

### Q4 — Login preflight hard-fail

Mirror claude's path. New `pipeline/codex_import.py:codex_installed_and_logged_in()` probes via `shutil.which("codex")` then `codex login status` (5s timeout). Returns `(False, parsed_stderr_msg)` on non-zero exit. Also extracts auth mode (`subscription` vs `api-key`) for the billing assertion (R5).

### Q5 — Load-bearing things missing from the four-bullet design

1. **Codex MCP-server crash mid-meeting.** Provider catches `MCPToolError` carrying "session disconnected" / "thread not found", clears threadId, falls back to fresh `codex(...)` call.
2. **`approved_execpolicy_amendment` payload shape unknown** — phase 0 probe must capture an actual approval-with-amendment round-trip.
3. **`codex_parsed_cmd` is a list.** Multi-entry compound shells must use strict-AND for auto-approve, lenient-OR for always-ask. (See R6.)
4. **Outbound MCPs from `~/.codex/config.toml` deferred to v2.** Codex agent ships with no inherited MCPs in v1; users add via wizard.
5. **Permissions vocabulary divergence.** `setup.py:_BUILTIN_TOOLS` lists Read/Bash/etc. (claude vocab). Codex needs `parsed_cmd.type` values. Branch in `_step_permissions`.
6. **`config.py` validation.** Add `"codex_mcp"` to provider tuple at line 107; extend track-A branches at 213/298/566.
7. **Track-A claude assumption in `chat_runner._wire_track_a_permissions`.** Add sibling `_wire_track_codex_elicitation` for `CodexMCPProvider`; don't unify (different mechanisms).
8. **Progress narration for codex.** Codex's MCP-server mode does NOT expose intermediate tool calls — only final response + per-command elicitations. Skip narrator for codex agent in v1; rely on `agent.voice: plain` + system_prompt directive.
9. **Per-server elicitation handler (not global).** Other servers (Linear, Sentry) don't emit elicitations today; if one ever does we don't want it routed through codex-aware chat formatting.

---

## Phase 0 — Spike completion (probes only)

**Files touched:** `debug/codex_spike/SPIKE.md` (append). No production code.
**Mergeable independently:** yes.

### Probes
1. **`parsed_cmd.type` enumeration.** Send through `codex mcp-server` with `approval-policy: untrusted`: `cat /tmp/x` (read), `grep foo /tmp/x` (search), `ls /tmp` (already known: `list_files`), `find /tmp -name foo`, a here-doc `apply_patch` write, network `curl example.com`, multi-command `cat foo && echo bar`, `python -c 'print(1)'`. Capture every `codex_parsed_cmd[*].type` value emitted. Aim: 8–12 distinct types.
2. **`approved_execpolicy_amendment` payload.** Send dict-form decision back; capture structure. Send a second identical command in same thread to confirm amendment suppresses re-elicitation.
3. **Compound `codex_parsed_cmd` (multi-entry).** Send `find / -name x | xargs cat`. Confirm whether codex emits one parsed_cmd entry per shell stage or one envelope per command.
4. **Subscription auth assertion.** With `OPENAI_API_KEY` set + logged-in subscription, observe `codex login status` output (parse for auth mode), and which billing path codex actually uses for an `mcp-server` invocation. If empirically clearing `OPENAI_API_KEY` in spawn env forces subscription, document.
5. **`~/.codex/config.toml` MCP server schema.** Read sample real-world config (or generate via `codex mcp add foo`). Confirm table name + field shape for v2 MCP-import.
6. **MCP Python SDK elicitation API surface (R1 mitigation).** `python -c "import mcp; print(mcp.__file__)"` + grep for `elicitation` in `client/session.py`. Determine which of the three R1 branches we're in.
7. **Tool-name namespace check (R2 sub-mitigation).** Confirm whether `codex__codex-reply` (hyphen in tool name) routes correctly through operator's `__` namespace separator.

### Acceptance
- `parsed_cmd.type` taxonomy documented (ideally 8+ types) with proposed `auto_approve` / `always_ask` split.
- `approved_execpolicy_amendment` dict shape documented with working round-trip log.
- Subscription forced via env-clear or via flag — documented either way.
- Defer codex-MCP-import (v1) or include in scope. **Default: defer.**
- MCP SDK elicitation hook strategy chosen.
- Tool-name hyphen status known.

### Risks (probe-specific)
- **Probe budget creep.** One-session cap; if any probe doesn't yield in two attempts, document the gap and move on. Implementation phases assume probes 1+2+3+6+7 succeed; 4+5 are nice-to-have.
- **Codex CLI version drift between probes and ship.** Pin `codex-cli 0.128.0` in `pipeline/codex_import.py`; WARN-not-fail on mismatch (R7).

---

## Phase 1 — `mcp_client.py` inbound elicitation handling

**Files touched:** `src/_1_800_operator/pipeline/mcp_client.py`. **LOC:** +30–50 (down from 60–90 — phase 0 confirmed SDK has `elicitation_callback` kwarg). **Mergeable independently:** yes.

### Changes
- `MCPClient.set_elicitation_handler(server_name, handler)`.
- Storage: `self._elicitation_handlers: dict[str, Callable] = {}`.
- Plumb into `_ServerHandle.__init__`; pass `elicitation_callback=self._elicitation_dispatch` to `ClientSession(...)` constructor in `_run`.
- `_elicitation_dispatch(context, params)` is async: schedule `loop.run_in_executor(None, handler, server_name, params_as_dict)` → await future → return `ElicitResult` with the handler's `{"decision": ...}` payload.
- Validate handler return shape; on schema violation → log + return `ErrorData(INVALID_REQUEST, "abort")`.

### Acceptance
- Unit test: mock MCP server emits `elicitation/create`; routing → handler → JSON-RPC response. Use `probe3c_write_approved.log` as fixture.
- Existing tests stay green.
- No-handler default documented.

### Risks
- **R1 resolved (phase 0):** SDK exposes `elicitation_callback` kwarg at `client/session.py:118`. No fallbacks needed.
- **Threading:** handler runs on executor thread, must not touch playwright. Codex handler routes via `runner._send` which already takes `_send_lock` — fine.

---

## Phase 2 — `CodexMCPProvider` + `CodexElicitationChatHandler`

**Files touched:**
- `src/_1_800_operator/pipeline/providers/codex_mcp.py` — NEW, 100–140 LOC.
- `src/_1_800_operator/pipeline/providers/__init__.py` — +12 LOC.
- `src/_1_800_operator/pipeline/codex_elicitation_handler.py` — NEW, 130–170 LOC.
- `src/_1_800_operator/config.py` — ~10 LOC.

**LOC delta:** +250–330. **Mergeable independently:** yes (depends on phase 1).

### `CodexMCPProvider`
- `__init__(self, mcp_client, *, default_approval_policy="on-request", default_sandbox="read-only", append_developer_instructions=None, cwd=None)`.
- `self._thread_id: str | None = None`.
- `complete(system, messages, ...)` — uses last user message as `prompt`. First call: `mcp.execute_tool("codex__codex", {"prompt": prompt, "approval-policy": ..., "sandbox": ..., "cwd": ..., "developer-instructions": system})`. Stores `threadId`. Subsequent: `codex__codex-reply` with stored `threadId`.
- On `MCPToolError` "thread not found" / "session disconnected" → clear `_thread_id`, retry once via `codex` (Q5.1).
- `complete_streaming` falls back to `complete`.
- `warmup` no-op.
- System prompt passed once via `developer-instructions` on first call only — codex stores per-thread. Document: mid-meeting system_prompt edits don't take effect until next meeting (mirrors claude_cli's spawn-time semantics).

### `CodexElicitationChatHandler`
~70–100 LOC (down from 130–170; phase-0 simplification removed type-matching logic).

`__call__(server_name, params)`:
1. Extract `codex_command`, `codex_cwd`, `proposed_execpolicy_amendment`.
2. Format chat prompt — plain: `Run \`<command_first_line>\` in \`<cwd>\`?`; verbose: full argv joined.
3. Block on `runner._await_reply` (existing pattern from `PermissionChatHandler`).
4. Parse reply:
   - `is_yes_always(reply)` → `{"decision": {"approved_execpolicy_amendment": {"proposed_execpolicy_amendment": <argv from params>}}}` — codex remembers exact-argv for thread.
   - `is_yes(reply)` → `{"decision": "approved"}` — single-shot.
   - else / timeout → `{"decision": "abort"}`.

Add `is_yes_always(text)` to `pipeline/confirmation.py` next to `is_yes`. Patterns: `^(yes |y |ok )?(always|forever|permanent|every time)`, `^(always|forever)( yes)?$`. ~10 LOC + tests.

No type-matching, no fnmatch, no auto-approve / always-ask lists. Codex's internal safe-allowlist already filters read-class commands before they reach this handler.

### `build_provider`
```python
if name == "codex_mcp":
    return CodexMCPProvider(
        mcp_client=None,  # late-bound by chat_runner._wire_track_codex_elicitation
        append_developer_instructions=config.SYSTEM_PROMPT or None,
        cwd=os.getcwd(),
    )
```

### Acceptance
- Mock MCPClient: first call → `codex__codex` correct args, threadId stored, content as `ProviderResponse`.
- Second call → `codex__codex-reply` with stored threadId.
- Thread-died sim → fallback to fresh `codex`.
- Handler: auto-approve all-types-allowed → amendment; mixed-types → round-trip; `unknown` always round-trips.
- `config.py` accepts `provider: codex_mcp` without `model`.

### Risks
- **R2 — Tool-name namespace `codex__codex-reply` hyphen vs `__` separator.** Resolved in phase 0 probe 7. Fallback: register codex MCP server under `codex_brain` instead of `codex`.
- **`developer-instructions` vs `base-instructions`.** We want injection (additive) → `developer-instructions`. Confirm phase 0.
- **Thread state across `LLMClient.set_record` resets.** Provider's threadId stays — fine, document.
- **R4 — Late-bind NPE.** Assert in `complete()`: `self._mcp_client is not None` else `RuntimeError("CodexMCPProvider not wired")`. Symmetric assert in `_wire_track_codex_elicitation` after assignment.

---

## Phase 3 — Agent files (`agents/codex/`)

**Files touched:**
- `src/_1_800_operator/agents/codex/__init__.py` — NEW, empty.
- `src/_1_800_operator/agents/codex/config.yaml` — NEW, ~110 LOC.
- `src/_1_800_operator/agents/codex/framework.py` — NEW, ~50 LOC.

**LOC delta:** +160–180. **Mergeable independently:** inert without phases 1–2 + 4.

### `config.yaml` shape
- `agent.name: "Codex"`, `trigger_phrase: "@codex"`, tagline.
- `llm.provider: "codex_mcp"` (no model, no history_messages).
- `transcript.captions_enabled: false` — **R8 deferral.** Header comment names the gap + roadmap link.
- `permissions.default_approval_policy: on-request` — codex's model gates; less noisy than `untrusted`. (Phase-0 simplification: no auto_approve / always_ask lists.)
- `permissions.default_sandbox: read-only`.
- `mcp_servers.codex` — bundled:
  ```yaml
  mcp_servers:
    codex:
      command: codex
      args: [mcp-server]
      env:
        OPENAI_API_KEY: ""   # R5 layer 1: clear at spawn → forces subscription auth
      description: "Codex CLI in MCP-server mode (the agent's brain)"
      enabled: true
  ```
- `skills.enabled: []`, `skills.external_paths: []` — codex has no `~/.codex/skills/` analogue.
- `system_prompt: ""`.

### `framework.py`
Mirror of claude's framework.py with codex-flavored voice. Tweak "your tools are…" line — codex has shell as universal tool, not Read/Bash/Edit.

### Acceptance
- `OPERATOR_BOT=codex python -c "from _1_800_operator import config"` exits clean.
- `operator` (no args) lists codex.
- `operator where codex` returns the right path.
- `operator edit codex` opens wizard with current config preloaded.

### Risks
- **R8 — Caption deferral UX gap.** Mitigation: header comment in `config.yaml` + startup banner in `operator dial codex`: "Codex agent does not see meeting captions in v1 — only chat messages." + roadmap entry "codex caption parity."
- **Wizard step 3.5 vocabulary** — phase 4 must teach `setup.py` the codex tool list before phase 3 reaches user-visible state.

---

## Phase 4 — Preflight + chat_runner wiring + setup.py + readiness

**Files touched:**
- `src/_1_800_operator/pipeline/codex_import.py` — NEW, ~60–80 LOC.
- `src/_1_800_operator/__main__.py` — +30 LOC.
- `src/_1_800_operator/pipeline/chat_runner.py` — +30 LOC.
- `src/_1_800_operator/pipeline/setup.py` — +20 LOC (two-radio-button UI for policy + sandbox; no `_BUILTIN_TOOLS_CODEX`, phase-0 simplification).
- `src/_1_800_operator/pipeline/readiness.py` — +20 LOC.

**LOC delta:** +170–200. **Mergeable independently:** no — depends on 1, 2, 3. Activation cut.

### Wiring details
- `__main__.py`: `if name == "codex":` branch → `codex_installed_and_logged_in()`, hard-fail on red. Optional `_sync_codex_imports()` (v1 stub).
- `if config.LLM_PROVIDER != "claude_cli":` guards at lines 778, 916, 1011, 1159 → drop the exclusion entirely for codex (operator's `MCPClient` *does* run for codex — it owns the codex MCP server itself).
- `chat_runner._wire_track_codex_elicitation`: after `connect_all`, `mcp_client.set_elicitation_handler("codex", CodexElicitationChatHandler(...))`, late-bind `provider._mcp_client = self._mcp`.
- `setup.py`: branch `_step_permissions` on provider. New `_BUILTIN_TOOLS_CODEX` from phase 0.
- `readiness._probe_codex` analog of `_probe_claude_code`.
- **R3 — runtime-failures special-case** in `MCPClient.execute_tool` / `record_tool_result`: `if server_name == "codex" and config.LLM_PROVIDER == "codex_mcp": skip auto-disable`. Surface to chat: "Codex hit an error — give it a moment, or try `@codex retry`."

### `codex_import.py:codex_installed_and_logged_in()`
1. `shutil.which("codex")` → `(False, "codex CLI not found on PATH...")`.
2. `codex --version` → parse; **WARN-not-fail** if major.minor doesn't match pinned `0.128.x` (R7).
3. `codex login status` (5s timeout) → parse stdout for auth mode.
4. **R5 layer 2:** if auth mode is `api-key`, hard-fail: "Codex agent requires ChatGPT subscription auth, not API key. Run `codex logout` then `codex login` to switch."
5. Return `(True, None)` only if all pass.

### Acceptance
- `operator dial codex` no codex CLI → exits 2, correct error.
- Not logged in → exits 2, correct error.
- API-key-only auth → exits 2, correct error (R5 layer 2).
- Happy path: joins meet, codex MCP connects, `@codex hello` → reply. End-to-end smoke.
- Write attempt: `@codex create /tmp/probe.txt with body hello` → elicitation lands in chat, "yes" approves, file created.
- `operator edit codex` permissions step shows codex vocab.
- 5 consecutive simulated `MCPToolError`s on codex server → server stays connected, chat surfaces the error (R3).

### Risks
- **R4 — Late-bind ordering.** Asserts on both sides (phase 2).
- **R3 — runtime auto-disable.** Mitigated above.
- **R5 layer 3 — runtime auth log.** On first `codex` MCP tool call's response, log auth mode if codex exposes it (probe-dependent). TIMING-style line in `/tmp/operator.log`.
- **Captions/transcript MCP under codex.** Deferred (R8).

---

## Phase 5 — Tests

**Files touched (all NEW):**
- `tests/test_codex_mcp_provider.py` — ~120 LOC.
- `tests/test_codex_elicitation_handler.py` — ~150 LOC.
- `tests/test_codex_import.py` — ~80 LOC.
- `tests/test_mcp_client_elicitation.py` — ~120 LOC.
- `tests/test_codex_agent_config.py` — ~60 LOC.

**LOC delta:** +500–600. **Mergeable independently:** parallel with phase 4.

### Acceptance
- All five runnable as `python tests/test_<name>.py` (matches existing convention).
- Each claude test pattern has a codex sibling.
- Phase 0's `probe3c_write_approved.log` baked into elicitation test as recorded round-trip.
- Compound-command security test (R6): `find / | xargs rm -rf`-shaped fixture must round-trip to chat, must not auto-approve.
- Runtime-failures special-case test (R3): 5 consecutive errors → server stays connected.

### Risks
- **R9 — MCP SDK mocking depth.** If phase 1 ended up needing low-level message routing, fall back to integration tests against real `codex mcp-server` subprocess (gated by `codex` on PATH; skip otherwise). Promote `probe3_mcp_elicitation.py` into `tests/test_codex_e2e.py` with skip guard. ~30 LOC.

---

## Risk register (post-phase-0)

| ID | Risk | Status / Mitigation |
|---|---|---|
| R1 | MCP SDK lacks elicitation hook | ✅ Resolved — `ClientSession(elicitation_callback=...)` confirmed at `client/session.py:118` |
| R2 | Phase 0 probe gaps | ✅ Resolved — taxonomy mapped (`unknown` only), amendment shape captured, no namespace collision |
| R3 | `MCPClient` auto-disables codex brain after 3 errors | Mitigation in phase 4: special-case `server_name == "codex" and provider == "codex_mcp"`; surface error to chat |
| R4 | Late-bind NPE | Asserts on both ends of the wire-up (phases 2 + 4) |
| R5 | `OPENAI_API_KEY` silently switches to API billing | Three layers: env-clear in `mcp_servers.codex.env` + preflight `codex login status` parse for "ChatGPT" + runtime log |
| R6 | Compound `parsed_cmd` auto-approve risk | ✅ Non-issue — Codex collapses compounds to single `unknown`; auto-approve list deleted from v1 anyway |
| R7 | Codex CLI version drift | Pin `0.128.x` constant; WARN-not-fail on mismatch (phase 4) |
| R8 | Caption deferral creates UX gap vs claude | Banner + `config.yaml` header comment + roadmap entry (phases 3 + 4) |
| R9 | Test mocking too brittle | ✅ Resolved — SDK callback hook is clean; standard unit-test patterns apply |

---

## Total scope (post-phase-0)

| Phase | LOC | Touch |
|---|---|---|
| 0 — probes | ✅ done | `debug/codex_spike/{SPIKE,PHASE_0_FINDINGS}.md` |
| 1 — elicitation in mcp_client | +30–50 | 1 file |
| 2 — provider + handler + config | +200–270 | 4 files |
| 3 — agent files | +160–180 | 3 new files |
| 4 — wiring | +140–170 | 5 files |
| 5 — tests | +400–500 | 5 new test files |
| **Total** | **~930–1,170** | **~17 files, mostly new** |

vs. Path A's 2,000–2,700 LOC for `codex_cli.py`. Path B confirmed cheaper AND with full feature parity for the v1 use case.
