# Pre-Launch Audit

*Methodical inspection passes to catch slop and embarrassment risks before flipping the repo public. Cross items off as we complete them.*

**Context:** Operator is a local CLI that runs on the user's machine with their API keys — no auth system, no payments, no DB, no servers. The "iceberg" for this product is narrower than a typical SaaS: install flow, live-meeting failure modes, secrets handling, crash/disconnect behavior, and dependency drift.

**Audit shape (S199 refresh).** The codebase grew ~15 new components since the last audit (slip-mode attach adapter, claude-cli provider, permission bridge, setup-preflight stack, audio pipeline). For each Tier-1 component we apply **all four lenses** in one sitting (security / edge case / PR review / AI slop) rather than sweeping one lens across the whole tree. Standalone workflow passes — install dry-run, `/ultrareview`, `/security-review`, dep pinning, runbook — stay separate at the bottom.

---

## Component matrix (Tier 1 = hot path, Tier 2 = supporting, Tier 3 = setup/cold path)

For each component, apply the four lens checklists below. Mark `done` / `findings → log` / `clean`.

### Tier 1 — live-meeting hot path (review first)

- [x] `pipeline/chat_runner.py` (619 → ~610 LOC) — DONE in commit `0ac3a6a` (S199): narration redaction, loop split, safe leave, drift docs purged.
- [x] `pipeline/llm.py` (415 → 267 LOC) — DONE in commit `091a348` (S199): purged dead tool-loop infra (`ask_stream`, `send_tool_result`, `extra_system`, `tools=`, `_scratch`, `wrap_tool_result` and friends), sanitized intro participant names, deleted `pipeline/guardrails.py` outright (had no callers post-pivot). Net -921 LOC across source + tests.
- [ ] `pipeline/providers/claude_cli.py` (1063 LOC) — claude subprocess + permission bridge (largest single file). **Carries deferred findings — see Findings log below.**
- [x] `pipeline/providers/base.py` (144 → 122 LOC) — DONE in commit `8e64bd2` (S200): deleted unused `ContextOverflowError` class + `complete_stream()` abstract method (distinct from live `complete_streaming()`, never implemented or called) + stale "Raises ContextOverflowError" docstring line + muddled "Tightened to require..." comment above `_PARAGRAPH_BOUNDARY_RE` that described a tightening never applied. Also dropped `ContextOverflowError` from `providers/__init__.py` exports.
- [x] `pipeline/meeting_record.py` (163 → ~152 LOC) — DONE in commit `de013fe` (S200). Two cuts. (1) Merged the two consecutive `self.path.open("a")` blocks at __init__ time into one open so meta-header + session_start land atomically. (2) After a "in which cases would a disk write actually fail" gut check — disk full, broken perms, RO remount; all rare-and-bigger-problems-than-operator on a user's Mac — deleted the defensive `try/except OSError` scaffolding around all file writes (boot writes + `append()`) and the dead `_memory.append()` shadow store that was only ever read in the no-path test mode but kept getting populated unconditionally. Trust the OS for normal user-file writes; raise loudly if it ever fails. As a bonus: two transcript-dedupe tests were silently relying on the memory-fallback path (reading `tail()` AFTER `tempfile.TemporaryDirectory()` cleanup), now correctly read inside the `with` block. Magic-string `kind` values still flagged for a multi-file cleanup pass.
- [x] `pipeline/confirmation.py` (95 → 51 LOC) — DONE in commit `d3cb63c` (S200): deleted unused `is_yes_always` + `_AFFIRM_ALWAYS_RE` (zero callers; the codex elicitation handler it was built for never landed), and rewrote the docstring to drop stale "track A vs track B" framing + nonexistent references to `chat_runner._handle_confirmation` and "openai/anthropic providers" — today there's exactly one caller (`permission_chat_handler` for claude_cli's PreToolUse hook). Net -44 LOC.
- [ ] `connectors/macos_adapter.py` (977 LOC) — Playwright + persistent Chrome profile
- [ ] `connectors/attach_adapter.py` (959 LOC) — slip-mode attach to existing Chrome
- [ ] `connectors/session.py` (277 LOC) — JoinStatus + session bookkeeping
- [x] `pipeline/transcript.py` (157 → 165 LOC) — DONE in commit `67c2939` (S200): two thread-safety fixes. (1) Closed a race on `_last_window_per_speaker` dict — three threads call `_emit` (silence loop, browser caption thread, main-thread `stop()`), the read-then-write of the per-speaker window was outside the existing lock. Now reads prior + writes new window in one critical section. (2) Moved the catch-all `try/except Exception` from `_emit` into `_silence_loop` only — the silence-loop call site is the only one where a raised exception silently kills a daemon thread (browser + stop call sites already have their own logging guards at the connector layer). Net consistent with cell 5's trust-the-OS principle. Caption-text-in-INFO-log finding flagged for cross-file logging-policy review.
- [x] `pipeline/audio.py` (173 → 171 LOC) — DONE in commit `02ec650` (S200): deleted unused `BYTES_PER_SAMPLE` constant (zero references in src/ or tests/) and removed redundant `.strip()` on whisper output (`transcribe()` already returns stripped text). Caption-text-in-INFO-log finding recurs at line 136 — same as transcript.py, both rolled into the cross-file logging-policy follow-up. Other small nits (frozenset for hallucinations, staticmethod for `_is_repetition_hallucination`, `bytearray` over `+=` accumulation, `_transcribe` naming) consciously left as stylistic.

