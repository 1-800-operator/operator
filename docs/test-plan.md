# Test Plan — component-scoped coverage

Built session 130 (2026-04-19) to address the accumulated test debt before launch. The goal is to pick up coverage one component at a time, without ever holding the whole codebase in one session's context.

## Ground rules

1. **One component per session** (sometimes two small ones). Stop at component boundaries — do not drift into neighbors.
2. **Inventory first, write second.** Open the component, list uncovered behaviors as a checklist, let the user pick which to write.
3. **Follow existing test conventions.** Standalone scripts under `tests/`, no pytest runner, runnable as `python tests/test_<name>.py` from an active venv. Mirror the structure of `test_911_size_management.py` and friends.
4. **Edge-case depth is declared, not improvised.** See "Edge-case depth" below — pick a level per component, write it into the session's inventory before writing any code.

## Edge-case depth — locked in

Every component gets a declared target level. Do not silently escalate; if you want to go deeper, say so to the user and get sign-off.

| Level | What it covers | Count per component |
|---|---|---|
| Smoke | Happy path only | 1–2 |
| **Boundary** *(default for never-tested components)* | Happy path + obvious nulls, empties, caps, missing keys, disabled flags | 3–6 |
| **Boundary + race** *(carve-out for concurrency-surfacing components)* | Boundary + 1–2 obvious concurrency / race cases that would actually happen in production | 5–8 |
| Gap-fill *(default for partially-tested components)* | Only branches not already covered by existing tests — read the existing test first | varies |
| Adversarial | Boundary + malformed input, partial failures, races, unicode weirdness | 8–15 |
| Exhaustive | All branches, all error paths | 20+ |

**Locked pre-launch policy:**

- Never-tested components → **Boundary**.
- Partially-tested components → **Gap-fill**.
- **Carve-outs** with concurrency surfaces → **Boundary + race**:
  - B — MeetingRecord (JSONL append from multiple callers, partial writes, tail racing with append)
  - F — MCPClient (tool timeout racing with reconnect, orphan cleanup during active call)

Adversarial and Exhaustive are **explicitly deferred** to post-launch. The 15h budget to April 19 does not have room for them, and the ROI-per-hour drops fast past Boundary.

## Testing layers — locked in

Three layers, each with its own mechanism and reach. Do not mix mechanisms across layers. In particular, **do not build formal `Fake*` provider classes** (see note below).

| Layer | Mechanism | Runs unattended? | Target reach |
|---|---|---|---|
| **L1 — Unit, automated** | Inline `MagicMock` + per-test stubs, matching the `test_9xx` pattern. Standalone `python tests/test_*.py`. | ✅ yes — model runs every session | ~80% of component behaviors |
| **L2 — Integration, automated** | `operator try <bot>` driven via piped stdin against real LLM + real MCP. Lightweight `tests/_helpers.py` emerges as duplication surfaces. | ✅ yes — model runs with harness | ~15% — tool-loop round-trips, 1-on-1 trigger, auto-leave, reconnect |
| **L3 — Manual, real-world** | Live Google Meet, real captions, real lobby, human judgment on LLM response quality | ❌ no — user runs before release | ~5% — release smoke only |

### Why no formal `Fake*` provider classes

Considered and rejected. A `FakeLLMProvider` / `FakeMCPServer` / `FakeConnector` would:

1. Duplicate what `MagicMock` already does well in the existing `test_9xx` suite.
2. Drift from real provider behavior over time — tests pass against fake semantics while prod breaks (violates parity-first).
3. Ship new untested code whose only purpose is testing other code. Net negative.

The `TerminalConnector` used by `operator try` already *is* the honest integration harness — it's a real `MeetingConnector` subclass, not a fake. Extend it (e.g. a `--script` mode for deterministic replay) if L2 needs more reach. Do not spawn parallel fake classes.

### When to extract a shared test helper

Only when duplication has already occurred — never upfront. If two tests end up writing the same MCP-mock setup or the same `MeetingRecord` tmpdir scaffolding, extract a function into `tests/_helpers.py`. That's shared utilities, not a framework.

## Component map

Ordered by risk × value (most valuable first). Each session picks the next unchecked one.

