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
- [x] `pipeline/providers/claude_cli.py` (1158 → 1163 LOC) — DONE in commit `bbd25fd` (S201). Two deferred findings turned out to be already resolved by other cells (heartbeat watchdog landed in S199 `b0d0d65`, `ContextOverflowError` deleted in S200 `8e64bd2`). New findings: (1) **latent tempdir leak** — when ClaudeCLIProvider has a meeting record but no permission_handler, `_maybe_write_mcp_config` creates `_mcp_only_tempdir` but `_teardown_permission_bridge` early-returned on `_perm_tempdir is None` before the cleanup ran; doesn't fire in production wiring (ChatRunner always sets a handler) but real for any custom integration. Restructured so mcp-only cleanup runs first. (2) Two `try/except Exception: pass` wrappers around `shutil.rmtree(..., ignore_errors=True)` — the kwarg is documented to never raise, so the wrappers were dead defense. Removed. (3) `_mcp_only_tempdir` lazily created via attribute write; explicit `__init__` initialization for consistency with sibling state attrs.
- [x] `pipeline/providers/base.py` (144 → 122 LOC) — DONE in commit `8e64bd2` (S200): deleted unused `ContextOverflowError` class + `complete_stream()` abstract method (distinct from live `complete_streaming()`, never implemented or called) + stale "Raises ContextOverflowError" docstring line + muddled "Tightened to require..." comment above `_PARAGRAPH_BOUNDARY_RE` that described a tightening never applied. Also dropped `ContextOverflowError` from `providers/__init__.py` exports.
- [x] `pipeline/meeting_record.py` (163 → ~152 LOC) — DONE in commit `de013fe` (S200). Two cuts. (1) Merged the two consecutive `self.path.open("a")` blocks at __init__ time into one open so meta-header + session_start land atomically. (2) After a "in which cases would a disk write actually fail" gut check — disk full, broken perms, RO remount; all rare-and-bigger-problems-than-operator on a user's Mac — deleted the defensive `try/except OSError` scaffolding around all file writes (boot writes + `append()`) and the dead `_memory.append()` shadow store that was only ever read in the no-path test mode but kept getting populated unconditionally. Trust the OS for normal user-file writes; raise loudly if it ever fails. As a bonus: two transcript-dedupe tests were silently relying on the memory-fallback path (reading `tail()` AFTER `tempfile.TemporaryDirectory()` cleanup), now correctly read inside the `with` block. Magic-string `kind` values still flagged for a multi-file cleanup pass.
- [x] `pipeline/confirmation.py` (95 → 51 LOC) — DONE in commit `d3cb63c` (S200): deleted unused `is_yes_always` + `_AFFIRM_ALWAYS_RE` (zero callers; the codex elicitation handler it was built for never landed), and rewrote the docstring to drop stale "track A vs track B" framing + nonexistent references to `chat_runner._handle_confirmation` and "openai/anthropic providers" — today there's exactly one caller (`permission_chat_handler` for claude_cli's PreToolUse hook). Net -44 LOC.
- [x] `connectors/macos_adapter.py` (977 → 953 LOC) — DONE in commit `9e07751` (S201). Hot-loop hygiene: `import re as _re` was running every 500ms tick of the meeting-holding loop (~7,200/hr); hoisted to module top, dropped the alias. Dead-attribute deletion: `self._seen_message_ids = set()` initialized in `__init__` but never read or written — left over from before the JS MutationObserver + data-message-id snapshot path; linux_adapter still uses the name legitimately. Trust-the-OS cleanups (consistent with cells 5+7, S201 session/attach): dropped `try/except OSError` around chmod 0o600 on debug screenshot in `_ensure_chat_open`, and around chmod 0o700 on `BROWSER_PROFILE` (the chmod itself stays — `mode=` doesn't fire on existing dirs — only the wrapper was dead). Narrowed the outer-finally `os.remove(pid_file)` catch from `OSError` to `FileNotFoundError` since that's the only legitimate failure mode; permission errors on a file we own would be unexpected and shouldn't be silently swallowed.
- [x] `connectors/attach_adapter.py` (959 → 957 LOC) — DONE in commit `8dc8b62` (S201, connector cell A — bundled with session.py promotion). Promoted `_is_real_meet_room` + `_MEET_ROOM_RE` from `macos_adapter` into `session.py`; the cross-adapter import was a self-acknowledged "temporary smell" comment in code, and attach_adapter being the second adapter using it was the threshold the comment named for promotion. Other cleanups: dropped chmod try/except around the debug-screenshot fallback in `_ensure_chat_open` (same trust-the-OS pattern); added belt-and-suspenders `os.chmod(SLIP_PROFILE_DIR, 0o700)` after `makedirs` since `mode=` doesn't tighten existing dirs (Google session cookies in slip profile shouldn't be world-readable on shared hosts); wrapped `stderr_sink = open("/tmp/operator.log", "ab")` in a `with` block in `_start_audio_pipeline` so the parent fd closes after `spawn_disclaimed`'s dup2 (was leaking a fd for the connector lifetime); tightened `_cdp_belongs_to_slip` from a plain `SLIP_PROFILE_DIR in stdout` substring match to `f"--user-data-dir={SLIP_PROFILE_DIR}" in stdout` to eliminate false-positive against sibling profile paths like `~/.operator/slip_profile_backup`.
- [x] `connectors/session.py` (277 → 297 LOC) — DONE in commit `d992052` (S201). Substantive: added `ps -p PID -o comm=` verification in `_chrome_kill_and_clear` before the operator-Python-process kill — macOS recycles PIDs aggressively, so a stale `.operator.pid` from a crashed prior run could otherwise get `--force`'d into SIGTERMing an unrelated user process (browser, IDE). Scoped to the `.operator.pid` path only since Chrome's SingletonLock self-cleans on exit (its stale-lock window is far narrower; comment explains the asymmetry). Bonus cleanup: deleted four `try/except OSError: pass` wrappers around `os.chmod` calls in `save_debug` (trust-the-OS — chmod on files we just created as the running user can't fail), and refreshed the module docstring to mention `_chrome_kill_and_clear` (single-instance / `--force`) and `save_debug`, both previously absent.
- [x] `pipeline/transcript.py` (157 → 165 LOC) — DONE in commit `67c2939` (S200): two thread-safety fixes. (1) Closed a race on `_last_window_per_speaker` dict — three threads call `_emit` (silence loop, browser caption thread, main-thread `stop()`), the read-then-write of the per-speaker window was outside the existing lock. Now reads prior + writes new window in one critical section. (2) Moved the catch-all `try/except Exception` from `_emit` into `_silence_loop` only — the silence-loop call site is the only one where a raised exception silently kills a daemon thread (browser + stop call sites already have their own logging guards at the connector layer). Net consistent with cell 5's trust-the-OS principle. Caption-text-in-INFO-log finding flagged for cross-file logging-policy review.
- [x] `pipeline/audio.py` (173 → 171 LOC) — DONE in commit `02ec650` (S200): deleted unused `BYTES_PER_SAMPLE` constant (zero references in src/ or tests/) and removed redundant `.strip()` on whisper output (`transcribe()` already returns stripped text). Caption-text-in-INFO-log finding recurs at line 136 — same as transcript.py, both rolled into the cross-file logging-policy follow-up. Other small nits (frozenset for hallucinations, staticmethod for `_is_repetition_hallucination`, `bytearray` over `+=` accumulation, `_transcribe` naming) consciously left as stylistic.

### Tier 2 — supporting infrastructure

- [x] `__main__.py` (709 → 696 LOC) — DONE in commit `bbae03f` (S202). Two surgical removals: (1) six dead `logging.getLogger("openai"/"anthropic").setLevel(...)` lines across `_run_slip`/`_run_macos`/`_run_linux` — both libraries deleted in 14.19.7's provider purge, zero imports anywhere in src/. (2) Redundant inner `if name != "claude"` guard at top of `_run_slip` — main() dispatcher already filters at argv parse, function is module-private with one caller, the inner check never fires. Out of scope: three near-identical 7-line logging-setup blocks could fold into a helper (cross-function refactor, low payoff today); `runner._stop_event.is_set()` private-attr access at lines 619+698 (mild PEP8 — ChatRunner audit territory).
- [x] `config.py` (99 → 73 LOC) — DONE in commit `36f37c7` (S202): deleted four definitely-dead constants from the pre-14.19.7 era — `TOOL_TIMEOUT_SECONDS` + `DEFAULT_TOOL_TIMEOUTS` (operator no longer runs the tool loop), `TOOL_RESULT_MAX_CHARS` (Phase 9.11 truncation guard, no callers), `LLM_STUCK_THRESHOLD_SECONDS` (streaming watchdog for deleted OpenAI/Anthropic providers; claude_cli has its own heartbeat). Trimmed obsolete "Tool-call timeout precedence" docstring prologue. Deferred to follow-up: `GOOGLE_ACCOUNT_FILE` + `ENV_FILE` are defined-but-unused documentation anchors (cross-file decision needed); CLAUDE.md "Configuration" section is heavily out-of-date post-14.19.7 (whole-section sweep needed, not the one-line `llm.provider` fix originally flagged in S200).
- [x] `pipeline/permission_chat_handler.py` (420 → 211 LOC) — DONE in commit `0c2ad02` (S202). Two dead-code regions purged: (1) `_disabled_mcp_for_cli_tool()` + its call site (54 LOC) — guarded against `config.DISABLED_MCP_SERVERS` which doesn't exist post-14.19.7; defensive getattr always resolved to `{}` so the guard never fired. (2) Whole confirmation-prompt formatter chain (`_format_confirmation`, `_format_terse`, `_format_verbose`, `_show_imperative`, `_human_size`, `_IMPERATIVE_*`, `ARG_RENDER_*`) ~150 LOC + two tests stubbing `config.VOICE` — per the 14.19.8 comment already in `_round_trip`, the LLM authors the prompt in pre-tool narration, this handler no longer renders templated cards; zero src/ callers anywhere. Also hoisted `is_yes` import to top (PEP 8), dropped stale `noqa: F401`, fixed module + class docstrings (stale `config.PERMISSIONS_*` and `runner._send` refs).
- [x] `pipeline/permission_bridge.py` (123 LOC) — CLEAN in S202 (no commit). Defensive-shim contract intentionally catches every failure mode and converts it to a JSON deny so claude's hook chain stays predictable; broad `except Exception` is the spec, not slop, and trust-the-OS doesn't apply to a protocol shim. Pipe perms verified end-to-end via `claude_cli.py:509-510` (`os.mkfifo(.., 0o600)`). No dead code, no premature abstractions.
- [x] `connectors/captions_js.py` (214 LOC) — CLEAN in S202 (no commit). Sole src/ caller is `macos_adapter.py` (slip captions go through the audio-helper Whisper pipeline, not DOM scraping); test coverage at `test_caption_late_bind.py`. Same XSS-safe text-only extraction pattern as chat_dom_js. Broad `except Exception:` at Playwright-interaction boundaries is appropriate. Caption-text INFO logging recurs at lines 202+210 — system-phrase / diagnostic content not user speech, lower PII risk than transcript.py/audio.py but rolls into the same cross-file logging-policy review. `nextNodeId++` / `state.id` dead-state observation noted but not touched (module docstring's explicit "Keep the JS unchanged unless Meet's DOM shifts" is the right cultural sign for a tuned-against-real-traces observer).
- [x] `connectors/chat_dom_js.py` (157 LOC) — CLEAN in S202 (no commit). Five JS strings injected via `page.evaluate()` from both adapters; all five exported and imported at expected call sites in `macos_adapter.py` + `attach_adapter.py`. No XSS surface (text-only DOM reads, no `eval`/`innerHTML`, regex correctly double-escaped). All JS functions have appropriate fallbacks (idempotent observer install, undefined-safe drain, multi-tier participant-name extraction). Future-Meet-DOM-resilience: 60-char `textContent` filter could drop long "Name + Org Title" displays if Meet redesign breaks the primary `data-self-name`/`aria-label` paths — observation only, not a fix today.
- [x] `connectors/base.py` (58 → 72 LOC) — DONE in commit `4e1082d` (S202). One readability fix: added a class-level docstring with a fingerpost to the three implementers (MacOSAdapter / AttachAdapter / LinuxAdapter) and the two-tier method split (required lifecycle/chat vs safe-default optional getters). Skipped `abc.ABC` migration — current NotImplementedError-at-first-call pattern is fine because nothing instantiates the base directly.
- [x] `bridges/claude.py` (81 → 25 LOC) — DONE in commit `446dfde` (S202): deleted dead `spawn_argv()` + `transcript_mcp_spec()` (both predicted to replace inline impls in `claude_cli.py` post-14.19.7; phase shipped without that refactor and the stubs never got callers); deleted unused `REPLY_PREFIX_DIAL` + `REPLY_PREFIX_DEPLOY` (only `_SLIP` is wired); trimmed obsolete "codex/gemini bridges Phase 14.20+" docstring sentence (14.20 is audio, not bridges). Net -56 LOC.
- [x] `mcp_servers/transcript_server.py` (425 LOC) — DONE in commit `d6d4044` (S202). Two doc fixes (no behavior change): (1) `list_speakers` docstring claimed speaker names are "case-sensitive substrings" but the actual filter lowercases both sides — flipped to "case-insensitive" to match. (2) Module docstring's marker-file rationale referenced the deleted codex agent; refreshed to point at the real current case (MCP registrations like `claude mcp add` that don't get per-meeting env interpolation). Code otherwise clean — comprehensive test coverage at `test_transcript_mcp.py`, `_now` monkeypatch hook genuinely used.

### Tier 3 — setup / cold path / platform-secondary

- [x] `pipeline/install_preflight.py` (175 LOC) — REVIEW-only in S203 (no commit; product call open). Module is a phantom feature: `run_install_preflight()` is the docstring-promised entry for "the top of `operator setup`" but no `operator setup` subcommand exists in `__main__.py`. The `chromium_installed` helper is genuinely live (called by `doctor.py`) so it stays regardless. Decision needed: (a) add `operator setup` subcommand wired to the orchestrator, (b) auto-invoke at top of `dial`/`slip`/`login` (silent when ready), (c) document `curl|sh` as the only path + delete orphans, (d) keep as scaffolding. Logged in revisit log below.
- [x] `pipeline/doctor.py` (282 LOC) — DONE in commit `1bf4527` (S203). Two doc-staleness fixes (no behavior change): (1) `_check_chromium()` had a local `import sys` already imported at module top — redundant. (2) Module docstring claimed "Phase 14.20 will add a Screen & System Audio Recording check when slip caption capture ships" — those TCC checks (Screen Recording + Microphone) are already live at lines 182-238 (landed in 14.20.4). Refreshed to today's reality. Also fixed `CheckResult` field list in the docstring — was `(name, status, fix)` but the dataclass has four fields (`name, ok, detail, fix`). Subprocess calls (`git --version`, `helper --probe`) verified safe (trusted paths, list args, no shell, no injection); `_check_microphone`'s "see Screen Recording check" detail with empty fix correctly suppresses duplicate fix-line printing in `run_doctor`.
- [x] `pipeline/readiness.py` (360 LOC) — DONE in commit `6e307bb` (S203, post-S202 follow-up). Removed the duplicate `load_dotenv` call. Pre-S202 readiness inlined `load_dotenv(...)` because the wizard constraint prevented `from _1_800_operator import config`; post-S202 (commit `da6dcb8`) the module imports config directly and config's own module-load already runs `load_dotenv` — by the time readiness's line ran, env was already populated. Dropped the call, the `from dotenv import load_dotenv` import, the now-unused `from pathlib import Path` import, and the override-explanation comment. Kept `from _1_800_operator import config` with `noqa: F401` so the import chain that triggers config's own `load_dotenv` is explicit. **Phantom-feature orphan flagged but NOT removed** (REVIEW-only, same product call as `install_preflight`): `preflight_mcp_readiness()` + `report_mcp_readiness()` have zero src/ callers outside the orphan-internal chain; preflight docstring describes "Runs inside `_run_bot()` after `OPERATOR_BOT` is set..." but `_run_bot` doesn't wire it in. Latent landmine: `from _1_800_operator.pipeline.auth import run_auth` at line 245 — `pipeline/auth.py` doesn't exist; tests pass a mock so the import never fires. References to `operator auth <name>` at lines 167+288 also point at a non-existent CLI subcommand. Logged in revisit log below.
- [x] `pipeline/google_signin.py` (334 LOC) — DONE in commit `9d4fe40` (S203). Three cleanups: (1) Dropped two inner `try/except OSError: log.warning` wrappers around `os.chmod(0o600)` on `auth_state.json` and `google_account.json` — same trust-the-OS dead-defense pattern S201 cleaned across session/attach/macos/linux adapters. Both files are written under `os.umask(0o077)` set by `__main__.main()`, so chmod is belt-and-suspenders for existing-file overwrites; the wrapper was wrapping an op that doesn't fail on files we just created. Outer try/except around `storage_state` / `write_text` stays — Playwright + disk write are real failure modes. (2) Hoisted `import os as _os` out of `_write_artifacts` to the module-top imports as plain `import os`. (3) Refreshed the session-178 / T1.11 docstring comment in `_launch_signin_flow` — said "wizard sign-in uses real Google Chrome explicitly" but the wizard's `run_signin_step` entry was deleted in `da6dcb8` (S202 follow-up); `_launch_signin_flow` is now invoked only via `operator login claude`. Tightened wording. **Out of scope but flagged**: `chrome_path` literal at line 202 duplicates `chrome_preflight.CHROME_PATH` — could import the constant. Different concern from this cell's three cleanups.
- [x] `pipeline/chrome_preflight.py` (62 LOC) — DONE in commit `b458e58` (S203). Refreshed module docstring to match session-163 reality (no code change). Was claiming "Both the wizard sign-in step (`pipeline/google_signin.py`) and the macOS adapter (`connectors/macos_adapter.py`) hard-code the system Chrome binary because Chrome profiles aren't compatible across binaries (Chrome-for-Testing vs real Chrome — session 159 hard-won knowledge)." Both halves out of date: `macos_adapter` switched to Playwright's bundled Chromium in S163 (the bundle-ID singleton bug fix), and that same session disproved the profile-incompat reasoning. Today's reality: real Chrome is required because (1) `google_signin.py` hardcodes it for Google's signin flow and (2) `attach_adapter.py` CDP-attaches to it for slip mode; the `require_chrome_or_exit` call from `MacOSAdapter.join()` is defense-in-depth against future code paths bypassing `__main__`.
- [x] `pipeline/claude_code_import.py` (229 → 68 LOC) — DONE in commit `8f1f167` (S203). Module-wide slim-down. `read_user_mcp_config()` + `user_config_path()` + `_USER_CONFIG_CANDIDATES` were dead in production — module + test docstrings claimed they were "Used by `claude_cli` to translate operator's `disabledMcpjsonServers` overlay back to JSON-keyed names" but `claude_cli.py:_maybe_write_mcp_config` writes a fresh per-spawn `mcp.json` with just the transcript server, never reads `~/.claude.json`. The 14.19.7-F slim-down kept these as "what claude_cli still calls"; a later change removed even those last callers without trimming. Today only `claude_code_installed_and_logged_in()` is alive (used by `_run_bot` + `_run_slip` to fail loud before any browser spins up). Deleted the orphans + `tests/test_claude_code_import.py` (4 tests targeting the removed function; the surviving wrapper is exercised in `test_1574_readiness.py`). Module name is now historical — flagged in the docstring. Net −161 LOC across src + tests. **Out of scope but flagged**: CLAUDE.md describes a "first-run auto-import the user's Claude Code MCP servers" flow with a `_claude_import_done` marker — no code in src/ implements it. Belongs in the broader CLAUDE.md staleness sweep.
- [x] `pipeline/oauth_cache.py` (45 LOC) — DONE in commit `62a9da8` (S203). Real bug fix + slop cleanup. **(B1, edge case)** `mcp_remote_cache_dir()` was using `sorted(...)[-1]` to pick the latest `~/.mcp-auth/mcp-remote-X.Y.Z/` directory. As strings, `mcp-remote-0.1.10 < mcp-remote-0.1.9` (digit-boundary inversion) — a user with both an older `0.1.4` cache and the current `0.1.38` cache would have `0.1.4` selected. Real false-positive shape: `oauth_cache_exists` reports True off the stale dir's `tokens.json`, the active mcp-remote@0.1.38 reads from its own dir, finds nothing, hangs at the OAuth popup at meeting join. Switched to mtime-based pick — currently-installed mcp-remote writes to its own dir at runtime, so its mtime is freshest. Robust to any version naming (semver, prerelease tags), unlike both the broken lex sort and a tuple-parse alternative. **(D1, slop)** Module + function docstrings referenced `mcp_client.py` as a current caller — `mcp_client.py` was deleted in 14.19.11; today only `readiness.py` is a caller. Updated both.
- [x] `pipeline/_disclaimed_spawn.py` (269 LOC) — DONE in commit `e487380` (S203). Three findings. **(B1, edge case)** Pipe-create partial-failure fd leak: if the second `os.pipe()` call (stdout_r/stdout_w) hit the fd limit and raised `OSError`, the first pipe (stdin_r/stdin_w) was never closed — the calling process leaks two fds per failure. Real failure rate low (only fires near per-process fd limit), fix is a trivial try/except that closes the first pipe on the rare second-pipe failure and re-raises. **(D1, slop)** Unused imports `import io` and `import errno` (latter only mentioned in a docstring). **(C2/D4, PR review + redundant prose)** `DisclaimedProcess` docstring claimed the wrapper "Implements only the subset AttachAdapter touches: .pid, .stdin, .stdout, .poll(), .wait(timeout=), .terminate(), .kill(), context-mgr exit." No `__enter__`/`__exit__` defined and the only caller (`attach_adapter`) doesn't use `with` syntax. Dropped the false claim. **Things checked and ruled out**: ctypes `posix_spawn` glue + child fd plumbing correct; spawn-failure cleanup paths correct (finally block destroys attr/file_actions handles, closes parent-side fds when `spawn_failed` is True); PID-reuse race not real (child can't be reused while held as a zombie pre-reap; `ProcessLookupError` catch handles the only legitimate race); `responsibility_spawnattrs_setdisclaim` is documented stable since macOS 10.14 (used by BackgroundMusic / Yabai), acceptable for v1.
- [x] `pipeline/ui.py` (68 LOC) — DONE in commit `5fe1198` (S203). One Lens D / dead-code finding: `chat_in()` and `chat_out()` defined as inbound/outbound chat narrators but zero callers anywhere in src/ or tests/. Runtime uses `ui.ok(f"Replied — {elapsed:.1f}s")` for outbound progress and doesn't echo inbound (Meet's chat panel already shows it to the user). Looks like a partially-implemented "narrate the chat" mode that never got wired up. Removed both functions plus the now-unused `cyan` and `blue` color entries in `_COLORS`. Net −13 LOC. NO_COLOR/isatty enable check, the four live narrators (`say`/`ok`/`warn`/`err`), and the colorize helper are all actively used and correct.
- [x] `connectors/linux_adapter.py` (701 LOC) — DONE in commit `1d6d44e` (S203). Parity-with-S201 cleanups against the secondary-platform adapter. **(C1, PR review)** `import re as _re` was running every 500ms iteration of the meeting-holding while loop (~7,200/hr for a 1-hour meeting) — same hot-loop import S201 cleaned in `macos_adapter`. Hoisted `re` to module top, dropped the `_re` alias, updated two call sites. **(D1, slop)** Chmod scaffolding in `_ensure_chat_open` (debug screenshot) and `_browser_session` (user_data_dir) — same trust-the-OS pattern S201 cleaned across `macos_adapter` / `attach_adapter` / `session.py`. Chmod stays load-bearing (`mode=` doesn't fire on existing dirs); only the wrappers go. **Things checked and ruled out**: `_seen_message_ids` is genuinely live in this adapter (used in `_do_read_chat` dedup), unlike the dead one S201 cleaned from macos_adapter — distinct dedup paths. Session-recovery ladder + admission poll + network-alert + 30s grace + health check are consistent with macos_adapter's parallel logic. `_browser_session` is ~350 LOC with mixed concerns (refactor candidate, no bug; out of scope for an audit cell).
- [x] `install.sh` + `pyproject.toml` + `requirements.txt` — DONE in commit `9362c25` (S203). Three pinned Python deps with zero imports anywhere — leftover from earlier phases: `readchar==4.2.2` (was for the wizard's arrow-key picker), `ruamel.yaml>=0.18.0` (was for the wizard's round-trip YAML edits), `soundfile==0.13.1` (audio I/O the pipeline ended up not needing — `numpy` + `mlx_whisper` handle frames directly). Removed all three from both `pyproject.toml` and `requirements.txt`. Refreshed stale wizard comments on `rich` (annotated as for the wizard's TUI; today rich is used by `google_signin`'s prompts) and `pyyaml` (annotated alongside ruamel.yaml round-trip; today pyyaml is test-only, harmless at runtime). **Kept** `openai==2.29.0` and `anthropic==0.94.0` despite zero src/ imports — same product call gates them as the vestigial `_tail_messages` / `_build_messages` in `llm.py`: if a second provider is in v1 scope they stay, if dropped they go in the same commit as the llm.py cleanup. **`install.sh` CLEAN** — `set -euo pipefail`, idempotent, sendoff hints (`operator doctor`, `slip claude`, `dial claude`) match `__main__.py` subcommands. The `agent-context.md` memory describing it as ending with `next: operator setup` was stale — actual script does not.
- [x] `src/_1_800_operator/agents/{claude,codex}/` preset configs — DONE in commit `51faf04` (S203). Three Lens-D dead-scaffolding findings. **Empty `agents/` tree**: `src/_1_800_operator/agents/{claude,codex}/` were empty directories holding only stale `__pycache__/` artifacts from a removed prior version's `framework.py` / `__init__.py`. Per commit `e7ccadf` (14.19.7-C "delete bundled agent presets, bundled skills, ensure-helpers"), the bundled-preset scheme was intentionally removed in the chat-first pivot; today's runtime uses user-scoped `~/.operator/agents/claude/config.yaml` exclusively and no Python code imports anything under `src/_1_800_operator/agents/`. Zero git-tracked files in the tree. Removed the entire empty hierarchy via `rm -rf`. **Vacuous test file**: `tests/test_bundled_mcps_bootable_static.py` (242 LOC) iterated `BUNDLED_AGENTS_DIR.glob("*/config.yaml")` (0 hits today) and `CUSTOM_TEMPLATE` at `src/_1_800_operator/custom_template.yaml` (file doesn't exist). All three test functions passed vacuously. Deleted. **Stale pyproject.toml comment**: `[tool.hatch.build.targets.wheel]` block carried a 4-line comment about hatchling auto-bundling `agents/**/*.{yaml,md,txt,env.example}` and verifying that the wheel ships "pm, engineer, designer" — three agents that don't exist either. Trimmed to just the live `packages` line. Net −242 LOC test + two empty dirs + 4 lines of stale build-config commentary.
- [x] audio-helper Swift binary — DONE in commit `66ab455` (S206) and live-validated end-to-end in S207. Apple Dev account approved and certs/bundle/notarytool prereqs landed (TEAMID DSW7V72HT7); `scripts/build_signed_helper.sh` does the swiftc → .app bundle → Developer-ID + hardened-runtime codesign → notarytool submit → stapler staple chain. Headline runtime fix: macOS 15 SCStream silently drops audio callbacks unless the stream is held in a strong reference that survives the configuration closure (voice-preserved worked on Sonoma because it internally retained SCStream; Sequoia does not). Fix is one line — pin the stream to a module-scope `var sysStream: SCStream?` in `operator-audio-capture.swift`. Plus three supporting changes: `cfg.sampleRate=48000, channelCount=2, queueDepth=5` to match Apple's documented config; AVAudioConverter on the system path downsamples 48k stereo Float32 back to 16k mono so `AudioProcessor` stays format-agnostic; `Info.plist` sets `LSUIElement=false` (required for SCK foreground-app prompt path on macOS 15) under bundle id `com.1-800-operator.audio-capture`. `install.sh` step 8 refactored: production path copies a pre-built `.app` from the wheel into `~/.operator/bin/` (Granola-style, no user compile); dev fallback still ad-hoc-signs a raw swiftc binary with an explicit warning that ad-hoc can't do system audio. Live-tested in S207 against a real Meet — phone in another room, slip claude on the laptop, headphones on, mic muted: helper captured the phone's audio out via SCStream, mlx-whisper transcribed it into the meeting JSONL, full pipeline alive. **Mic-leg E2E carry-forward**: the `[M]` stream itself was demonstrated working in every prior failure run, but the user-back-at-desk → unmute → speak → `@claude what did the other person say?` round-trip remains genuinely untested as of S208. Gates `/ultrareview` + `/security-review`.

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

### `pipeline/providers/claude_cli.py` (S201 — deferred findings resolved + new cleanups)

- **B1 (edge case): no heartbeat-based wedge detection.** ✅ **Already resolved** by S199 commit `b0d0d65` ("wedge watchdog + tempdir leak fix + dead overflow purge"). `HEARTBEAT_SILENCE_SECONDS = 60` at line 60, `_check_heartbeat` at lines 750-764, called from both `_send_and_collect` (line 890) and `_send_and_collect_streaming` (line 1041). The audit doc just hadn't been updated.
- **A2/D1 (security + slop): characterize claude_cli's overflow signaling.** ✅ **Moot post-S200**. `ContextOverflowError` was deleted entirely in `providers/base.py` cleanup (commit `8e64bd2`); there's no exception left for claude_cli to raise. Inner-claude does its own context management (auto-compaction), so overflow is invisible to operator at this layer. Confirms the cross-file question for `llm.py`: the dead `_tail_messages` chain there is provably unused.
- **B2 (edge case): latent mcp tempdir leak in no-permission-handler path.** ✅ Fixed in S201 `bbd25fd`. When ClaudeCLIProvider has a meeting record but no permission_handler, `_maybe_write_mcp_config` creates a standalone `_mcp_only_tempdir`, but `_teardown_permission_bridge` early-returned on `_perm_tempdir is None` before the cleanup ran. Today's wiring always sets a permission_handler (ChatRunner does it), so the path doesn't fire in production — but it would leak `/tmp/operator-claude-mcp-XXXX/` per meeting in any future code path that wires the provider without one. Restructured so mcp-only cleanup runs first.
- **D1 (slop): dead `try/except Exception` around `shutil.rmtree(..., ignore_errors=True)`.** ✅ Fixed in S201. `ignore_errors=True` is documented to never raise; the outer wrapper was double-defense.
- **D2 (slop): `_mcp_only_tempdir` lazily created via attribute write.** ✅ Fixed in S201 — explicit `__init__` initialization for consistency with sibling state attrs.
- **Things checked and ruled out:** ~250-LOC duplication between `_send_and_collect` and `_send_and_collect_streaming` is a refactor candidate (could share one event loop with a strategy parameter) but out of scope for an audit cell — dispatch helpers (`_dispatch_assistant_blocks`, `_user_event_carries_tool_result`, `_check_heartbeat`) are already factored out. `_terminate_subprocess` broad excepts on `stdin.close()` and `wait(timeout=5)` are defensible for shutdown paths. The synchronous handler call inside `_permission_pump` is intentional (one PreToolUse at a time per turn). Bare `except OSError: pass` around the sentinel write at line 615-617 is correct — NONBLOCK write to a pipe with no reader returns ENXIO; that's the expected case when the pump already exited.

### `pipeline/llm.py` follow-up cleanup — RESOLVED in S204 commit `6b20483`

- **D1 (slop): vestigial history-replay machinery.** ✅ **Resolved with a design refinement, not full deletion.** The S204 1-hour-meeting endurance audit surfaced a sharper framing: the user's UI-continuity model says chat-history-in-prompt is load-bearing (people assume the bot saw earlier chat) but caption-history is not (people reference ambient talk explicitly when they want it). Chose option (b) over the binary delete-vs-keep: kept the tail mechanism but scoped chat-only, dropped captions from the prompt entirely (they remain accessible to inner-claude on demand via the bundled transcript MCP server). Also fixed an A4 latency bug from the same audit: `MeetingRecord.tail()` was reading the entire JSONL on every LLM call → replaced with a deque-backed `tail_chat()` (O(n), no I/O). The 40-slot context budget now goes 100% to chat instead of being crowded out by captions in active meetings. `wrap_spoken` / `_neutralize_close` / `SAFETY_RULES` are now dead in src/ (still tested in test_wrap_spoken_sanitizes_speaker); separate cleanup commit. **The `openai` + `anthropic` Python deps stay or go on a separate decision** — they were paired with this question in S203 but the second-provider scope question is independent of how chat history is plumbed.

### `connectors/session.py` (S201)

- **A1 (security): PID-reuse race in `_chrome_kill_and_clear --force` path.** ✅ Fixed: added `ps -p PID -o comm=` verification before SIGTERM. macOS recycles PIDs aggressively, so a stale `.operator.pid` from a crashed prior run could otherwise get `--force`'d into killing an unrelated user process (browser, IDE). Scoped to `.operator.pid` only — Chrome's SingletonLock self-cleans on exit, so its stale-lock window is far narrower; comment explains the asymmetry.
- **D1 (slop): chmod scaffolding in `save_debug`.** ✅ Fixed: deleted four `try/except OSError: pass` wrappers around `os.chmod` calls. Trust-the-OS pattern from S200 cells 5+7 — chmod on files we just created as the running user doesn't fail.
- **C2 (PR review): stale module docstring.** ✅ Fixed: added `_chrome_kill_and_clear` (single-instance / `--force`) and `save_debug` to the docstring; both previously absent.

### `connectors/attach_adapter.py` (S201, connector cell A)

- **C1 (PR review): cross-adapter import smell.** ✅ Fixed: promoted `_is_real_meet_room` + `_MEET_ROOM_RE` from `macos_adapter` into `session.py`. The original code had a self-acknowledged "temporary smell — if a third adapter ever needs this, promote to session.py" comment; attach_adapter being the second user was the threshold the comment named.
- **D1 (slop): chmod scaffolding in `_ensure_chat_open`.** ✅ Fixed: dropped dead `try/except OSError` around `os.chmod` on debug screenshot.
- **A1 (security): SLIP_PROFILE_DIR perms not tightened on existing dirs.** ✅ Fixed: added `os.chmod(SLIP_PROFILE_DIR, 0o700)` after `makedirs` (mirrors `macos_adapter._browser_session`'s belt-and-suspenders pattern). `mode=` only fires at creation; if the dir already existed with looser perms (umask-default 0o755) Google session cookies were silently world-readable on shared hosts.
- **D2 (slop): stderr-sink fd leak in `_start_audio_pipeline`.** ✅ Fixed: wrapped `open("/tmp/operator.log", "ab")` in a `with` block so the parent fd closes after `spawn_disclaimed`'s dup2. Effectively harmless in single-meeting CLI runs (process exit reclaims it) but off-pattern; matters for any long-lived parent process or test harness.
- **B1 (edge case): `_cdp_belongs_to_slip` substring false-positive.** ✅ Fixed: tightened from plain `SLIP_PROFILE_DIR in stdout` to `f"--user-data-dir={SLIP_PROFILE_DIR}" in stdout`. Prevents silent attach-to-wrong-Chrome when a sibling profile (e.g. `slip_profile_backup`) has overlapping path text.

### `connectors/macos_adapter.py` (S201)

- **C1 (PR review): hot-loop local import of `re`.** ✅ Fixed: `import re as _re` was running every 500ms iteration of the meeting-holding while loop (~7,200/hr for a 1-hour meeting). `sys.modules` cache makes each one cheap, so no measurable runtime cost — but it was noise in the hot path. Hoisted `import re` to module top, dropped the `_re` alias which existed only to fit the local-import shape.
- **D1 (slop): dead `_seen_message_ids` attribute.** ✅ Fixed: `self._seen_message_ids = set()` initialized in `__init__` but never read or written anywhere in this file. Left over from before the JS MutationObserver + `data-message-id` snapshot dedup path. `linux_adapter` still uses the name for actual message-ID tracking.
- **D2 (slop): chmod scaffolding in `_ensure_chat_open`.** ✅ Fixed: same dead `try/except OSError` around chmod 0o600 on debug screenshot.
- **D3 (slop): chmod-with-warning try/except on `BROWSER_PROFILE`.** ✅ Fixed: dropped the wrapper around chmod 0o700. The chmod itself is load-bearing (mode= doesn't fire on existing dirs) but the wrapper was dead defense — the WARN-and-continue form is slightly less bald than silent variants but still wrapping an op that doesn't fail.
- **C2 (PR review): broad `OSError` catch on PID file removal.** ✅ Fixed: narrowed to `FileNotFoundError`. The legitimate failure mode is `_write_operator_pid` having never run (early failure → file doesn't exist); permission errors on a file we own would be unexpected and shouldn't be silently swallowed.
- **Things checked and ruled out:** the 200-LOC mixed-concerns holding loop (admit poll + network alert + health check + chat queue drain) is a refactor candidate but no bug found; out of scope for an audit cell. `last_admit_attempt` cooldown (lines 791-862) handles the sticky-pill suppression cases correctly. The post-success-signal exception-handler guard (`if not js.ready.is_set()` at line 951) is correct — once join succeeded, mid-meeting exceptions shouldn't retroactively flip success to failure.

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
- [x] ~~Verify `playwright install chromium` completes (~170 MB)~~ — **obsolete S246**: bundled Chromium dropped in 14.22.5 (slip CDP-attaches to real Chrome.app)
- [ ] Verify `~/.operator/.env` is seeded with mode 0600 and never overwrites existing
- [ ] Verify Chrome.app cask nudge fires only on macOS without Chrome installed
- [x] ~~Verify PATH check + "next: `operator setup`" hint appears~~ — **obsolete S246**: `operator setup` wizard deleted in 14.19.7
- [x] ~~Run `operator setup` and `operator dial pm` end-to-end on the fresh machine~~ — **obsolete S246**: `setup` and `dial` subcommands deleted in 14.19.7 (chat-first pivot)
- [ ] Run `operator doctor` end-to-end on the fresh machine — all checks green, MCP registered, audio-helper TCC granted
- [ ] Run `/operator:slip <meet-url>` end-to-end on the fresh machine — bot joins, `@claude` reaches inner-claude, reply lands in chat

## Pass 2 — Embarrassment audit (live-meeting failure paths)

*Role after S199 refresh: example bank for **Lens B (edge case)** when reviewing Tier-1 components like `chat_runner.py`, `attach_adapter.py`, `claude_cli.py`. Each item below is a concrete scenario to trace through the file under review.*

Trace what the bot does when things go wrong in front of a stranger. For each: read the relevant code path, document current behavior, decide accept / fix / note-as-known-issue.

- [x] ~~Anthropic API down mid-turn — bot says something useful or sits silent?~~ — **obsolete S246**: operator no longer calls Anthropic directly (`feedback_no_direct_llm_api`); inner-claude owns API calls and surfaces its own error path via PTY stdout
- [x] MCP server crashes mid-tool-call — graceful chat message? **Audited S246**: claude code (not operator) sees the dead MCP socket and surfaces it via standard `tool_result` envelope with `is_error`. Claude composes a chat-friendly message ("the X tool is unavailable, let me try another way"); operator just relays. `claude_cli.py:1149-1180` parses tool_results agnostically — no special-case for errors. No operator-side intervention needed. Live confirmation of *what claude says* would be educational but not blocking.
- [x] Chrome killed mid-meeting — clean exit, rejoin, or zombie? **Resolved S246**: tab-close path live-validated (page closed → AttachAdapter exits cleanly, worker takes over sealing in ~333ms). Chrome eviction (0-tabs zombie) validated via wgp wiretap launch (180ms evict + relaunch + clean attach)
- [x] User types `@claude` then nothing (just the trigger, no message) — does it reply to a blank prompt? **Audited S246**: graceful. `chat_runner.py:761` gates forwarding on `if prompt:` — bare `@claude` (or trigger + whitespace / `,` / `:`) strips to empty string, no forward, no sticky window opens. *(refreshed S246: trigger renamed from `@operator`)*
- [x] Tool result returns 200KB of JSON (Phase 9.11 mitigation — verify still holds) **Audited S246**: Phase 9.11 was voice-era LLM-client mitigation, now obsolete (operator no longer the API client). Current state: bundled `operator-meeting-record` MCP has hard 80KB ceiling per result with explicit paging notice (`record_server.py:95 RESULT_BYTE_CEILING = 80000`); other MCPs handled by claude code's own truncation; claude text replies stream paragraph-by-paragraph via `STREAM_PARAGRAPH_MIN_INTERVAL = 0.25s` so no single chat-send carries the full reply; operator's own failure messages capped at `_FAILURE_MESSAGE_MAX = 2000` chars.
- [x] ~~Confirmation prompt while user is mid-sentence — does it lose the rest?~~ — **obsolete S246**: voice-era confirmation flow; chat mode uses permreq (PreToolUse hook with `— OK?` chat message), no audio interleaving
- [x] Two `@claude` messages 200ms apart — race condition? **Audited S246**: no race possible. Polling loop is synchronous, `for msg in messages` iterates strictly sequentially, `_handle_message` blocks the loop on `_llm.ask` (~5s per turn) before next iteration. Two triggers 200ms apart → same tick's `messages` list → processed in strict order with full claude response time between them. S234 debounce addresses the different "follow-up within window" case. *(refreshed S246: trigger renamed)*
- [x] Bot's own message accidentally re-triggers itself **Audited S246**: three-layer defense (`chat_runner.py:666-680`): (a) ID-based `_seen_ids` dedup (primary), (b) sender-based `_is_self_sender` comparing against connector-authoritative `get_self_name()` from S231 fix that survived the "set your name to Claude to mute the bot" attack, (c) text-match `_own_messages` fallback for empty senders. All three apply on every read_chat path. Solid.
- [x] Bot disconnected from network for 30s mid-meeting — recovery behavior **Audited S246**: non-event. Operator is fundamentally local — CDP is local IPC, polling reads chat over local IPC, JSONL is local disk. Network drop affects Chrome↔Google but not operator's loop. `is_connected()` stays True (`attach_adapter.py:1153` reads threading.Event flags, not network state). When network recovers, Meet reconnects and DOM observer resumes seamlessly. *Vestigial finding: `test_915_reconnection.py` has a `test_network_loss_grace_period` testing a "wait 30s after role=alert then exit" algorithm that is NOT wired into production. Designed but never shipped. Test passes in isolation but doesn't reflect production. Keep as design documentation or remove.*
- [x] ~~User dismisses confirmation, then asks something else — state cleanup correct?~~ — **obsolete S246**: voice-era flow; permreq state cleanup covered by S242 H-20 (denied-tool announcement leak fix) + H-27 (permreq `?` clears `_last_reply_had_question`)

## Pass 3 — Secrets & data egress audit

*Role after S199 refresh: example bank for **Lens A (security)**. Use these as the concrete grep targets when reviewing each Tier-1 component.*

What we write to disk, and what we put into Google Meet chat. Mostly mechanical grep work.

- [x] Grep every disk-write site (`~/.operator/debug/`, `/tmp/operator.log`, `~/.operator/history/*.jsonl`) — see S246 findings below
- [x] Confirm no API keys, no full tool-args containing tokens, no full chat history with user PII land in logs — see S246 findings below
- [x] Grep every place we send text to Google Meet chat — could a tool result leak a secret? (e.g. `cat .env` via misbehaving MCP) — see S246 findings below
- [x] Confirm `~/.operator/.env` file mode is 0600 — verified: `install.sh:118` chmod 600, `os.umask(0o077)` at `__main__.py:560` enforces for any new file
- [x] Confirm `.env` is never copied into debug dumps — verified: `save_debug` writes only screenshot + DOM, never reads `.env`
- [x] Confirm `~/.operator/slip_profile/` (slip Chrome user-data-dir with Google session cookies) is never copied into debug dumps — verified: not referenced by `save_debug`; dir at 0700
- [x] Audit `session.save_debug` — what fields land in `~/.operator/debug/`? — see S246 findings below
- [x] ~~Verify no secrets get echoed in `say "..."` TTS hooks (if any)~~ — **obsolete S246**: TTS path was voice-era; no `say`/TTS in chat-first runtime

### S246 secrets-sweep findings

**No must-fix-before-launch issues.** Three local-only-logging notes worth surfacing in user docs.

**✅ API keys / cloud creds — fully isolated:**
- `ANTHROPIC_API_KEY` stripped from inner-claude PTY spawn (`claude_cli.py:530`) and classifier sidecar (`classifier.py:230`). Inner-claude uses subscription auth via `claude login`, not API key.
- Audio helper + TCC probe + doctor spawns use `minimal_helper_env()` (`_disclaimed_spawn.py:131`) — 9-var allowlist (PATH/HOME/USER/LOGNAME/LANG/LC_ALL/LC_CTYPE/TMPDIR/SHELL). Explicit-deny list (in docstring): `ANTHROPIC_API_KEY`, `AWS_*`, `GITHUB_TOKEN`, `OPENAI_API_KEY`, every `*_TOKEN`/`*_SECRET`.

**✅ File modes — all sensitive paths owner-only:**
- `.env`: 0600. install.sh seeds at 0600, never overwrites.
- `~/.operator/history/<slug>.jsonl`: dir 0700, files 0600 (`meeting_record.py:11-12`).
- `~/.operator/slip_profile/`: 0700.
- `~/.operator/debug/`: 0700; screenshots + HTML dumps 0600 (`session.py:93,100`).
- `~/.operator/cdp_origin`: 0600 (file IS the secret that gates CDP access).
- `/tmp/operator.log`: 0600 (per `os.umask(0o077)`).

**ℹ Local-only logging surface — leaks chat content into `/tmp/operator.log`:**
- `attach_adapter.py:1223` — `log.info(f"AttachAdapter: chat sent: {full_message!r}")` logs every outgoing chat body verbatim. Includes claude's replies (which contain tool-result summaries) and operator's permreq questions.
- `llm.py:71` — `log.debug(f"LLM message: {message}")` logs every `@claude` prompt body verbatim. Currently visible because logging is DEBUG-level (`__main__.py:1027`).
- `claude_cli.py:638,652` — on inner-claude crash, `_pty_tail` dumps the last 2KB of PTY output to `operator.log` (could include tool results with sensitive content if claude died mid-stream).
- All file modes 0600, owner-only. **Not a blocker, but document for users:** a support log shared without sanitization could leak meeting chat content + pasted secrets.

**ℹ `save_debug` dumps (`~/.operator/debug/*.{png,html}`):**
- Triggered on failure paths (Chrome attach failed, pre-camera-toggle, etc.).
- Screenshot is `full_page=True` — captures the whole Meet UI including chat panel if open.
- HTML is `page.content()` — full DOM serialization (chat messages included).
- Files chmod 0600, dir 0700 — local-only.
- **Not a blocker, but document:** `~/.operator/debug/` may contain Meet screenshots/DOM; users should review before sharing.

**ℹ Chat-content egress to Google Meet — inner-claude's discretion:**
- Operator forwards claude's responses verbatim via `send_chat`. If a user asks `@claude show me ~/.env` and claude reads + echoes it, that secret lands in meeting chat.
- This is an inner-claude behavior question, not an operator-mechanism issue. Operator has no content filter.
- Mitigation by mode:
  - `/operator:slip` (default) + `/operator:slip-strict`: PreToolUse permreq pops before Read/Bash/Write → user explicitly approves each access.
  - `/operator:slip-yolo`: no permreq, tools run unattended. User opts in; SKILL.md should warn.

**Release-doc additions (recommended, not blocking):**
1. "/tmp/operator.log contains your meeting chat content verbatim — sanitize before sharing for support."
2. "Files in ~/.operator/debug/ may contain Meet screenshots/DOM at failure time."
3. "slip-yolo runs all tools unattended; don't use in meetings where you don't fully trust claude with arbitrary tool access."

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

- ~~**`install_preflight.run_install_preflight` + `readiness.preflight_mcp_readiness` orphans (S203 REVIEW-only).**~~ ✅ **CLOSED in S206 commit `51b69f3` — option (c) "delete orphans".** User picked delete over wire-up: the wizard architecture these served was deliberately removed in 14.19.7; `report_mcp_readiness` operates on a `mcp_servers: dict` config that operator no longer has (inner-claude inherits MCPs from `~/.claude/`); wiring up `operator setup` would un-do the chat-first pivot. Inlined `_probe_claude_code` into `claude_code_import.py` (its only public consumer wraps it 1:1 — collapsed the wrapper layer); moved `chromium_installed` + `_playwright_browsers_root` into `doctor.py` (its only caller). Deleted `pipeline/readiness.py` (348 LOC), `pipeline/oauth_cache.py` (49 LOC), `pipeline/install_preflight.py` (175 LOC) and the three vacuous test files (`test_1574_readiness.py` 398 LOC, `test_install_preflight.py` 161 LOC, `test_15745_preflight.py` 322 LOC). Net −1407 LOC. The latent ImportError landmine at `readiness.py:247` is eliminated by virtue of the file being gone. 17/17 remaining tests pass; `operator doctor` runs clean; `operator slip claude` reaches its arg-validation gate which proves the preflight chain still wires through `claude_code_import.claude_code_installed_and_logged_in` correctly.
- ~~**`openai==2.29.0` + `anthropic==0.94.0` Python deps (S203 deferred).**~~ ✅ **CLOSED in S205 second-pass** — second-provider scope call resolved NO; deps removed from `pyproject.toml` + `requirements.txt`, comment headers refreshed to explain the no-API-keys / `claude -p` only architecture. Saved to memory as `project_second_provider_resolved_no.md`.
- **Cross-file caption-text-in-INFO-log finding (S200/S202).** Three sites flagged across `transcript.py`, `audio.py`, `captions_js.py` — every transcribed utterance lands in `/tmp/operator.log` as a quoted string. /tmp file mode is 0o600 from umask 0o077, but the real risk is a user pasting log contents into a github issue for a bug report. Needs a cross-file logging-policy review, not a per-cell fix.
- **Magic-string `kind` values across `meeting_record.py` + `llm.py` + `transcript.py` + `transcript_server.py` + `bridges/claude.py` (S200).** No central enum. Risk: typo on the `get("kind") == "session_start"` comparison silently breaks session scoping — every prior session leaks into the LLM prompt with no error. Multi-file refactor; deferred.
- **Heartbeat watchdog from S199 still has no tests.** Nice-to-have follow-up; the runtime path is exercised in production but not unit-tested.
