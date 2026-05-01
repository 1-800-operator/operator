# Permission MCP spike — report

*Phase 14.12.1 deliverable, session 165 (2026-04-26). Validates the host-callable permission contract for track A: operator as a Meet-front-end for the user's Claude Code, with inner tool-use confirmations routed through Meet chat.*

## Bottom line

**Track A is buildable as designed.** The CLI path via `--settings <file>` + `PreToolUse` hook + named-pipe IPC works under the user's Claude Max subscription (no API key) and gives us:

- Synchronous tool-call gating with up to 600s timeout (well above the ~45s typical user reply latency)
- Clean deny semantics with custom reason text relayed back to inner-claude's final result
- Stream-json visibility into tool_use events for progress reporting alongside permission prompts
- Confirmed `apiKeySource: "none"` end-to-end — actual subscription billing, not API

**One known limitation:** sub-agent (Task tool) inner tool calls are opaque to our hook. The top-level dispatch fires, but the sub-agent's internal tool calls do not. Mitigation: gate the dispatch itself, run inside a per-session worktree sandbox, surface sub-agent progress in chat from stream-json's `parent_tool_use_id`-tagged events.

## API shape that works

```bash
claude -p "<task>" \
  --settings <tempfile.json> \
  --output-format stream-json --verbose \
  --include-hook-events \
  --permission-mode default
```

`<tempfile.json>`:
```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/perm_bridge.sh /path/to/req.pipe /path/to/resp.pipe",
            "timeout": 120
          }
        ]
      }
    ]
  }
}
```

Hook receives JSON on stdin: `{"tool_name": "Write", "tool_input": {...}, "tool_use_id": "...", ...}`.
Hook writes JSON to stdout: `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"|"deny"|"ask"|"defer", "permissionDecisionReason": "..."}}` and exits 0.

Inner-claude blocks on the hook synchronously. On deny, its final result text includes our reason verbatim:

> *"Write blocked by PreToolUse hook: 'probe3: simulated user rejection — please don't write that file'. Not retrying."*

The `--settings` flag accepts an ad-hoc settings file per invocation — no need to mutate the user's `~/.claude/settings.json` or `.claude/settings.local.json`.

## Probe results

| Probe | Question | Result |
|---|---|---|
| 1 — happy path | Does the hook fire and respect our allow decision? | ✅ Pass. Hook fired at 7.25s of 9.19s elapsed. File written after parent's allow. `apiKeySource: "none"` confirmed in result event. |
| 2b — sub-agent visibility | Do sub-agent inner tool calls fire our hook? | ✅ Confirmed NO. Top-level Agent dispatch fired our hook (1 call). Sub-agent's 2 internal Writes did not. Stream-json shows them with `parent_tool_use_id` set. |
| 3 — deny path | What does claude do on deny? | ✅ Pass. Exits 0 with `subtype: "success"`, `permission_denials` populated, reason text in result. No retries, no loops. |

Probe scripts live at `debug/permission_mcp_spike/cli_probe_*.py`. Stream-json artifacts at `debug/permission_mcp_spike/probe*_stream.jsonl`.

## CLI vs Agent SDK pathway comparison