| # | Component | Files | Existing tests | Target depth | Size | Done? |
|---|---|---|---|---|---|---|
| B | **MeetingRecord** — JSONL append, `tail(n)`, meta header, slug uniqueness | `pipeline/meeting_record.py` | `test_meeting_record.py` (session 133) | **Boundary + race** | S | ☑ |
| A | **Config loader** — YAML→module, env precedence, disabled-server filter, `tool_timeout_seconds` override | `config.py` | `test_config_loader.py` (session 133) | Boundary | S | ☑ |
| C | **LLMClient** — prompt shape, scratchpad merging, skill + MCP hint injection, record tail wiring | `pipeline/llm.py` | `test_llm_client.py` (session 133) | Boundary | M | ☑ |
| G | **Skills loader** | `pipeline/skills.py` | `tests/test_skills.py` (extended session 133) | Gap-fill | S | ☑ |
| Setup | **Setup wizard** — picker, card, path writes | `pipeline/setup.py`, `pipeline/picker.py`, `pipeline/build_card.py` | `tests/test_setup.py` (extended session 134) | Gap-fill | M | ☑ |
| F | **MCPClient** — reconnect, backoff, `tool_timeout_for`, orphan cleanup, `server_for_tool` | `pipeline/mcp_client.py` | `test_mcp_client.py`, `test_mcp_shutdown.py`, `test_mcp_client_units.py` (session 133) | Gap-fill + race | M | ☑ |
| E | **ChatRunner** — trigger, 1-on-1, auto-leave, confirm flow | `pipeline/chat_runner.py` | `test_chat_hardening.py`, `test_911/912/913/915*.py`, `test_chat_runner_gaps.py` (session 134) | Gap-fill | M | ☑ |
| D | **OpenAI provider parity** | `pipeline/providers/openai.py` vs `pipeline/providers/anthropic.py` | `test_anthropic_provider.py`, `test_openai_provider.py` (session 135) | Boundary | M | ☑ |
| Entry | **CLI entry + `operator try`** — arg parsing, bot discovery, `_run_try` wiring | `__main__.py` | `test_entry_cli.py` (session 135) | Boundary | M | ☑ |
| I | **Guardrails** | `pipeline/guardrails.py` | `test_guardrails.py` (extended session 135) | Gap-fill | S | ☑ |
| H | **Connectors** — terminal (unit), session state machine; macos/linux adapters are integration-only | `connectors/terminal.py`, `connectors/session.py`, `connectors/macos_adapter.py`, `connectors/linux_adapter.py` | `test_connectors.py` (session 135) — macos/linux adapters remain manual/Playwright-driven | Boundary (unit) / manual (integration) | S unit / L integration | ☑ unit / ☐ integration |

## Per-session protocol

1. **Before opening files:** re-read this document, pick the next unchecked component, note its target depth.
2. **Read the component source and any existing tests.** For gap-fill, explicitly list what existing tests already cover.
3. **Produce an inventory** — a bulleted list of behaviors to cover at the chosen depth. Share with the user before writing code.
4. **User picks which to write** (or approves all).
5. **Write one test at a time.** Run it. Move on.
6. **Update this file** — check the box in the component map, note the session and the test file(s) added.
7. **Handoff note** mentions which component was done and which is next.

## Notes and decisions

*(Add session-level notes here as components are completed — what the uncovered gaps actually were, any surprises, any decisions to revisit.)*

