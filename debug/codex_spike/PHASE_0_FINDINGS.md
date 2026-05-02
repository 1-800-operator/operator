# Phase 0 probe findings (2026-05-01)

Probe results from the seven Phase 0 items in `docs/codex-agent-implementation-plan.md`. Implementation phases (1–5) should be re-anchored against these before any code is written. Several findings simplify the plan; one finding meaningfully shifts the permissions design.

## Probe results

### R1 — MCP Python SDK elicitation hook → ✅ best case

`/Users/jojo/Desktop/operator/venv/lib/python3.11/site-packages/mcp/client/session.py:118` exposes `ClientSession(..., elicitation_callback=ElicitationFnT, ...)`. Default callback returns `INVALID_REQUEST: "Elicitation not supported"`. Capability is auto-advertised in `initialize()` based on whether a non-default callback was provided. Plumbing is one keyword arg in `_ServerHandle._run`.

**Impact on plan:** Phase 1 LOC drops from ~+60–90 to **~+30–50**. No fork, no JSON-RPC bypass. Callback signature: `async def(context: RequestContext, params: ElicitRequestParams) -> ElicitResult | ErrorData`.

### R2 — Tool-name namespace `codex__codex-reply` hyphen → ✅ no collision

`pipeline/mcp_client.py:66-68` splits on `__` (first occurrence), so `codex__codex-reply` parses as server `codex` + tool `codex-reply` cleanly. Hyphen preserved. **No rename to `codex_brain` needed.**

### R5 — `codex login status` output parse → ✅ trivial

Output: `Logged in using ChatGPT`. Single-line literal. `"ChatGPT" in stdout` → subscription auth. Anything else (e.g. `Logged in using API key`) → fail preflight per R5 layer 2.

### Probe 5 (`~/.codex/config.toml` MCP schema) → deferred to v2 ✅

No `~/.codex/config.toml` exists out of the box; created via `codex mcp add ...`. The plan's "v1 ships codex with no inherited MCPs" stance holds — users add via operator's wizard.

### Taxonomy probe — 🟡 simpler than expected, but reshapes the permissions design

Sent 9 prompts under `approval-policy: untrusted` covering read/grep/ls/find/write/compound/python/network/destructive. Observed `parsed_cmd.type` values that **reached our elicitation handler**:

```
unknown (5 occurrences):
  echo abc > /tmp/codex_taxonomy_write.txt
  echo abc > /tmp/codex_taxonomy_write2.txt && cat /tmp/codex_taxonomy_write2.txt
  python3 -c 'print(1+1)'
  curl -sI https://example.com | head -1
  rm /tmp/codex_taxonomy_write.txt
```

Read-class commands (`cat`, `grep`, `ls`, `find`) **never elicited** — auto-allowed by Codex's internal safe-command allowlist before reaching the parent. Their `parsed_cmd.type` values (`list_files`, etc., observed in spike Probe 3a) only show up in `codex/event` notifications, not in `elicitation/create` requests.

**Implication:** every elicitation operator's handler ever sees is typed `unknown`. Per-type matching against an `auto_approve` list keyed on `parsed_cmd.type` does nothing useful. The original plan's `auto_approve: [list_files, read_file]` defaults are dead code.

Compound commands (`echo && cat`, `curl | head`) collapse to a **single `unknown` entry** with the full compound shell string. R6's "strict-AND auto-approve, lenient-OR always-ask" mitigation is unnecessary in compound form — Codex doesn't decompose them, so partial-match auto-approve isn't even possible.

### Amendment shape probe — ✅ exact-argv suppression confirmed

Sent the dict-form decision with the proposed amendment that arrived in the elicitation envelope:

```json
{"jsonrpc":"2.0","id":<elicit_id>,"result":{"decision":{
  "approved_execpolicy_amendment":{
    "proposed_execpolicy_amendment":["/bin/zsh","-lc","echo amendment-test-one > /tmp/amendment_test.txt"]
  }
}}}
```

Then asked Codex to run **the exact same command** in the same thread → **NO elicitation, auto-allowed**. ✅ Suppression works.

Then asked Codex to run a **slightly different argv** (changed `one` to `TWO` in the echo string) → re-elicited. ✅ Amendment scope is exact-argv match, not a pattern.

**Implication:** the amendment is "remember THIS exact `/bin/zsh -lc <exact-string>` for the thread." It helps when users intentionally re-run identical commands (polling a test, retrying an echo). It does NOT generalize to "approve all writes to /tmp" or "approve all curls."

## Plan adjustments — proposed simplifications

### Drop the `permissions.auto_approve` / `always_ask` lists from v1