| Criterion | CLI path (chosen) | Agent SDK path |
|---|---|---|
| Subscription auth (claude.ai/max) | ✅ Yes — verified `apiKeySource: "none"` | ❌ No — `ANTHROPIC_API_KEY` only ([GitHub #559](https://github.com/anthropics/claude-agent-sdk-python/issues/559)) |
| Permission callback contract | PreToolUse hook → JSON I/O via stdin/stdout, named-pipe IPC to host | `can_use_tool` async Python callback in same process |
| Sub-agent visibility | Top-level dispatch only (sub-agents opaque) | Full visibility (sub-agent calls hit `can_use_tool` with `parent_tool_use_id`) |
| Timeout for permission decision | 600s default, configurable per-hook | No built-in timeout — caller can wait indefinitely |
| Latency overhead | Subprocess spawn per `claude -p` call (~1–2s) + hook spawn per tool call (negligible) | In-process, minimal |
| Cancellation | Kill the subprocess | No per-call cancel; kill the query |
| Lines of host code | ~60 (settings.json + bridge script + IPC reader) | ~30 (async callback in options) |
| Testability | Real subprocess; harder to mock | Direct unit-test with mock callback |

**Decision: CLI path.** The subscription-auth requirement is non-negotiable for track A's "no API key" promise. Sub-agent opacity is the cost — manageable with worktree sandboxing.

The SDK path would be the cleaner architecture *if* track A pivoted to API-key auth. It's not on the table for this product, but worth noting for any future track-A-style product that wants to charge API rates.

## Findings to feed into Phase 14.12.2

1. **The `--settings <file-or-json>` flag exists** even though the public docs say it doesn't. Direct `claude --help` confirms. Per-invocation settings is the cleanest registration mechanism for our hook — no global state mutation, no clean-up race conditions.

2. **`--permission-prompt-tool` does NOT exist.** Both early research agents and one upstream blog article reference this flag — it's an Agent SDK feature, not a CLI flag. PreToolUse hook is the actual CLI mechanism.

3. **Hooks support `permissionDecision: "defer"`** (Claude Code v2.1.89+). Defer surfaces a `deferred_tool_use` event to the calling process via stream-json. Not used here (synchronous bridge already works), but could simplify the IPC layer if we want to push the wait-for-user round-trip into stream-json itself instead of named pipes. Worth considering during 14.12.2 implementation.

4. **`apiKeySource` field in stream-json's system-init event** is the definitive billing signal. operator should log this on every inner-claude spawn so we never accidentally ship a config that flips back to API billing. Worth turning into a startup assertion: if `apiKeySource != "none"` and we expected subscription, fail loud.

5. **Sub-agent dispatch sees the full sub-agent prompt in our hook** (we logged it during probe 2b: `{"description": "Write two test files", "prompt": "Create two files. First, use Write to create..."}`). So we can render *what* the sub-agent will do in chat for the user to gate, even though we lose visibility once it runs.

6. **`--include-hook-events` did not actually surface hook events in stream-json** in our probes. Stream-json only showed the gated tool_use events. This isn't a bug — the hook activity is the gate, not a stream event. We get visibility via our own bridge script's logs.

7. **Inner-claude's default model when invoked via `claude -p`** is `claude-opus-4-7[1m]` (the 1M-context Opus). Each fresh `claude -p` invocation starts a new prompt cache (cache_create dominates first-call cost). On subscription this is metered as messages-per-window, so the cost-per-task framing inverts vs. API. Worth surfacing in the README warning text alongside "Claude Code in your Meet uses your Max plan, not your API budget."

## Architectural recommendation for Phase 14.12.2

Build `pipeline/providers/claude_cli.py` that implements `LLMProvider` with the following shape:

1. **One `claude -p` subprocess per LLM turn**, invoked with `--settings <tempfile.json>` containing a PreToolUse hook pointing to a `pipeline/permission_bridge.py` script we ship.

2. **Named-pipe IPC** between the bridge and the operator parent: bridge writes the tool_use details to a request pipe, blocks on a response pipe; chat_runner reads the request, posts the confirmation prompt to Meet chat using the same `_request_confirmation` flow we already have, awaits user reply, writes the decision to the response pipe.

3. **Worktree sandboxing as the second line of defense.** Every track-A run gets its own per-session `.claude/worktrees/<adjective-noun>/` worktree (already supported via `--worktree`). Sub-agent opacity is contained because anything written goes into the sandbox.

4. **Subscription-auth assertion at startup.** Read the system-init event's `apiKeySource` from stream-json and fail loud if it's not `"none"`. This catches any environment-variable leak (e.g., if `ANTHROPIC_API_KEY` ever ends up in the env operator passes to its subprocess) before it bills the user's API budget.

5. **Defer the `defer` decision pattern** to a future iteration. Synchronous IPC works today. If hook-spawn-per-tool-call latency becomes a problem (it shouldn't — the bridge script is ~10ms), revisit `defer` to push the wait into stream-json events.

## Estimated effort impact for 14.12.2

Spike findings reduce 14.12.2's estimate from "2–3 days" to roughly **1.5–2 days**:

- IPC bridge script: 1–2 hours
- claude_cli.py provider: 4–6 hours (substreams, paragraph-flush, system-prompt augmentation)
- chat_runner integration of permission bridge: 2–3 hours (reuse existing `_request_confirmation`)
- Worktree-per-session lifecycle: 1–2 hours (mostly removing now-redundant `delegate_to_claude_code` plumbing)
- Subscription-auth assertion: 30 min
- Migration: drop bundled MCPs from claude bot, retire dead Chrome preflight: 1 hour
- Tests: 2 hours

The unknown is the system-prompt augmentation — passing operator's track-A `personality` + `ground_rules` into `claude -p`'s system prompt without conflicting with the user's `~/.claude/CLAUDE.md`. Several flags exist (`--system-prompt`, `--append-system-prompt`, `--append-system-prompt-file`); choosing the right one will need a small experiment but probably not its own spike.

## Open questions for 14.12.2

1. Does the subprocess-spawn-per-LLM-turn pattern work cleanly for streaming replies, or do we want a long-lived `--input-format stream-json` session with one process per meeting? The latter is more ergonomic but ties us to Anthropic's stream-json input contract more deeply. Recommend: spike both during 14.12.2 implementation, default to whichever has lower complexity.

2. How does the inner-claude session interact with operator's meeting record JSONL? Today operator builds prompts from `MeetingRecord.tail(n)`. Under track A, do we still curate the prompt or do we trust inner-claude's own context loop (with the meeting record fed as either `--add-dir` or as the user-message stream)?

3. Does `--worktree` work with `claude -p` cleanly (the current `delegate_to_claude_code` MCP uses it, so almost certainly yes — but the parent invocation hasn't been tested)?

These are 14.12.2 decisions, not blockers. The spike's job is done: track A as designed is technically buildable, subscription-billed, and gated through chat.