- **B — MeetingRecord** (session 133, 2026-04-19) — 8 tests added to `tests/test_meeting_record.py`, all pass. No production bugs surfaced; the `_lock` contract holds under 10×20 concurrent appends, and tail()/append() interleaving is clean.
- **A — Config loader** (session 133, 2026-04-19) — 6 tests added to `tests/test_config_loader.py`, all pass. Covers missing/unknown `OPERATOR_BOT`, yaml field parse + defaults, `SYSTEM_PROMPT` composition (personality + ground_rules), `intro_on_join` default-True, and MCP server filter/`tool_timeout_seconds`/`${VAR}` env resolution. Loader tests use a tmp `config.py` copy + tmp `agents/<bot>/config.yaml` so no real agents are touched.
- **C — LLMClient** (session 133, 2026-04-19) — 6 tests added to `tests/test_llm_client.py`, all pass. Covers `ask()` wiring (system+tail), `_tail_messages` shape (agent→assistant, user prefix, caption `[spoken]` branch, first-contact hint attached once per first name), `ask()` tool_call scratchpad seeding, `send_tool_result` scratch-clear on final text, `ContextOverflowError` halving `_max_messages` (floor 2), and `intro()` single-shot + exception propagation. **Confirmed behavior**: captions never attach the first-contact hint and don't mark the speaker as greeted — ambient spoken talk doesn't trigger a direct greeting.
- **G — Skills loader** (session 133, 2026-04-19) — 5 gap-fill tests appended to `tests/test_skills.py`: no frontmatter skipped, unterminated frontmatter skipped, non-dict frontmatter skipped, allowed-tools comma-string parsed, empty parent folder warns. All 18 now pass. **Incidental fix**: test_skills.py was missing `os.environ.setdefault("OPERATOR_BOT", "pm")` at the top — the existing LLM/ChatRunner wiring tests (9-13 in the __main__ list) had been silently failing on `SystemExit` from `config.py` before running their body. One-line fix added; all pre-existing tests now actually execute.
- **F — MCPClient** (session 133, 2026-04-19) — 6 gap-fill tests added to new `tests/test_mcp_client_units.py`, all pass. Covers `_classify_startup_failure` (FileNotFoundError + "process exited" branches + BaseExceptionGroup unwrap), `server_for_tool` + `tool_timeout_for` override precedence, Linear `limit` arg stripping in `execute_tool`, and the `get_file_contents` binary-extension guardrail firing pre-execution. **Race carve-out deviation**: the plan flagged "tool timeout racing with reconnect" but there is no reconnect path in current mcp_client.py (each `_ServerHandle` runs once per session); "orphan cleanup during active call" is already covered by `test_mcp_shutdown.py`. So the "+race" portion of the carve-out has no target in the current code — revisit if reconnect is added post-launch. Next: **E — ChatRunner** (Gap-fill, M) or **Setup** wizard (Gap-fill, M).
- **E — ChatRunner** (session 134, 2026-04-19) — 19 gap-fill tests added to new `tests/test_chat_runner_gaps.py`, all pass. Six buckets: **M1** 1-on-1 dispatch + participant auto-leave (grace timer); **M2** `_needs_confirmation` three branches + `_handle_confirmation` affirmative/correction + record `kind="confirmation"` tagging; **M3** `_tools_for_llm` progressive gating, `/skill-name` slash fast path (known + unknown), `_handle_load_skill` valid + unknown-name; **M4** intro-on-join post-once + buffer drain, empty-intro still drains, `INTRO_ON_JOIN=False` no buffering; **M5** `_record_mcp_outcome` trip → notify + `inject_mcp_status`, no-trip silent; **M6** `_send` own-message tracking + on-failure discard. **Incidental** (also session 134): `test_911`, `test_913`, `test_915` gained the one-line `os.environ.setdefault("OPERATOR_BOT", "pm")` fix so they execute their bodies standalone instead of silently exiting — same hazard session 133 patched in `test_skills.py`.
- **Setup — Setup wizard** (session 134, 2026-04-19) — 14 gap-fill tests appended to `tests/test_setup.py` (29/29 now pass). Six buckets: **S1** `_parse_env` direct — quote-stripping (single + double), `#` comments + blank-line skipping, malformed (no `=`) lines tolerated; **S2** `_append_env` creates the .env when the file doesn't exist; **S3** `_collect_env_refs` gathers `${VAR}` from enabled servers only (skips disabled), empty-set when no MCPs; **S4** `_is_valid_skill_source` accepts the three shapes (md / SKILL.md folder / parent-of-SKILL.md) and rejects empty dirs + non-md files (documented: .md shape accepts any .md, doesn't re-check frontmatter — that's the loader's job); **S5** `build_card.render` Panel-mode (non-rainbow) returns `Panel` with title + fields landed; rainbow-mode returns `Text` with frame glyphs; `_compose_body` renders `—` for empty power_ups/skills; `_wrap_cells` hard-splits wide tokens on code-point boundaries; **S6** `_first_line` first-non-empty + ellipsis truncation + empty input. Next: **D — OpenAI provider parity** (Boundary, M) or **Entry — CLI entry + `operator try`** (Boundary, M).
- **D — OpenAI provider parity** (session 135, 2026-04-19) — 12 boundary tests added to new `tests/test_openai_provider.py`, all pass. Mirrors `test_anthropic_provider.py` shape. **Request translation**: system prepended as role:"system" message, empty system omitted, neutral assistant `tool_calls` → OpenAI `tool_calls` with JSON-string `function.arguments` + tool_result → role:"tool" + `tool_call_id`, assistant text + tool_call preserved side-by-side, tools pass through with `parallel_tool_calls=False`, no-tools case omits both kwargs. **Response translation**: plain text (`stop`→"end"), `tool_calls` present → "tool_use" (derived from tool_calls, not finish_reason), `length` → "length", `content_filter` → "other". **Error translation**: `BadRequestError.code == "context_length_exceeded"` → `ContextOverflowError`; other codes propagate as-is.
- **Entry — CLI entry + `operator try`** (session 135, 2026-04-19) — 18 boundary tests added to new `tests/test_entry_cli.py`, all pass. Three buckets: **bot discovery** — `_available_bots` filters dirs with `config.yaml` + sorts + tolerates missing agents dir; `_bot_tagline` prefers yaml `agent.tagline`, falls back to first non-header README line, empty when neither; **main() dispatch** — no-args/`-h`/`--help` print usage + return 0; `list`/`setup`/`try <name>` route to their handlers; `try` alone returns 2; unknown flag + unknown bot/subcommand return 2; known bot routes to `_run_bot(name, rest)`; **arg parsing** — `_run_bot` parses url + `--force`/`--check-mcp`/`--plain` and sets `OPERATOR_BOT` before dispatch; `--check-mcp` short-circuits to `_check_mcp()` and returns its code without calling the platform runner; unknown flag returns 2; **`_run_try`** — unknown bot bails early (before any `import config`). Loaded `__main__.py` via `importlib.util.spec_from_file_location` to avoid re-executing the script guard; dispatch targets (`_run_list`, `_run_setup`, `_run_try`, `_run_bot`, `_check_mcp`, `_run_macos`, `_run_linux`) patched per-test via a context manager.
- **I — Guardrails** (session 135, 2026-04-19) — 5 gap-fill tests appended to `tests/test_guardrails.py` (22/22 now pass). Covers: **GIF (`R0lGOD`) + WebP (`UklGR`) base64 prefixes** flagged (two branches that existed but weren't exercised); **base64 prefix beyond 1000-char window** passes — boundary for the sliding head window; **non-printable ratio at the 10% threshold** — check is strict `>`, so exactly 10% passes and 11% fails; **`\t`/`\n`/`\r` are whitelisted** — 100% whitespace controls still pass; **`log_rejection`** emits a single WARNING with stage/tool/reason/args, and `default=str` handles non-JSON-serializable argument values. **Incidental fix** (same hazard as session 133/134): added `os.environ.setdefault("OPERATOR_BOT", "pm")` so standalone `python tests/test_guardrails.py` runs don't SystemExit on the `pipeline.mcp_client`/`pipeline.llm` imports inside the integration tests.
- **H — Connectors (unit portion)** (session 135, 2026-04-19) — 11 boundary tests added to new `tests/test_connectors.py`, all pass. **TerminalConnector (5)**: `join()` no-op returns None; `send_chat` prints `[bot]` prefix + message; `read_chat` drains the queue with incremental `term-N` ids + `sender="you"` (and returns `[]` when empty); `/quit` and `/exit` (even surrounded by whitespace) route through `os.kill(getpid(), SIGINT)` — verified with `os.kill` patched so the test process survives; `get_participant_count() == 2` (1-on-1 threshold always holds) + `leave()` flips `is_connected` + `set_caption_callback` no-ops cleanly. Used `patch("connectors.terminal.threading.Thread")` at construction to avoid spinning up the real stdin-reading daemon thread (which would EOF in-test and SIGINT the runner). **session.py pure helpers (6)**: `JoinStatus` initial state + `signal_success(recovered=True)` + `signal_failure("reason")` transitions; `validate_auth_state` rejection branches (None, empty string, missing file, bad JSON, no `.google.com` SID cookie); `validate_auth_state` happy path returns the parsed dict; `inject_cookies` filters non-`.google.com` cookies and returns True after calling `context.add_cookies` exactly once with only the two allowed cookies; `inject_cookies` returns False for empty-filter + exception branches; `_chrome_lock_is_live` three branches — nonexistent lock → False, symlink→self-pid → True, symlink→dead-pid → False. Same `OPERATOR_BOT` setdefault hazard hit this file too (connector imports). **Integration portion still ☐**: macos/linux adapters + `detect_page_state` + `save_debug` stay as manual/Playwright-driven tests per the plan. **Test-plan sprint complete for unit coverage across all 11 components.**

- **Self-intro on join** (added session 131, 2026-04-19) — must appear in the inventory of three components when their session comes up, even at Boundary depth:
  - **A — Config loader**: `agent.intro_on_join` reads as `True` when present, defaults to `True` when absent.
  - **C — LLMClient**: `intro()` issues a single `provider.complete` call with no message history, returns the trimmed text; on provider exception the caller (ChatRunner) is responsible — `intro()` itself does not catch.
  - **E — ChatRunner**: when `INTRO_ON_JOIN=True`, the background `_generate_intro` thread fires after join; main loop posts `_intro_text` exactly once and drains `_pre_intro_buffer` in order; messages arriving pre-intro are persisted to the record but their LLM dispatch is deferred; on intro-gen failure (`_intro_text=""`) the post is skipped silently and the buffer still drains; when `INTRO_ON_JOIN=False`, no thread spawns, no buffering occurs, processing is immediate.

---

## What's intentionally excluded

- **Live Meet integration tests.** `test_playwright*.py` and `test_meet_guest.py` exist and should continue to be run manually when shipping; formalizing them into a CI suite is a post-launch goal.
- **LLM response quality.** We do not test the model's judgment — only that we call it correctly and parse its output correctly. Response-quality regressions are caught by using the bots.
- **External service availability.** Linear, GitHub, Figma APIs are mocked or stubbed in tests. Live connectivity is caught by `operator try <bot>` smoke checks.