### Tier 2 — supporting infrastructure

- [ ] `__main__.py` (709 LOC) — CLI dispatch (dial/slip/doctor), claude-import sync, legacy migration
- [ ] `config.py` (99 LOC) — per-agent config loader + INTERNAL TUNING constants
- [ ] `pipeline/permission_chat_handler.py` (420 LOC) — PreToolUse → chat round-trip
- [ ] `pipeline/permission_bridge.py` (123 LOC) — bridge plumbing
- [ ] `connectors/captions_js.py` (214 LOC) — caption MutationObserver payload (JS string)
- [ ] `connectors/chat_dom_js.py` (157 LOC) — chat DOM payload (JS string)
- [ ] `connectors/base.py` (58 LOC) — abstract MeetingConnector
- [ ] `bridges/claude.py` (81 LOC) — claude preset hooks
- [ ] `mcp_servers/transcript_server.py` (425 LOC) — bundled transcript MCP

### Tier 3 — setup / cold path / platform-secondary

- [ ] `pipeline/install_preflight.py` (175 LOC)
- [ ] `pipeline/doctor.py` (282 LOC)
- [ ] `pipeline/readiness.py` (360 LOC)
- [ ] `pipeline/google_signin.py` (334 LOC)
- [ ] `pipeline/chrome_preflight.py` (62 LOC)
- [ ] `pipeline/claude_code_import.py` (68 LOC)
- [ ] `pipeline/oauth_cache.py` (45 LOC)
- [ ] `pipeline/_disclaimed_spawn.py` (269 LOC) — TCC-disclaim wrapper
- [ ] `pipeline/ui.py` (68 LOC)
- [ ] `connectors/linux_adapter.py` (701 LOC) — not daily-driver
- [ ] `install.sh` + `pyproject.toml` + `requirements.txt`
- [ ] `src/_1_800_operator/agents/{claude,codex}/` preset configs
- [ ] audio-helper Swift binary (sources tracked separately; review pre-notarize in 14.20.5)

---

## The four lenses (apply each to every Tier-1 component)

### Lens A — Security

- **Disk-write sites:** every `open(..., "w")` / `Path.write_*` / log call — does it ever land secrets, full tool-args containing tokens, or user PII? Targets: `~/.operator/debug/`, `/tmp/operator.log`, `~/.operator/history/*.jsonl`, debug dumps.
- **Egress sites:** every place text leaves the box — Meet chat (`send_chat`), LLM prompt body, MCP tool args. Could a misbehaving MCP echo `cat .env` content into chat?
- **Subprocess sites:** every `subprocess` / `Popen` / `asyncio.create_subprocess_exec` — argument injection from user input or LLM-generated tool args? unsanitized env passthrough?
- **File-mode + path trust:** are secret files (`~/.operator/.env`, `auth_state.json`) created/maintained at 0600? do we follow user-controlled paths from config without resolving symlinks?

### Lens B — Edge case (live-meeting failure paths)