**Why:** every elicitation reaches the handler typed `unknown`. There's no useful per-type matching to do. Pattern-matching against the `codex_command` argv string is possible but it's new vocabulary, new wizard copy, new tests, and likely unused in v1 (users will just chat-confirm).

**Replacement (simpler):** the codex agent's `config.yaml` has only two permission knobs:
```yaml
permissions:
  default_approval_policy: on-request   # codex's model gates; less noisy than untrusted
  default_sandbox: read-only
```
That's it. `setup.py` step 3.5 for the codex agent is a two-radio-button UI: pick policy, pick sandbox. No tool list.

**LOC impact:** removes the `_BUILTIN_TOOLS_CODEX` work entirely (~30 LOC + tests). Removes the "Wizard step 3.5 vocabulary" risk entirely.

### Use `approval-policy: on-request` as default, not `untrusted`

**Why:** `untrusted` elicits on every non-allowlisted command — that includes `python -c 'print(1+1)'`, `curl -sI`, etc. Way too noisy for meeting chat. `on-request` lets Codex's model judge — it only escalates when the model itself flags risk. Far better default UX. (The probe data above is from `untrusted` mode; production default is `on-request`.)

**Mid-meeting override** still possible: power users can flip via `operator edit codex`.

### "User says yes always" → amendment-form decision

**Why:** the only natural way for the amendment-form decision to fire is **user-explicit consent in chat** ("yes always" / "ok permanently"). The original plan had it firing automatically on auto-approve list hits — but with the auto-approve list deleted, there's no auto path left. User-driven only, which is cleaner anyway.

Behavior:
- Plain "yes" / "ok" / "👍" → `{"decision":"approved"}` (this command, this time).
- "yes always" / "always" / "always yes" → `{"decision":{"approved_execpolicy_amendment":{"proposed_execpolicy_amendment":<argv>}}}` (this exact command, rest of thread).
- "no" / silence past timeout → `{"decision":"abort"}`.

Single regex / `is_yes_always` helper next to `is_yes`.

### `CodexElicitationChatHandler` simplification

Original plan: ~130–170 LOC. With permissions list deleted: **~70–100 LOC**.

Logic shrinks to:
1. Extract `codex_command`, `codex_cwd`, optional `proposed_execpolicy_amendment`.
2. Format chat prompt: `Run \`<command>\` in \`<cwd>\`?` (plain) / full argv (verbose).
3. Block on `runner._await_reply`.
4. `is_yes_always(reply)` → amendment decision; `is_yes(reply)` → plain approved; else abort.

No type-matching, no fnmatch, no AND/OR logic.

## Updated total scope

| Phase | Old LOC | New LOC | Delta |
|---|---|---|---|
| 0 — probes | docs only | docs only | — |
| 1 — elicitation in mcp_client | +60–90 | **+30–50** | −30–40 |
| 2 — provider + handler + config | +250–330 | **+200–270** | −50–60 |
| 3 — agent files | +160–180 | +160–180 | — |
| 4 — wiring | +170–200 | **+140–170** | −30 |
| 5 — tests | +500–600 | **+400–500** | −100 |
| **Total** | **~1,140–1,400** | **~930–1,170** | **−210–230** |

R6 (compound parsed_cmd) downgraded to "non-issue" — Codex doesn't decompose compounds. R2 (namespace) closed. R1 (SDK) resolved to happy path.

## Risk register, updated

| ID | Status | Note |
|---|---|---|
| R1 SDK elicitation hook | ✅ Resolved | Best-case: `elicitation_callback` kwarg available |
| R2 Phase-0 unknowns (taxonomy, amendment, namespace) | ✅ Resolved | Findings above |
| R3 Auto-disable codex brain after 3 errors | unchanged | Special-case still required |
| R4 Late-bind NPE | unchanged | Asserts on both ends |
| R5 Subscription billing leak | ✅ Layer 1+2 confirmed implementable | env-clear works; status parse trivial |
| R6 Compound parsed_cmd auto-approve | ✅ Non-issue | Codex collapses compounds to single `unknown`; auto-approve list deleted anyway |
| R7 CLI version drift | unchanged | Pin 0.128.x, WARN-not-fail |
| R8 Caption deferral UX gap | unchanged | Banner + roadmap entry |
| R9 Test mocking depth | ✅ Resolved | SDK has clean callback hook; standard unit-test patterns work |

## Next step

Either:
- (a) Update `docs/codex-agent-implementation-plan.md` to fold in these adjustments, then proceed to phase 1.
- (b) Talk through the permissions-block deletion before locking — it's the one substantive plan change here.
