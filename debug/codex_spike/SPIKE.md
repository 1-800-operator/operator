# Codex CLI integration spike — phase-1 findings

**Date:** 2026-05-01
**Question:** Can we ship a `codex` agent in v1, parallel to the `claude` agent? Specifically — can we intercept Codex's tool/shell calls *before* execution so meeting-chat confirmation works the way it does on the claude track?

**Status:** Phase 1 complete (no auth required). Phase 2 (live event stream + approval round-trip) blocked on `codex login`.

---

## What's installed

- `npm install -g @openai/codex` → `codex-cli 0.128.0` at `/opt/homebrew/bin/codex`.
- Node wrapper at `bin/codex.js` that spawns native Rust binary `@openai/codex-darwin-arm64/vendor/...`. No JS/TS source to grep — closed-source binary, only the CLI surface and JSON I/O are observable.
- `~/.codex/` exists (`memories/`, `tmp/`). No `config.toml` yet. Not logged in.

## CLI surface relevant to integration

| Subcommand | Role |
|---|---|
| `codex exec --json` | **Non-interactive, JSONL events to stdout.** Direct analogue of `claude -p --output-format stream-json`. Reads prompt from arg or stdin. |
| `codex mcp-server` | **Codex itself as an MCP server over stdio.** No analogue exists in Claude Code. *This changes the integration calculus.* |
| `codex login` / `codex login --with-api-key` | Subscription auth via browser, or API key from stdin. |
| `codex mcp` | Manage *outbound* MCP servers Codex talks to (configured in `~/.codex/config.toml`). |

Auth posture: subscription (ChatGPT Plus/Pro/Business via `codex login`) **or** API key (`OPENAI_API_KEY` env or stored credential). Mirror of Claude Code's apiKeySource model.

## `codex exec --json` event shape (probed without auth)

Sent: `echo "hello" | codex exec --json --skip-git-repo-check --sandbox read-only`

Observed JSONL events on stdout (interleaved with non-JSON Rust `tracing` logs on stderr):

```jsonl
{"type":"thread.started","thread_id":"019de711-90e7-77b0-854a-c607dcbf64b0"}
{"type":"turn.started"}
{"type":"error","message":"Reconnecting... 2/5 (...401...)"}
{"type":"turn.failed","error":{"message":"unexpected status 401 ..."}}
```