- **Network/IO failure:** API 5xx mid-turn, MCP server crash, Chrome killed, network blip 30s — graceful chat message vs. silent hang vs. zombie process?
- **Races:** two `@operator` messages 200ms apart, bot's own send re-triggering itself, confirmation prompt arriving while user mid-sentence, captions toggled mid-meet.
- **Boundary inputs:** empty trigger (`@operator` then nothing), 200KB tool result, binary/null-byte payload, 0 other participants, lobby timeout, MCP returns malformed JSON.
- **State cleanup:** dismissed confirmation, mid-tool-loop disconnect, partial join (chat panel never opened), participant churn during `ALONE_EXIT_GRACE_SECONDS` countdown.

### Lens C — PR review (senior engineer skimming the diff)

- **Hot-path complexity:** any single function >50 LOC doing 3+ unrelated things — split, or document why it can't be.
- **Error handling shape:** bare `except:` or broad `except Exception:` swallowing real bugs? Errors caught at the wrong layer (caller can't react)? Re-raises that lose the stack?
- **Naming + cohesion:** do public APIs / config keys read right out loud? Does the file's location match its responsibility (e.g. is anything in `pipeline/` actually a connector concern)?
- **Test coverage gap:** load-bearing path with no test? Test exists but mocks the thing it should be hitting (cf. integration-tests-must-not-mock-the-DB feedback)?

### Lens D — AI slop (the persnickety reviewer)

- **Dead code:** uncalled functions, unread config keys, abstractions used in exactly one place, drift comments describing code that no longer exists.
- **Defensive bloat:** try/except guarding errors that can't happen, validation of values created by our own code 3 lines up, fallbacks for branches that never fire, "just-in-case" `Optional[...]` parameters that are always passed.
- **Premature abstraction:** `Manager`/`Helper`/`Handler` classes wrapping a single function, factories with one product, ABCs with one concrete impl, hooks/registries with one caller.
- **Redundant prose:** docstrings restating the function name, comments describing WHAT (not WHY), TODOs for tasks already done, references to deleted files/symbols (e.g. CLAUDE.md still cites `pipeline/mcp_client.disabled_server_for_tool` post-14.19.11).

---

## How to log findings

Per Tier-1 component, append a section here with the structure:

```
### <component>

- A1 (security): finding @ file:line — disposition (fix / wontfix / note)
- B3 (edge case): ...
- C2 (PR review): ...
- D1 (slop): ...
```

Where `A/B/C/D` is the lens and the digit is the bullet within that lens. Keeps findings cross-referencable to the checklist above.

---

## Findings log

### `pipeline/providers/base.py` (S200)

- D1 (slop): `ContextOverflowError` class — defined + re-exported, **zero raisers, zero catchers** post-S199 `llm.py` purge. Removed.
- D1 (slop): `complete_stream()` abstract method (distinct from live `complete_streaming()`) — never called, never implemented in `claude_cli.py`. Removed.
- C2 (PR review): `complete()` docstring claimed "Raises ContextOverflowError" — would have lied after the deletion. Removed.
- D4 (slop / redundant prose): comment above `_PARAGRAPH_BOUNDARY_RE` said "Tightened to require at least one is followed by a non-whitespace char on the next break, but the simple `\\n{2,}` split is what models actually emit." Describes a tightening that was never applied — leftover thought-process. Trimmed.
- **Out of scope for this file but flagged:** `CLAUDE.md` describes the `llm.provider` config field as `openai | anthropic`, but `build_provider()` only ever returns `ClaudeCLIProvider` (no OpenAI/Anthropic backend in the tree). Provider key in user configs is currently inert. *(disposition: docs cleanup, queue for Tier-2 `config.py` cell or a CLAUDE.md sweep.)*

### `pipeline/meeting_record.py` (S200)

- B1 (edge case): **header + session_start written as two separate file opens** with two independent try/except blocks. Failure mode: header write succeeds, session_start fails → file ends up with meta but no session marker → next `tail()` falls back to whole-file replay. Asymmetric. ✅ Fixed: merged into one open, atomic on success/failure.
- D1 (slop) + B1 (edge case): **defensive `try/except OSError` on every file write + `_memory.append()` post-failure looking like a fallback but `tail()` never reads it once `path` is set.** Walked through realistic failure causes (disk full, broken perms, RO remount, FileVault, sandboxing) — none happen in normal operation on a user's Mac, and when they do operator failing is the user's least-bad problem. Per CLAUDE.md "don't add error handling for scenarios that can't happen, trust framework guarantees," ✅ deleted: WARN-and-continue try/except around all writes, plus the orphan `_memory.append` (now guarded with `if self.path is None` so memory only carries state in the slugless in-memory mode). On a real disk-write failure now: `OSError` bubbles up to the caller's top-level handler — clear traceback over silent confusion.
- D2 (slop): `if self.path.exists():` guard before chmod at line 105 was dead in the success path. ✅ Removed (chmod now runs unconditionally; its own try/except still catches the legitimate "network FS doesn't support mode bits" case, which is a real failure mode unlike the file write).
- **Bonus finding from the cleanup:** two tests in `test_transcript_dedupe.py` were silently passing only because of the `_memory` fallback — they read `rec.tail(10)` AFTER `tempfile.TemporaryDirectory()` cleanup, so the file was gone and `tail()` returned the in-memory mirror. Tests rewritten to read inside the `with` block. Net: more honest test coverage.
- C2 (PR review): **magic strings for `kind` values** (`"chat"`, `"caption"`, `"meta"`, `"session_start"`) used across this file + `llm.py` + `transcript.py` + `transcript_server.py` + `bridges/claude.py`. No central enum. Risk: typo on the comparison at `tail()` line 160 (`get("kind") == "session_start"`) silently breaks session scoping — every prior session leaks into the LLM prompt with no error. *(disposition: 🟢 nice-to-have, multi-file scope, defer.)*

### `pipeline/confirmation.py` (S200)

- D1 (slop): `is_yes_always()` + supporting `_AFFIRM_ALWAYS_RE` had zero callers in src/ or tests/. Built for a "codex elicitation handler" that never shipped. ✅ Removed.
- D4 (redundant prose): docstring talked about "track B (openai/anthropic providers)," "chat_runner._handle_confirmation," and the "tracks A and B" architectural split — none of which exist post-claude_cli pivot. ✅ Rewrote: one caller, one purpose, today's reality.
- 🟢 (nit, not fixed): `re.I` flag on the affirm/negation regexes plus `lower = text.lower()` is belt-and-suspenders — neither alone would be wrong. The `lower()` is needed for the substring checks (`"don't" in lower`), so the redundancy is just on the regex side. Leaving alone.

### `pipeline/transcript.py` (S200)

- B1 (edge case): **race on `_last_window_per_speaker` dict.** Three threads (silence loop, browser caption thread, main-thread `stop()`) all call `_emit`. The read at `prior = self._last_window_per_speaker.get(speaker, "")` followed by the write `self._last_window_per_speaker[speaker] = full_window` happened outside the existing `_lock`. Two finalizes in the same 100ms window (silence timer fires for speaker A while speaker change to B arrives) could read-then-write concurrently. Realistic under heavy meeting chatter. ✅ Fixed: collapsed read+write into one critical section under `_lock`.
- C3 / D3 (PR review + slop): **`_emit` swallowed all exceptions via `try/except Exception`.** Inconsistent with cell 5's trust-the-OS principle. The fault isolation we actually need is only at the silence-loop call site (raised exception there silently kills a daemon thread, disabling silence detection). The other two call sites (browser thread `on_caption_update`, main thread `stop()`) already have try/except wrappers at the connector layer (`macos_adapter.py:181`, `attach_adapter.py:916`). ✅ Fixed: lifted the try/except into `_silence_loop` only.
- A1 (security): **caption text logged at INFO level.** `log.info(f'caption_finalized ... text="{text}"')` writes every spoken utterance to `/tmp/operator.log`. If the user shares that log file for a bug report (a common diagnostic flow), they're sharing the full meeting transcript. /tmp file mode is best-case 0o600 from umask 0o077, but a user pasting the file contents into a github issue is the real risk. *(disposition: 🟡 part of a broader logging-policy concern; defer to a cross-file logging audit pass.)*
- 🟢 (nit): `full_window = text` variable name is mildly misleading — it's the just-stripped current text, not the historical window. The cache key intent is clearer at the assignment site. Leaving alone.

### `pipeline/audio.py` (S200)

- D1 (slop): `BYTES_PER_SAMPLE = 4` constant defined but never referenced anywhere — not in this file, not in any caller, not in tests. ✅ Removed.
- D2 (slop): `text.strip().lower()` at line 139 — `text` is already stripped by `transcribe()` at line 173. ✅ Removed redundant `.strip()`.
- A1 (security): `log.info(f'AudioProcessor: whisper_done "{text}"')` at line 136 — every transcribed utterance from the user's mic AND from remote participants goes into `/tmp/operator.log` as a quoted string. Same finding as transcript.py; rolled into the cross-file logging-policy review.
- 🟢 (nits, not fixed):
  - `WHISPER_HALLUCINATIONS` could be `frozenset` — same lookup perf, just stylistic.
  - `_is_repetition_hallucination` is `@staticmethod` instead of a module-level function — slight ceremony, fine.
  - `transcribe` could be `_transcribe` (only called internally + by test) — minor naming.
  - `utterance_audio +=` on bytes is quadratic (each append copies the whole buffer); during a 10s MAX_DURATION utterance this is ~6.4MB of redundant copies. `bytearray` would fix it. Tolerable; not a real bottleneck at meeting timescales.

### `pipeline/providers/claude_cli.py` (deferred from earlier cells)

- **B1 (edge case): no heartbeat-based wedge detection.** From `chat_runner.py` audit. The 600s `TURN_TIMEOUT_SECONDS` is the only ceiling on a hung subprocess — claude going silent for 9 minutes 59s with no events on `_out_q` is indistinguishable from working hard. Proposal: track `last_event_ts`, if silent > ~60s with no in-flight tool call → raise. Keep 600s as backstop. ~30 LOC. *(disposition pending — review during this cell.)*
- **A2/D1 (security + slop): characterize claude_cli's overflow signaling.** From `llm.py` audit. `ContextOverflowError` is defined and caught in `llm.py` but **never raised by claude_cli** (grep confirms zero `raise.*ContextOverflowError` in `claude_cli.py`). Claude Code internally manages context (auto-compaction). Need to: (a) confirm what stream-json events claude emits when its session approaches/hits the window, (b) decide whether to raise `ContextOverflowError` in those cases or surface differently, (c) once known, the entire `_tail_messages` + `_build_messages` + `_max_messages` + `except ContextOverflowError` chain in `llm.py` is candidate dead code (claude_cli only reads `messages[-1]` per turn — the rest of the list is constructed and thrown away). *(disposition pending — review during this cell.)*

### `pipeline/llm.py` follow-up cleanup (gated on claude_cli cell)

- **D1 (slop): vestigial history-replay machinery.** Whole `_tail_messages` (37 LOC) + `_build_messages` (7 LOC) + `_max_messages` config plumbing + halving logic + `record=True/False` branch in `ask` are leftover from the OpenAI/Anthropic-direct architecture where every call had to replay full history. claude_cli ignores all of it (only `messages[-1]` reaches the subprocess). Cannot delete until we know whether to retain anything for a hypothetical future second provider. *(disposition: defer to claude_cli audit conclusion. If claude_cli stays sole provider for v1, delete; if a second provider is in v1 scope, keep.)*

---

## Standalone workflow passes (run separately, not per-component)

These don't fit the matrix — they're cross-cutting workflows. Sequence: matrix first → then 1 → then 2 + 3 → then 4 + 5 right before flipping public.

---

## Pass 1 — Cold-machine install dry-run

Eliminates the #1 launch-day failure mode. `install.sh` end-to-end is currently unverified per S182 carry-over.

- [ ] Read `install.sh` line-by-line together; document what each step does and what the user sees when it fails
- [ ] Identify a fresh macOS environment (VM or second Mac)
- [ ] Run `curl -fsSL <url>/install | sh` exactly as a user would; time it
- [ ] Log every prompt, every error, every "did that work?" moment
- [ ] Verify `uv tool install` resolves against the public repo
- [ ] Verify `playwright install chromium` completes (~170 MB)
- [ ] Verify `~/.operator/.env` is seeded with mode 0600 and never overwrites existing
- [ ] Verify Chrome.app cask nudge fires only on macOS without Chrome installed
- [ ] Verify PATH check + "next: `operator setup`" hint appears
- [ ] Run `operator setup` and `operator dial pm` end-to-end on the fresh machine

## Pass 2 — Embarrassment audit (live-meeting failure paths)

*Role after S199 refresh: example bank for **Lens B (edge case)** when reviewing Tier-1 components like `chat_runner.py`, `attach_adapter.py`, `claude_cli.py`. Each item below is a concrete scenario to trace through the file under review.*

Trace what the bot does when things go wrong in front of a stranger. For each: read the relevant code path, document current behavior, decide accept / fix / note-as-known-issue.

- [ ] Anthropic API down mid-turn — bot says something useful or sits silent?
- [ ] MCP server crashes mid-tool-call — graceful chat message?
- [ ] Chrome killed mid-meeting — clean exit, rejoin, or zombie?
- [ ] User types `@operator` then nothing — does it reply to a blank prompt?
- [ ] Tool result returns 200KB of JSON (Phase 9.11 mitigation — verify still holds)
- [ ] Confirmation prompt while user is mid-sentence — does it lose the rest?
- [ ] Two `@operator` messages 200ms apart — race condition?
- [ ] Bot's own message accidentally re-triggers itself
- [ ] Bot disconnected from network for 30s mid-meeting — recovery behavior
- [ ] User dismisses confirmation, then asks something else — state cleanup correct?

## Pass 3 — Secrets & data egress audit

*Role after S199 refresh: example bank for **Lens A (security)**. Use these as the concrete grep targets when reviewing each Tier-1 component.*

What we write to disk, and what we put into Google Meet chat. Mostly mechanical grep work.

- [ ] Grep every disk-write site (`~/.operator/debug/`, `/tmp/operator.log`, `~/.operator/history/*.jsonl`)
- [ ] Confirm no API keys, no full tool-args containing tokens, no full chat history with user PII land in logs
- [ ] Grep every place we send text to Google Meet chat — could a tool result leak a secret? (e.g. `cat .env` via misbehaving MCP)
- [ ] Confirm `~/.operator/.env` file mode is 0600
- [ ] Confirm `.env` is never copied into debug dumps
- [ ] Confirm `auth_state.json` and `browser_profile/` are never copied into debug dumps
- [ ] Audit `session.save_debug` — what fields land in `~/.operator/debug/`?
- [ ] Verify no secrets get echoed in `say "..."` TTS hooks (if any)

## Pass 4 — Dead code / phantom features pass (SUPERSEDED in S199 refresh)

*The component list below is stale (`mcp_client.py` deleted 14.19.11, `providers/openai.py` + `anthropic.py` never existed, missing all the Tier-1 attach/claude_cli/permission/audio components added since). Use the **component matrix above** with **Lens D (AI slop)**. Pattern bullets here have been folded into Lens D — kept for reference only.*

The classic AI-slop patterns. Run file-by-file on the hot path; produce a list with `file:line`; user decides go/no-go on each.

- Patterns to flag (now in Lens D above):
  - Functions that exist but are never called
  - Try/except catching errors that can't happen, then doing something dumb
  - Config options that are read but have no effect
  - Comments describing code that no longer exists
  - "Helper" abstractions used in exactly one place
  - Defensive validation of values that came from our own code 3 lines up
  - Backwards-compat shims for versions we no longer support

## Pass 5 — `/ultrareview` on the launch branch

Multi-agent cloud review of the current branch (billed). Closest thing to a senior engineer reviewing your PR.

- [ ] Trigger `/ultrareview` on the launch branch (you must run this — I cannot)
- [ ] Triage findings into: must-fix-before-launch / nice-to-have / wontfix
- [ ] Address must-fixes
- [ ] Document wontfixes with rationale

## Pass 6 — `/security-review` on the launch branch

Security-focused review of pending changes. Cheap insurance against a token-leak incident.

- [ ] Trigger `/security-review` on the launch branch
- [ ] Triage findings same as Pass 5
- [ ] Address must-fixes

## Pass 7 — Pin dependencies harder

Future slop comes from silent minor-version behavior changes in `playwright`, `anthropic`, `mcp` etc.

- [ ] Audit `pyproject.toml` — every dep pinned exactly or with `>=`?
- [ ] Audit `requirements.txt` — same
- [ ] Lock to exact versions for v0.0.1 launch; document upgrade-intent process
- [ ] Document the bundled MCP server versions (Linear bridge `mcp-remote@0.1.38`, GitHub `github-mcp-server` v0.32.0, etc.)

## Pass 8 — One-page "what to do when it breaks" runbook

For you, not the user. When a user hits a bug at 11pm, present-you will thank past-you.

- [ ] List the 5 most likely failure modes
- [ ] For each: symptom the user reports, file to look in, command to gather diagnostics
- [ ] Save at `docs/runbook.md` (or similar)

---

## Items to revisit / wontfix log

*Add items here that we consciously decide not to address before launch, with rationale.*

-
