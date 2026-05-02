# Code-quality audit (session 178)

Persnickety pass over the runtime layers (~11k LOC). Bar: would a reasonable reviewer block a PR on this?

Smell taxonomy: **faulty** (latent bug or wrong invariant), **overengineered** (abstraction or branch with no payer), **unclean** (duplicated state, leaky boundary, dead code, misleading shape).

Trimmed for review: **10 tier-1 PR-blockers** + **10 tier-2 structural items** in tables; ~30 nits collapsed into a one-line appendix.

---

## Tier 1 — PR-blockers (correctness / shipping bugs)

| # | Location | Smell | Finding | User impact + likelihood | Fix sketch |
|---|----------|-------|---------|--------------------------|------------|
| T1.1 | ~~`__main__.py:866–892`~~ | ~~faulty~~ | ~~`_sync_claude_imports()` runs before `os.environ["OPERATOR_BOT"] = name`.~~ **RESOLVED in session 178**: moved `os.environ["OPERATOR_BOT"] = name` to the first line of `_run_bot`'s post-arg-parse block, before the claude pre-flight gate and the sync. Contract is now enforced by code position, not by comment discipline. Verified pipeline modules touched by the sync (setup, claude_code_import) have no top-level config imports today. | n/a | n/a |
| T1.2 | ~~`config.py:178/225–242`, `setup.py:1013–1056`, four `agents/*/config.yaml` files~~ | ~~unclean~~ | ~~The wizard exposes one free-text input for "system prompt" but the YAML schema, the loader, and four bundled configs all carry two separate fields (`personality`, `ground_rules`) that get joined with `\n\n` at compose time.~~ **RESOLVED in session 178**: collapsed to a single `system_prompt` field. Loader, wizard, four bundled configs (pm/engineer/designer/claude), framework docstring, comments, and tests updated. `personality`/`ground_rules` removed from the schema. | n/a | n/a |
| T1.3 | `chat_runner.py:730–732` and `mcp_client.py:366–368` | faulty | "Strip the LLM's unprompted `limit` from Linear tool calls" hack lives in **two places** with two different implementations. Both silently mutate the model's tool args before render and execute. The user is shown one set of args; another is executed. | **User impact:** when the bot calls a Linear list tool, the user sees one set of arguments in the chat confirmation and a different set actually executes. Surprises like "I asked for 5 results, why did I get 50?" — Linear list calls just don't paginate the way the user expected. **Likelihood:** every Linear list call is affected. Today's behavior is "always strip", so the discrepancy is consistent — but the duplicated layers mean a future fix lands in one place and not the other. | Pick one site (mcp_client), delete the other. Better: fix at the schema/prompt level. |
| T1.4 | ~~`chat_runner.py:828–847`~~ | ~~faulty~~ | ~~`_dispatch_result`'s if/elif/elif/elif chain has no `else`.~~ **RESOLVED in session 178**: added an else-arm that delegates to a unified `_narrate_failure(context, fallback)` helper. Helper hands the offending payload to the LLM via `ask(record=False, tools=None, retry_rate_limits=False)` asking for a plain-text reply; on any narration failure (raise, empty, exhausted retries), posts the hardcoded `fallback`. One-shot — the fallback's reply is rendered inline as a string and never re-enters the dispatcher. `_emit_turn_done(failed=True)` always fires. | n/a | n/a |
| T1.5 | ~~`chat_runner.py:864–873`~~ | ~~faulty~~ | ~~`_handle_load_skill`'s exception branch sends "Couldn't load that skill" and `return`s without calling `_emit_turn_done`.~~ **RESOLVED in session 178**: replaced the generic message with `_narrate_failure(...)` so the user gets a context-specific explanation (rate-limit / network / etc.) instead of "check the logs". Helper handles turn_done(failed=True). | n/a | n/a |
| T1.12 | ~~`chat_runner.py:682–685` (main loop), `chat_runner.py:969–973` (tool-result summary)~~ | ~~faulty~~ | ~~Main loop's exception branch on LLM-call failure logs and emits turn_done but sends nothing to chat — silent failure for the most common code path (every user message). Tool-result summary site posts a generic "couldn't summarize" with no cause hint.~~ **RESOLVED in session 178**: both sites now route through `_narrate_failure(...)`. Main loop gets a context-aware message ("Anthropic is rate-limiting — try again in 30s" etc.); tool summary site additionally inlines the raw tool result (truncated to 600 chars) into the LLM context so the user gets the underlying answer even when the summary call fails. Surfaced an audit gap from cases 2/3 where 429s caused silent or generic failures. Found and fixed in same pass as T1.4/T1.5. | n/a | n/a |
| T1.6 | ~~`providers/anthropic.py:296–323`~~ | ~~faulty (latent)~~ | ~~Streaming retry on `RateLimitError` catches 429s anywhere in the `with stream` block.~~ **RESOLVED in session 178**: added `flushed_any` flag in `complete_streaming`. If a 429 fires after at least one paragraph has reached the user via `on_paragraph`, post a one-line apology (`"Hit a rate limit mid-reply — sorry, that's all I've got for now. Try again in a moment."`) and bail with a synthetic empty `ProviderResponse` so the caller doesn't fire its own narrate fallback on top. Pre-flush 429s still retry as before (safe — `buffer` is reset per attempt). Two new tests in `test_anthropic_provider.py` cover both branches. | n/a | n/a |
| T1.7 | ~~`providers/claude_cli.py:975–987`~~ | ~~faulty~~ | ~~The streaming path computes `accumulated` and `canonical_text`; on divergence, uses canonical.~~ **REVISED + RESOLVED in session 178**: original audit framing was wrong. Both chat and record receive `accumulated` via `on_paragraph` → `_send`; `canonical_text` was returned in `ProviderResponse.text` but ignored downstream because `chat_runner._dispatch_result` skips the resend on `streamed=True`. So canonical was dead computation, not divergence. **Fix shipped:** deleted `canonical_messages` tracking and the divergence-warning block; `final_text` is now always `"".join(full_text_parts).strip()`. The assistant-event handler still walks content blocks, but only to dispatch `tool_use` to `progress_callback`. | n/a | n/a |
| T1.8 | `meeting_record.py:138–142` vs `:144–163` | faulty | In-memory `tail()` returns `_memory[-n:]` with no `session_start` filter. Disk `tail()` scans for the most recent `session_start` and returns only entries after it. Same method, two semantics — tests using in-memory mode see different behavior than production runs against disk. | **No direct user impact today** — `MeetingRecord(slug=None)` (in-memory mode) is only used by `operator try` and tests. **Likelihood of becoming user-visible:** low while `try` is single-session. **Why a reviewer cares:** test coverage diverges from production behavior. Anything we test against in-memory `tail()` doesn't validate the same code path users hit. The session_start guard exists *to prevent the LLM from echoing prior-session replies* (line 96–98) — tests that set up multi-session scenarios in-memory would silently miss this guard. | Track `session_start_idx` for `_memory` too, or fold both branches through one path. |
| T1.9 | ~~`permission_chat_handler.py:105–130` vs `chat_runner.py:769–789`~~ | ~~faulty~~ | ~~Two yes/no matchers with different rules.~~ **RESOLVED in session 178**: created `pipeline/confirmation.py` with a single `is_yes(text) -> bool` exposing the union vocab (yes/ok/okay/sure/approve/approved/confirmed/yep/yeah/y, plus "go ahead"/"do it" phrases) and the negation gate (no/nope/nah/stop/cancel + don't/dont/do not). Both call sites import and delegate: `permission_chat_handler._is_yes` is now `from … import is_yes as _is_yes`; `chat_runner._handle_confirmation` calls `is_yes(text)` directly. New `tests/test_confirmation.py` covers vocab, negation gate, contractions, word-boundary avoidance, and verifies both surfaces delegate. | n/a | n/a |
| T1.10 | ~~`linux_adapter.py:181–193` vs `macos_adapter.py:410–428` (and base)~~ | ~~faulty~~ | ~~Linux's `_process_chat_queue` only handles `send`/`read` — no participant signal.~~ **RESOLVED in session 178**: mirrored mac's full surface to Linux. Added `get_participant_count()` and `get_participant_names()` public methods (queue-routed, with sane timeout defaults), `_do_get_participant_count` and `_do_get_participant_names` private DOM-query helpers (selectors verbatim from macOS — Meet's tile structure is platform-agnostic), and two new arms in `_process_chat_queue`. Linux now flips `saw_others`, enters 1-on-1 mode, and fires alone-grace auto-leave. New `tests/test_linux_adapter_participants.py` covers queue routing, timeout fallbacks, DOM-query happy path, and exception degradation. Note: T2.1 still tracks the larger refactor of pulling shared adapter logic into a mixin/base — this fix is the tactical mirror; the structural dedup is post-launch. | n/a | n/a |
| T1.11 | `google_signin.py:202` vs `macos_adapter.py:598–603` | faulty (latent) | Wizard signs in via real Chrome; runtime adapter uses bundled Chromium. **DEFERRED in session 178** after honest cost-benefit review: no observed failures, theoretical risk only. Updated the stale comment in `google_signin.py:197–201` to accurately describe the current divergence and document the future-fix conditions (Google moves auth cookies to keychain-encrypted slot, or Chrome bumps a profile-DB schema beyond Chromium-for-Testing's range). Revisit when a "you're logged out at runtime" failure surfaces post-launch. One-line fix at that point: `executable_path=str(CHROME_PATH)` in the adapter's `launch_persistent_context`. | n/a | Deferred. |

## Tier 2 — Structural cleanups (would request changes in review)

| # | Location | Smell | Finding | User impact + likelihood | Fix sketch |
|---|----------|-------|---------|--------------------------|------------|
| T2.1 | `__main__.py:920–1093` / `:1096–1206` / `:724–835`; `connectors/macos_adapter.py` (1038 LOC) vs `linux_adapter.py` (631 LOC) | overengineered / unclean | Triple-source-of-truth boot sequences (`_run_macos`/`_run_linux`/`_run_try`) and dual-adapter near-duplicates. ~300 LOC of duplicated entry-point boilerplate; ~600 LOC of duplicated browser-session code. Linux already strictly inferior (no captions, no MutationObserver, no host-admit, no participant signal). | **No direct user impact.** **Why a reviewer cares:** every audit fix you've shipped over the last six sessions has had to be remembered for two-or-three places; T1.10 is one such miss already in production. The cost is paid every time we touch the runtime — both in dev hours and in the bug rate Linux silently inherits. | Extract `_boot_pipeline()` helper for entry; extract `MeetConnector` mixin for adapters. |
| T2.2 | `config.py:1–197` (whole module) | unclean / faulty | Entire module is a script that runs on import: env-var check, YAML load, validation, `SystemExit(2)`, MCP discovery shell-out, deprecation translations. Importing config for any reason triggers it all (and possibly exits). Untestable in isolation. | **No direct user impact.** **Why a reviewer cares:** anything that wants to inspect or test config (a future `operator validate` command, unit tests for cluster 5/6 fixes, a tooling script) has to forge a fake `~/.operator/agents/<bot>/` tree first. Compounds the bug rate of T1-class fixes because we can't easily test them. | Wrap body in `def load_config(bot_name) -> Config:` returning a dataclass. |
| T2.3 | `__main__.py:278–286` | overengineered | `subprocess.Popen.__init__` monkey-patched at module-import time to set `start_new_session=True` for **every** subprocess in the process — Playwright internals, `pgrep`, `claude mcp list`, MCP servers, tests. Intent narrow (Chrome SIGINT containment); blast radius the whole process tree. | **No direct user impact today** — the patch happens to be safe for every subprocess we currently spawn. **Why a reviewer cares:** globally mutating stdlib behavior at module import is the kind of thing nobody expects to find. The next library we add (or upgrade) that *needs* its child to share the parent's process group will fail in surprising ways. **Likelihood it bites:** medium-over-time. | Pass `start_new_session=True` explicitly to the Chrome launch call; revert the global patch. |
| T2.4 | `llm.py:111`, `:144`, `:155`, `:207`, `:196–200` | unclean / faulty | `_system_prompt` mutated via `+=` from four injectors. Only `inject_mcp_status` knows how to remove its own block (via fragile `string.replace`). Calling `inject_skills` or `inject_mcp_hints` twice silently appends a duplicate. Order of calls is load-bearing, encoded only as prose comment. | **User impact when triggered:** if any code path ends up calling `inject_skills` or `inject_mcp_hints` twice in one boot, the LLM sees a doubled prompt — wasted tokens, possibly degraded behavior (instructions repeated at different priority weights). **Likelihood today:** low (each is called once from the entry point), but a future reconnect / re-init path could double up silently. Reviewer's real concern is the comment-as-contract pattern. | Refactor to named segments: `{"name": "skills", "text": ...}`; compose on demand. |
| T2.5 | `llm.py:96–109`; `mcp_client.py:424–443`; `readiness.py:152` | unclean | Three "neutral" abstractions with hardcoded server-specific knowledge. `LLMClient.set_record` calls a method only `ClaudeCLIProvider` implements (violates `feedback_provider_neutral.md`). `MCPClient.resolve_github_user` hardcodes `github__get_me`. `report_mcp_readiness` hardcodes `name == "claude-code"`. | **No direct user impact.** **Why a reviewer cares:** explicit user-stored feedback says "no shims that make one provider mimic another's shape" — and the code does exactly that, in three places. Adding a new provider or a new MCP with similar needs (identity, prereqs) means another `if name == "..."` branch, not a clean override. The abstractions are lying about being neutral. | Promote to neutral hooks (`LLMProvider.set_session_state`, per-server `identity_tool` / `prereq_check` declarations on the bundled config). |
| T2.6 | `chat_runner.py:1086`, `:1199`; `permission_chat_handler.py:280–406`; `chat_runner.py:954` | unclean | `ChatRunner` has become an undeclared shared kernel. Entry layer reads `runner._stop_event.is_set()`. Permission handler reads/writes `runner._send`/`runner._seen_ids`/`runner._own_messages`. Runner imports from `_1_800_operator.agents.engineer.claude_code` for a path constant used by every agent. | **User impact possible but not direct.** A latent race exists between the permission handler and the main loop both polling chat (each can grab the same message; ordering depends on thread scheduling) — under tight timing, a user's "yes" could get processed by the LLM instead of the permission handler, or vice versa. Hard to reproduce, easy to trigger by accident in a future refactor. **Why a reviewer cares:** four private fields and one private method crossed across class boundaries — renaming any of them silently breaks the handler. | Public API (`runner.stopped()`, `runner.send()`, shared `MessageDedupTracker`); hoist constants to a shared module. |
| T2.7 | `setup.py:272/520/737/927/1016/1099/1158` (function names) vs on-screen labels and run() ordering | unclean | Function names (`_step1`, `_step2`, `_step3`, `_step_permissions`, `_step4`, `_step6`, `_step7`) bear no relation to on-screen labels (`1.`, `3.`, `2.`, `4.`, **`4.`** — duplicate, `5.`). Module docstring claims 7 steps; `run()` says 6. Execution order doesn't match either. | **User impact:** every `operator build` user sees two consecutive screens both labeled "4." (Permissions, then System Prompt). Looks broken. **Likelihood:** 100% — every wizard run. Trivial fix; the reason it's not Tier 1 is "it's confusing, not wrong." | Renumber once: rename functions to match screen labels, fix the duplicate `4.`, sync both docstrings. |
| T2.8 | `config.py:49–197` | overengineered | `_validate_config` is 148 LOC of hand-rolled imperative shape-checking. Validates `llm.provider` against an allowlist but doesn't validate `agent.voice` (only fallback at runtime); validates `skills.external_paths` only when `paths` is absent (line 142, an obvious accident). | **User impact possible:** a hand-edited `agent.voice: "wierd"` (typo) silently falls back to "plain" with only a log line — user thinks they set technical voice, doesn't get it. **Why a reviewer cares mostly:** the inconsistency in what's validated is the kind of accident that creeps in over hand-rolled validators. Migrating to pydantic kills three nits at once and shrinks the file by ~120 LOC. | Replace with pydantic/dataclass model + custom error formatter. ~30 LOC. |
| T2.9 | `setup.py:104`, `__main__.py:278–286`, `claude_code_import.py:225` | overengineered | Three modules mutate global state on import: `setup.py` registers a global yaml string-representer affecting all `yaml.safe_dump` callers; `__main__.py` monkey-patches Popen (T2.3); `claude_code_import` holds a module-global cache. Tests pollute each other unless reset. | **No direct user impact today.** **Why a reviewer cares:** import-time side effects are the classic "tests behave differently in isolation vs together" smell — every time we add a test that needs to introspect import-time state, we pay for it. The yaml-representer one in particular bites any future code that wants `yaml.safe_dump` to behave normally. | Local Dumper subclass for setup; revert Popen patch (T2.3); promote cache to a class instance. |
| T2.10 | `macos_adapter.py` selectors + `session.py:detect_page_state` (text matching) | faulty (fragility / i18n) | The whole product hinges on a chain of CSS/text selectors keyed to Meet's English UI (`"Join now"`, `"You can't join this video call"`, `[data-message-id]`, etc.). Non-English locales fail page-state detection entirely. No selector audit at startup, no canary failure mode — failures degrade silently to broad-except logs. | **User impact when Meet redesigns or for non-English users:** the bot fails to join, or joins but can't read/send chat, with no clear error — just a generic "no_join_button" failure that doesn't tell the user *what* moved. Today every selector happens to work; the day Google ships a UI tweak, every user breaks at once. **Likelihood near-term:** Meet UI changes happen ~yearly. i18n is a v1 audience consideration if we're targeting any non-English user. | Add a once-per-startup selector audit: if zero of {join_btn, leave_btn, sign_in} resolve, dump full HTML to `~/.operator/debug/` and surface. Localize text matches to URL/role/data-attr where possible. |

---

## Appendix — Deferred nits (one line each, no fix sketches)

Saved for later. Roughly grouped; nothing here is a shipping risk.

**Style / minor cleanups**
- `__main__.py:916–917` — `or 0` is dead; both branches return ints.
- `__main__.py:645–648` — `_find_oauth_mcp_config` imported but unused.
- `__main__.py:289–340` — `_kill_orphaned_children` silent-failure on missing `pgrep`.
- `__main__.py:557–568` vs `:585` — `_resolve_config_target` not reused by `_run_edit`.
- `__main__.py:144–150` — dirty-check by reload instead of comparing in memory.
- `__main__.py:81–84` — silent `except Exception: return` on cfg load failure.
- `__main__.py:810–833`/`:1055–1078`/`:1179–1192` — `_shutdown_called` idempotency duplicated 3×.
- `chat_runner.py:769–826` — `if affirmative: pass / else: ...` confusing flow.
- `chat_runner.py:1007–1014` — `last = [0.0]` mutable-list-as-closure (use `nonlocal`).
- `chat_runner.py:228` vs `:1052` — `_last_send_time` read/write under different locks (CPython-atomic so benign).
- `chat_runner.py:661–674` vs `:615–616` — slash-skill matching case-sensitive; trigger-phrase matching isn't.
- `chat_runner.py:740–752` — `_request_confirmation` AttributeError if LLM hands non-dict args.
- `chat_runner.py:305`/`:322–350` vs `:421–431` — MCP failure banner posts before `saw_others`.
- `chat_runner.py:140–141`/`:570` — `_pre_intro_buffer` unbounded (BaseException recovery path).
- `chat_runner.py:112`/`:114` — `_seen_ids`, `_own_messages` unbounded.
- `providers/openai.py:128–222` (vs llm.py:336) — streaming + tool_call drops pre-tool-call text from the model's later context.
- `providers/anthropic.py:176–178` — `_is_context_overflow` substring-matches SDK error text.
- `providers/claude_cli.py:720–747` / `:869–952` — `while/else` deadline pattern (confusing idiom).
- `providers/claude_cli.py:166`/`:515–551` — `_turn_history` unbounded; restart shovels everything into one user envelope.
- `mcp_client.py:90–115` — `_AUTH_ERROR_PATTERNS` matches "forbidden"/"unauthorized" without word boundary (false-positive bias).
- `mcp_client.py:171–181` — `_classify_startup_failure` substring-matches anyio messages.
- `mcp_client.py:285–333` — success-on-disabled-server resets counters; trip can be silently undone.
- `mcp_client.py:445–461` — `shutdown()` doesn't cancel in-flight tool futures (hangs at meeting end).
- `mcp_client.py:519–542` — `future.cancel()` doesn't stop the running coroutine; session deadlocks.
- `mcp_client.py:435`/`:528` — inline `import json as _json`/`import concurrent.futures as _cf`.
- `mcp_client.py:500–508` — `_session` not declared in `_ServerHandle.__init__`.
- `meeting_record.py:131–163` — `tail()` reads entire JSONL on every call (perf).
- `meeting_record.py:120–128` — append divergence: memory has it, disk doesn't (carry-over from S177 cluster 6).
- `meeting_record.py` whole file — unbounded JSONL per slug across rejoins.
- `meeting_record.py:122–125` — file open/close per append inside lock (perf).
- `guardrails.py:79–85` — `_BASE64_IMAGE_PREFIXES` mixes base64 prefixes with `"data:image/"` URI scheme.
- `guardrails.py:120–127` — non-printable ratio threshold 10% over 4096 chars (false-positive on i18n).
- `permission_chat_handler.py:169–236` and `setup.py:910–924` — two enumerations of Claude Code built-in tools.
- `permission_chat_handler.py:377–406` and `chat_runner.py:467–471` — race between handler and runner polling chat.
- `permission_chat_handler.py:36–68` — `_disabled_mcp_for_cli_tool` heuristic name normalization.
- `permission_bridge.py:62–115` — eight failure modes all funnel to permanent deny (no retry/ask).
- `connectors/base.py` — inconsistent abstract-vs-default policy across methods.
- `connectors/base.py:25–30` and `chat_runner.py:399–403` — `count == 0` ambiguous (alone vs unknown).
- `connectors/base.py` (missing) vs `__main__.py:1015` — `wait_for_resolved_url` is mac-only but called via base typed reference.
- `session.py:37–46` — Chrome `SingletonLock` parse format reverse-engineered; failure-to-parse means "lock absent" (hostile).
- `session.py:62–138` and `__main__.py:1062–1068` — `.operator.kill_reason` IPC without atomic write.
- `terminal.py:47–49`/`:74–78` — `os.kill(os.getpid(), SIGINT)` for shutdown (self-signal as control flow).
- `macos_adapter.py:215–248` — send-ID readback race against multi-participant chat (carry-over from S177 cluster 5).
- `macos_adapter.py:565–1038` — `_browser_session` is one 470-line method, 6 levels of indentation.
- `macos_adapter.py:565–584` — TOCTOU between `_chrome_lock_is_live` check and `_chrome_kill_and_clear`; PID reuse risk.
- `macos_adapter.py:260–332` — MutationObserver JS as Python f-string with quadruple-escaped regex.
- `macos_adapter.py:837–942` — admit-pill stickiness handling is 100 LOC of nested branches.
- `macos_adapter.py:1008–1011` + `:527` — two layers of workaround for Playwright's persistent-context hang.
- `linux_adapter.py:75–80` vs `macos_adapter.py:101–114` — `send_chat` raises `queue.Empty` on linux, returns None on mac (different contract).
- `auth.py:28–51` — `find_oauth_mcp_config` returns first matching bot's config without validating consistency.
- `google_signin.py:128–134` — `_capture_email` regex-scans full body text; first match wins.
- `oauth_cache.py:24–28` — lexicographic sort of `mcp-remote-*` dirs breaks at `0.10` vs `0.9`.
- `google_signin.py:35–42` and `setup.py:69–75` — path constants inlined in three places with "keep in lockstep" comments.
- `chrome_preflight.py:20` — single hardcoded Chrome path; users with non-standard installs fail preflight.
- `claude_code_import.py:225`/`:240–261` — module-global `_CLAUDE_MCP_LIST_CACHE` with documented "set to None to bust" reset.
- `claude_code_import.py:213–215` — regex parses `claude mcp list` text output (no `--json` form attempted).
- `claude_code_import.py:264–271` — `_slugify_mcp_name` non-injective; collisions silently overwrite.
- `claude_code_import.py:497–535` — `append_env_placeholders` non-atomic; concurrent `_sync_claude_imports` could double-append.
- `skills.py:58`/`:114–128` — `SUPPORTED_ALLOWED_TOOLS = {"load_skill"}`; non-conforming skills warn but load anyway (decorative validation).
- `skills.py:91–96` — `text.split("---", 2)` truncates body if frontmatter or top-of-body contains `---`.
- `picker.py:222–224`/`:281–286` — magic numbers in layout math, "mirrors `build_card.width_for()`" by comment.
- `picker.py:233`/`:295` — `Live(refresh_per_second=30)` for a keypress-driven UI.
- `setup.py:295–308` — `_step1_fighter_select` recurses on its own for retry instead of looping.
- `setup.py:383` — `state._reset_backup_path = ...  # type: ignore` extending dataclass via untyped attr.
- `setup.py:1127–1152` — `_parse_env`/`_append_env` re-implement dotenv when `python-dotenv` is already a dep.
- `setup.py:1360–1362` — prints "✗ build failed" then re-raises (user sees one-liner + traceback).
- `setup.py:1099–1124`/`:1368–1378` — `_step6_api_keys` always says "nothing to prompt for" on the claude preset.
- `setup.py:125`/`:1174`/`:1198` — `mode: str = "new" | "edit"` stringly typed.
- `setup.py:1167` — wizard temp dirs leak under `~/.operator/agents/.{name}.tmp-*` on crash.
- `config.py:228–238` — `_load_framework_system_prompt` only works for bundled agents (not `~/.operator/agents/<name>`).
- `config.py:537–556` — `_resolve_env_vars` only handles whole-string `${VAR}`; partial interpolation silently passes.
- `config.py:303–306` — CLAUDE.md mirror gated on provider rather than list-was-configured.
- `config.py:399–424`/`:330–361`/`:643–676` — three deprecation translations with no removal date; `PERMISSION_VERBOSITY` shim unaudited.
- `config.py:583` — `{"_track_a_toggle_only": True}` magic string.
- `config.py:657–676` — module-level `PERMISSIONS_*` "constants" mutated in-place during import.

---

**Recurring patterns (one fix closes several):**
- Substring-matching against vendor error messages (anthropic, anyio, GitHub auth, Meet UI).
- Server-specific knowledge inside neutral abstractions (T2.5 covers the load-bearing instances).
- Module-import side effects (T2.9).
- Triple-implementation duplication (T2.1).
- Leaky abstractions to ChatRunner (T2.6).
- Path/layout constants kept in sync by comment, not code.
- Unbounded sets / strings / files over long meetings.

**Carry-overs from session 177:**
- `_do_send_chat` ID-readback race (in nits).
- `MeetingRecord.append` memory/disk divergence (in nits).
- `_on_tool_use` docstring drift — not flagged; one-line fix to ride along on next chat_runner edit.