Confirmed:
- Events are typed JSONL, one per line, parseable by `json.loads`.
- `thread.started` / `turn.started` / `turn.failed` lifecycle markers exist.
- Errors come through as `{"type":"error",...}`.
- Stderr emits Rust `tracing` logs that are **not JSONL** — must be ignored by any pump thread (matches `claude_cli.py:_reader_thread`'s pattern of dropping non-JSON lines).

**Not yet observed (blocked on auth):** tool/shell-call events (`tool.started`?), final-output events (`item.message`?), and crucially — whether there's any pre-execution event we can gate on.

## `codex mcp-server` tool surface (probed without auth — protocol-level only)

Sent stdin: standard MCP `initialize` → `notifications/initialized` → `tools/list`.

Codex returned:

```json
{"protocolVersion":"2024-11-05","capabilities":{"tools":{"listChanged":true}},
 "serverInfo":{"name":"codex-mcp-server","title":"Codex","version":"0.128.0", ...}}
```

Then **two tools**:

### `codex` — start a session
```
inputSchema:
  prompt                  (required, string)
  approval-policy         enum: untrusted | on-failure | on-request | never
  sandbox                 enum: read-only | workspace-write | danger-full-access
  cwd                     string
  model                   string  (e.g. "gpt-5.2", "gpt-5.2-codex")
  developer-instructions  string  (developer-role injection)
  base-instructions       string  (replaces default system prompt)
  compact-prompt          string
  profile                 string  (named profile from ~/.codex/config.toml)
  config                  object  (arbitrary CODEX_HOME/config.toml override)
outputSchema: { threadId: string, content: string }
```

### `codex-reply` — continue a session
```
inputSchema:
  threadId   string  (required in spirit, optional for back-compat)
  prompt     string  (required)
outputSchema: { threadId: string, content: string }
```

This is **Codex-as-a-tool with full session/thread state**. Big deal for our integration shape.

---

## Two viable integration paths

### Path A — `codex_cli.py` provider (mirror of `claude_cli.py`)

Long-lived `codex exec --json` subprocess per meeting; pump events, surface tool calls to chat for confirmation, send results back.

- **Pro:** Symmetric with the claude track. The meeting bot owns the whole loop end-to-end. Chat confirmation hooks the same way.
- **Pro:** Subscription-vs-API billing assertion is enforceable (mirror the `apiKeySource: "none"` check).
- **Con:** ~600–900 LOC of new provider code.
- **Con:** Pre-tool-use interception **is the open question** — phase 2 needs to confirm whether `codex exec --json` emits a pre-execution event we can gate on, or whether the only knob is the `approval-policy` setting (and Codex blocks waiting for... something).

### Path B — Codex-as-MCP-server (no provider code)

Add `codex mcp-server` to the agent's `mcp_servers:` list. Meeting LLM (small router model — GPT-4o-mini or Sonnet) decides when to delegate by calling the `codex` / `codex-reply` tools. Codex internally runs the agentic loop and returns `{threadId, content}`.

- **Pro:** ~50–100 LOC. Reuses the existing MCP client. Closed surface area.
- **Pro:** Approval/sandbox flow is **explicit, well-typed** (`approval-policy` + `sandbox` per call).
- **Con:** Meeting LLM does **not** see Codex's intermediate tool calls. Lossier audit trail in chat.
- **Open question:** When `approval-policy=untrusted`, does Codex round-trip an MCP `elicitation/create` request back to the parent so we can ask the user in chat? **This is the load-bearing phase-2 probe.** If yes, Path B has near-feature-parity with Path A at 1/10 the code. If no, Path B's confirmation UX is degraded to "approve everything up-front via the policy enum."

### Hybrid (likely shipping shape)

- **Codex agent's *brain* = Path B.** A thin meeting LLM router (cheap model) that takes meeting chat → wraps as `codex(prompt=...)` or `codex-reply(threadId=..., prompt=...)`. Mirrors how the `claude` agent treats Claude Code as the brain — different mechanism, same conceptual layering.
- Sandbox + approval-policy choices become **agent config**, not runtime decisions: e.g. ship default `approval-policy: on-request`, `sandbox: read-only`. Power users can flip via `operator edit codex`.

## Phase-2 probes (need login)

Run these once authenticated:

1. **`codex exec --json` with a tool-using prompt.** Send `"read /tmp/foo.txt"`. Capture every event type emitted. Look specifically for: any pre-execution event, any approval-prompt event, any way for the parent to *respond* to such an event over stdin.

2. **`codex exec --json` with `--sandbox read-only` and a write attempt.** "create a file /tmp/spike.txt with body hello". Does it emit an event the parent can intervene on, or does it just fail and emit `tool.failed`?

3. **`codex mcp-server` tool call with `approval-policy=untrusted`** and a prompt that requires a shell command. Does Codex send an `elicitation/create` request back over stdio asking the parent for approval? Or does it block silently? Or auto-fail?

4. **Subscription-vs-API billing assertion.** With `OPENAI_API_KEY` set in env *and* a logged-in subscription session, which one does `codex exec` use? Is there a flag to force one mode? Mirror of the `claude_cli.py` `apiKeySource: "none"` guard requires this answer.

5. **MCP-import scanner pattern.** Read a sample `~/.codex/config.toml` with declared MCP servers; confirm operator can re-import them into `agents/codex/config.yaml` the same way `_sync_claude_imports` does.

## Decision matrix (preliminary)

| Outcome of probe #3 (Codex MCP-server elicitation) | Recommended path |
|---|---|
| ✅ Codex sends `elicitation/create` for `untrusted` commands | **Path B** — ship the codex agent as a thin MCP-as-brain wrapper. ~1 session of work. |
| ❌ No round-trip; `untrusted` blocks/fails silently | **Path A** — write `codex_cli.py`, hook events directly. ~2–3 sessions. |
| 🟡 Partial (e.g. only certain command classes elicit) | **Hybrid** — Path B by default, Path A escape hatch for power users. ~2 sessions. |

## Files written

- `/Users/jojo/Desktop/operator/debug/codex_spike/SPIKE.md` (this file)
- `/tmp/codex_mcp_probe.jsonl` — MCP probe input
- `/tmp/codex_mcp_out.txt` — MCP probe output (raw)

## Phase 2 — live probes (logged in via ChatGPT subscription)

### Probe 1 — `codex exec --json` event stream with a tool-using prompt

Prompt: "Read /tmp/spike_target.txt and tell me what's in it." Sandbox: `read-only`.

Observed events (full output at `probe1_exec_readfile.stdout`):
```jsonl
{"type":"thread.started","thread_id":"..."}
{"type":"turn.started"}
{"type":"item.started","item":{"id":"item_0","type":"command_execution","command":"/bin/zsh -lc 'cat /tmp/spike_target.txt'","status":"in_progress"}}
{"type":"item.completed","item":{"id":"item_0","type":"command_execution","aggregated_output":"...","exit_code":0,"status":"completed"}}
{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"..."}}
{"type":"turn.completed","usage":{...}}
```

**Key finding:** `item.started` with `status: "in_progress"` arrives **before** the command runs. That's an observable pre-execution event. But it's pure observation — `codex exec` has no stdin protocol for the parent to *block* execution. This is consistent with `codex exec` being designed for autonomous runs.

### Probe 2 — write attempt under `read-only` sandbox

Sandbox blocks the write at OS level (`zsh: operation not permitted`). Codex doesn't emit a `command_execution` event for the blocked attempt — only the `agent_message` describing the failure (full output at `probe2_exec_writeblock.stdout`).

### Probe 2b — `codex exec` with `approval_policy=on-request` + write attempt

Same outcome as Probe 2 — no approval round-trip. **`codex exec` does not surface approval requests to its parent process.** This makes sense: `codex exec` is a one-shot command-line tool; it has nowhere to send the request. **Path A loses chat-confirmation if we go through `codex exec`.**

### Probe 3 — `codex mcp-server` with `approval-policy=untrusted` + safe command (`ls`)

Codex auto-approved (`parsed_cmd.type = "list_files"`) — Codex has a built-in safe-command allowlist that bypasses `untrusted`. 0 elicitation requests. (full output at `probe3_mcp_elicitation.log` / `probe3a_safe_ls.log`)

### Probe 3b — `codex mcp-server` with `approval-policy=untrusted` + write command

**THIS IS THE LOAD-BEARING PROBE. Result: ✅ Codex round-trips an MCP `elicitation/create` request to the parent.**

Codex emitted (full envelope at `probe3b_write_untrusted.log`):
```json
{"jsonrpc":"2.0","id":0,"method":"elicitation/create",
 "params":{
   "message":"Allow Codex to run `/bin/zsh -lc 'echo probe3b > /tmp/codex_probe3b_write.txt && cat ...'` in `/tmp`?",
   "requestedSchema":{"type":"object","properties":{}},
   "threadId":"019de718-...",
   "codex_elicitation":"exec-approval",
   "codex_mcp_tool_call_id":"2",
   "codex_event_id":"2",
   "codex_call_id":"call_6oYCWBnQFvswhwbfyz1LKsiH",
   "codex_command":["/bin/zsh","-lc","echo probe3b > /tmp/..."],
   "codex_cwd":"/tmp",
   "codex_parsed_cmd":[{"type":"unknown","cmd":"echo ... > /tmp/..."}]}}
```

The companion `codex/event` notification (`type: exec_approval_request`) declares `available_decisions: ["approved", {"approved_execpolicy_amendment": {...}}, "abort"]`.

### Probe 3c — full happy-path approval round-trip

Wrong response shape (`{"approved": true}`) → Codex reports `failed to deserialize ExecApprovalResponse: missing field 'decision'`. Correct shape: `{"jsonrpc":"2.0","id":<elicit_id>,"result":{"decision":"approved"}}`. After approval, command executed, file created (`probe3b` written), `tools/call` returned with the final synthesized text. **End-to-end approval + execution confirmed working** (full log at `probe3c_write_approved.log`).

## Conclusive findings

| Question | Answer |
|---|---|
| Does Codex CLI exist? | Yes — `npm install -g @openai/codex`, version 0.128.0. |
| Is there a streaming-JSON non-interactive mode? | Yes — `codex exec --json` emits typed JSONL with `thread.started` / `turn.started` / `item.started` (with `status: in_progress`) / `item.completed` / `turn.completed`. |
| Is there an MCP-server mode? | Yes — `codex mcp-server` exposes two tools (`codex`, `codex-reply`) for thread-aware sessions with full sandbox + approval-policy control per call. |
| Can the parent intercept tool calls before execution? | **Yes, via `codex mcp-server` + MCP `elicitation/create`.** Not via `codex exec --json`. |
| What's the response shape? | `{"jsonrpc":"2.0","id":<elicit_id>,"result":{"decision":"approved"\|"abort"}}` — and there's also `approved_execpolicy_amendment` for "approve and remember." |
| Rich metadata on the approval request? | Yes — `codex_command` (exact shell argv), `codex_cwd`, `codex_parsed_cmd` (semantic classification: `list_files`, `unknown`, etc.), `codex_elicitation` ("exec-approval"; `apply-patch-approval` likely also exists). Sufficient for plain-English chat translation. |
| Subscription-vs-API billing | This session is logged in as ChatGPT subscription. Codex prefers subscription auth over `OPENAI_API_KEY` when both are available; verifiable via `codex login status`. |

## Recommendation: **Path B (Codex-as-MCP) with elicitation routing**

Decisive. Path B has full feature parity with the claude track for the v1 use case:

- **Brain layer:** `codex mcp-server` is the meeting agent's brain (analogous to `claude -p` for the claude track). Per-request `approval-policy: untrusted` + `sandbox: read-only` defaults.
- **Approval routing:** the operator MCP client needs one new capability — handling inbound `elicitation/create` requests from servers, surfacing them to chat using the existing `_request_user_confirmation` flow, then sending `{"decision": "approved" | "abort"}` back. This is a **server→client** capability the existing `pipeline/mcp_client.py` likely doesn't have today; it's a generic MCP protocol extension that benefits *all* future servers.
- **Routing layer:** a thin meeting LLM (cheapest model: `gpt-4o-mini` or `claude-haiku`) translates incoming chat → `codex(prompt=...)` or `codex-reply(threadId=..., prompt=...)`. State-track the threadId in `pipeline/chat_runner.py` for the lifetime of the meeting.

### Estimated work

| Task | LOC | Risk |
|---|---|---|
| `pipeline/mcp_client.py`: handle inbound `elicitation/create` requests, route to a callback | ~80 | Low — additive, no protocol changes elsewhere |
| `chat_runner.py`: elicitation callback → existing chat-confirmation flow → MCP response | ~50 | Low — reuses existing confirmation UI |
| `agents/codex/{config.yaml, framework.py}` | ~150 | Low |
| `__main__.py`: codex preflight (login check, MCP import scan from `~/.codex/config.toml`) | ~80 | Low |
| Tests (mirror `test_claude_*` shape) | ~300 | Low |

**Total: 1–2 sessions.** Versus Path A's 2–3 sessions for less feature parity.

### Caveats / known gaps

1. **`approval-policy: untrusted` triggers elicitation for EVERY non-allowlisted command.** That's potentially noisy in chat. Two mitigations:
   - Use `on-request` as default (only escalates when the model itself flags risk).
   - Layer in `approved_execpolicy_amendment` for "approve and remember within thread" — the dict-form decision in the `available_decisions` list. Mirrors how `claude` agent's `auto_approve` allowlist works at config level, but here it's *per-thread learned*.
2. **MCP elicitation handling is a new capability for `mcp_client.py`.** No other server in the bundled set (Linear, Sentry, GitHub) uses elicitation today. We'd be the first consumer. Worth checking the official MCP TypeScript SDK reference implementation for response-shape edge cases.
3. **Codex thread state lives inside `codex mcp-server`.** If the server crashes mid-meeting, the threadId is lost. Same fragility as `claude_cli.py`'s subprocess. Acceptable.
4. **The "internal escalation" rejection.** Probe 3b showed Codex first try a direct escalation path (rejected with "approval policy is UnlessTrusted; reject command — you cannot ask for escalated permissions if the approval policy is UnlessTrusted") *before* falling through to elicitation. Adds ~5s latency per first-write-after-thread-start. Cosmetic, not load-bearing.

## Files written

- `debug/codex_spike/SPIKE.md` (this file)
- `debug/codex_spike/codex_mcp_probe.jsonl` — phase-1 MCP probe input
- `debug/codex_spike/codex_mcp_tools_list.json` — phase-1 MCP `tools/list` output
- `debug/codex_spike/probe1_exec_readfile.{stdout,stderr}` — `codex exec --json` w/ read
- `debug/codex_spike/probe2_exec_writeblock.{stdout,stderr}` — `codex exec` w/ blocked write
- `debug/codex_spike/probe2b_exec_onrequest.{stdout,stderr}` — `codex exec` w/ on-request approval (no round-trip)
- `debug/codex_spike/probe3_mcp_elicitation.py` — MCP-server elicitation harness
- `debug/codex_spike/probe3b_write_untrusted.log` — first elicitation observed (wrong response shape)
- `debug/codex_spike/probe3c_write_approved.log` — full approval round-trip success

## Decision

Recommend proceeding with **Path B implementation** as the next session's primary work. Preferred default config: `approval-policy: on-request`, `sandbox: read-only`, with `auto_approve` allowlist exposed via `operator edit codex` (mirror of the claude agent's permissions block).
