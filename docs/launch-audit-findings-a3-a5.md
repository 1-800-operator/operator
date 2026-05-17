# Launch Audit Findings — Audit 3 + Audit 5

Output from running Audit 3 (hardcoded numbers) and Audit 5 (secrets)
from `docs/launch-audit-plan.md` across all 8 components. Written as a
standalone doc so it can be merged into `launch-audit-findings.md` by
hand without conflicting with the A1 / A2 sections already there.

Run: 2026-05-17 (single pass, two parallel agents).

---

# Audit 3 — Hardcoded ceilings, timeouts, magic numbers

One row per load-bearing constant. "centralized?" column = `config.py`
means importable from `_1_800_operator.config`; everything else is named
in-module (module-level constant) or inline literal.

## Consolidated table

| name | value | location (file:line) | what it bounds | why this value | centralized? |
|---|---|---|---|---|---|
| ALONE_EXIT_GRACE_SECONDS | 60 s | config.py:42 | grace period after we've seen a peer and they leave before auto-leave | tuned-once; comment "once we've seen a peer and they leave, exit after this many seconds" | config.py |
| LOBBY_WAIT_SECONDS | 600 s | config.py:43 | max wait in the Meet waiting room for host admission | tuned-once; comment "max wait in Meet waiting room for host to admit us" | config.py |
| MAX_TOKENS | 2000 | config.py:44 | runaway guard on LLM output (read by LLMClient) | comment "runaway guard on LLM output; 'be brief' system-prompt does the real shaping" | config.py |
| BLEED_DEDUPE_WINDOW_SECONDS | 4.0 s | config.py:52 | how recent an S-leg caption must be to dedupe an M-leg match | comment notes window absorbs minor whisper drift while still tight enough to catch live bleed | config.py |
| BLEED_DEDUPE_SIMILARITY | 0.75 | config.py:53 | SequenceMatcher ratio threshold for bleed dedupe | comment "loose enough to absorb minor whisper-text drift… tight enough not to nuke genuine short user phrases" | config.py |
| pgrep child-reap timeout | 3 s | __main__.py:73 | how long _kill_orphaned_children waits for `pgrep -P` | unknown — no rationale in code | scattered |
| ps child-label timeout | 1 s | __main__.py:91 | how long to label each orphan via `ps` | unknown — no rationale in code | scattered |
| orphan SIGTERM→SIGKILL gap | 0.5 s | __main__.py:106 | settle time between SIGTERM and SIGKILL on safety-net path | unknown — no rationale in code | scattered |
| audio-helper --probe timeout | 5 s | __main__.py:133 | bound on the helper's probe JSON read | comment notes helper is <200ms, probe is safe; 5s is generous bound | scattered |
| TCC warmup `open -W -a` timeout | 30 s | __main__.py:180 | bound on macOS opening helper bundle for perm dialogs | comment notes `-W` blocks until helper exits "~10s via its watchdog" → 30s generous | scattered |
| ps PID-identity timeout | 2 s | __main__.py:219 | how long _pid_is_operator waits on `ps -p` | unknown — no rationale in code | scattered |
| hangup wait deadline | 3.0 s | __main__.py:618 | how long _run_hangup polls for the daemon to exit | comment "long enough to confirm exit on the happy path, not so long that the plugin skill feels stuck" | scattered |
| hangup poll interval | 0.2 s | __main__.py:624 | poll cadence inside the 3s hangup deadline | unknown — no rationale in code | scattered |
| CDP_PORT | 9222 | connectors/attach_adapter.py:83 | TCP port Chrome's remote-debugging-port binds to | Chrome convention; comment threads describe Chrome 121+ user-data-dir restriction | scattered |
| CDP_READY_TIMEOUT_SECONDS | 30 s | connectors/attach_adapter.py:94 | bound on waiting for Chrome's CDP TCP listener | comment "Chrome can take 20+s to bring up the debug server on a profile with extensions or syncing data. 30s is generous" | scattered |
| _SPEAKING_RESCAN_INTERVAL_S | 2.0 s | connectors/attach_adapter.py:143 | how often the speaking observer rescans for new tiles | comment "2s is short enough that a late joiner who immediately starts talking gets attributed correctly within their first utterance, and long enough that the per-call DOM walk doesn't pile up" | scattered |
| SLIP_PROFILE_DIR | ~/.operator/slip_profile | connectors/attach_adapter.py:90 | dedicated Chrome user-data-dir for slip mode | "Operator-owned slip profile — never touches the user's main Chrome" | scattered (path) |
| _recent_s_captions deque maxlen | 16 | connectors/attach_adapter.py:431 | rolling buffer of recent S-leg captions for bleed dedupe | unknown — no rationale in code | scattered |
| _speaking_history deque maxlen | 512 | connectors/attach_adapter.py:463 | timeline of speaking events for interval-overlap attribution | comment "512 entries ≈ 8min of dense conversation, well past any plausible Whisper lag" | scattered |
| lsof eviction timeout | 2 s | connectors/attach_adapter.py:196 | `lsof -iTCP:9222` to find Chrome holding the port | unknown — no rationale in code | scattered |
| ps eviction-verify timeout | 2 s | connectors/attach_adapter.py:210 | `ps` to verify the PID is Chrome before SIGTERM | unknown — no rationale in code | scattered |
| Chrome eviction SIGTERM→SIGKILL grace | 2 s (20×0.1) | connectors/attach_adapter.py:224-227 | wait between SIGTERM and SIGKILL during Chrome eviction | unknown — no rationale; pattern matches __main__ orphan reap | scattered |
| CDP-alive socket probe timeout | 1.0 s | connectors/attach_adapter.py:246 (`_cdp_endpoint_alive(timeout=1.0)`) | TCP probe of CDP endpoint | default arg | scattered |
| post-eviction port-release settle | 0.5 s | connectors/attach_adapter.py:586 | wait for kernel to release port before relaunching Chrome | comment "Brief settle so the kernel releases the port before the new Chrome tries to bind" | scattered |
| CDP-ready inner-socket probe timeout | 0.5 s | connectors/attach_adapter.py:360 | per-attempt connect timeout inside _wait_for_cdp_ready | unknown — no rationale in code | scattered |
| CDP-ready poll interval | 0.1 s | connectors/attach_adapter.py:364 | retry cadence inside _wait_for_cdp_ready | comment "Polling at 100ms beats a fixed sleep" | scattered |
| send_chat queue.get timeout | 10 s | connectors/attach_adapter.py:736 | block on browser-thread send result | unknown — no rationale in code | scattered |
| read_chat queue.get timeout | 10 s | connectors/attach_adapter.py:747 | block on browser-thread read result | unknown — no rationale in code | scattered |
| participant_count queue.get timeout | 5 s | connectors/attach_adapter.py:758 | block on browser-thread participant count | unknown — no rationale in code | scattered |
| participant_names queue.get timeout | 5 s | connectors/attach_adapter.py:769 | block on browser-thread participant-name scrape | unknown — no rationale in code | scattered |
| self_name queue.get timeout | 5 s | connectors/attach_adapter.py:785 | block on browser-thread self-name scrape | unknown — no rationale in code | scattered |
| send_chat textarea wait | 5000 ms | connectors/attach_adapter.py:856 | Playwright wait for textarea to appear before fill | unknown — no rationale in code | scattered |
| send_chat readback poll | 20 × 0.05 s (1 s) | connectors/attach_adapter.py:860-865 | post-send poll for new data-message-id | comment notes caller falls back to text-match dedup; 1s ceiling implied | scattered |
| meeting-entry inner poll interval | 1.0 s | connectors/attach_adapter.py:1220 | _wait_for_meeting_entry poll cadence (no timeout) | comment "Polls every 1s. No timeout — lobby admission can take many minutes" | scattered |
| meeting-entry progress-log interval | 30 s | connectors/attach_adapter.py:1217 | log "still waiting…" cadence during lobby wait | unknown — no rationale in code | scattered |
| chat-panel button wait | 3000 ms | connectors/attach_adapter.py:1255 | Playwright wait for "Chat with everyone" button before click | unknown — no rationale in code | scattered |
| chat-textarea visibility wait | 2000 ms | connectors/attach_adapter.py:1259 | Playwright wait for textarea to render after chat-toggle click | unknown — no rationale in code | scattered |
| meet-tab discovery poll deadline | 3.0 s | connectors/attach_adapter.py:1324 | scan for an existing Meet tab in Chrome's tab list before opening | comment "Brief poll (~3s) handles the post-relaunch race where Chrome's tab list hasn't propagated to CDP yet" | scattered |
| meet-tab discovery poll interval | 0.25 s | connectors/attach_adapter.py:1334 | retry cadence inside the meet-tab discovery loop | unknown — no rationale in code | scattered |
| new-tab page.goto timeout | 30000 ms | connectors/attach_adapter.py:1344 | Playwright nav timeout for opening the meeting tab | unknown — no rationale in code | scattered |
| leave() browser-thread join | 10 s | connectors/attach_adapter.py:1135 | wait for the browser thread's clean exit on leave() | comment "browser-thread close timed out (10s)" | scattered |
| leave() thread.join | 2 s | connectors/attach_adapter.py:1137 | hard upper bound on browser-thread join post-close | unknown — no rationale in code | scattered |
| _whisper_warmup_thread.join | 30 s | connectors/attach_adapter.py:1448 | wait for the pre-warm thread before falling back to sync warmup | aligns with whisper cold-load up to ~100s; comment "_start_audio_pipeline joins this thread before spawning the helper" | scattered |
| audio MAX_FRAME_BYTES | 1 << 20 (1 MiB) | connectors/attach_adapter.py:1586 | sanity cap on per-frame PCM length parsed from helper stdout | comment "helper emits ~40ms chunks (~5KB at 16kHz Float32). Anything > 1MB means the stream is corrupted" | scattered |
| helper-shutdown stdin-close wait | 2.0 s | connectors/attach_adapter.py:1741 | wait for helper to exit after stdin close | unknown — no rationale in code | scattered |
| helper-shutdown SIGTERM wait | 1.0 s | connectors/attach_adapter.py:1746 | wait between terminate() and kill() on helper | unknown — no rationale in code | scattered |
| audio_threads.join | 1.5 s | connectors/attach_adapter.py:1767 | per-thread join on the utterance + reader threads | unknown — no rationale in code | scattered |
| _FAILURE_MESSAGE_MAX | 2000 | pipeline/chat_runner.py:27 | cap on exc message string in last-failure snapshot | comment "Cap each string field in the failure snapshot — bounds disk + keeps doctor's rendered output legible" | scattered |
| _FAILURE_PTY_TAIL_MAX | 2000 | pipeline/chat_runner.py:28 | cap on pty_tail string in last-failure snapshot | (same block) PTY tail typically <2KB anyway | scattered |
| _FAILURE_LOG_TAIL_LINES | 30 | pipeline/chat_runner.py:29 | how many lines of /tmp/operator.log to capture in snapshot | unknown — no rationale beyond comment block | scattered |
| POLL_INTERVAL | 0.1 s | pipeline/chat_runner.py:100 | chat-runner main poll cadence (read_chat + state checks) | comment: dropped from 0.5→0.1 after S220 instrumentation showed consistent 500ms poll_lag_ms | scattered |
| PARTICIPANT_CHECK_INTERVAL | 3 s | pipeline/chat_runner.py:101 | cadence for participant-count refresh + roster file write | inline comment "seconds between participant count checks" | scattered |
| STREAM_PARAGRAPH_MIN_INTERVAL | 0.25 s | pipeline/chat_runner.py:107 | min wall-clock between back-to-back streamed paragraph posts | comment "(a) Meet's chat panel rate-limits rapid sends and may swallow back-to-back messages, (b) staggered posts give the user's eye a chance to register each paragraph as a distinct message" | scattered |
| CONTINUATION_WINDOW_SECONDS | 90.0 s | pipeline/chat_runner.py:117 | sticky conversation window after @claude (slip mode) | comment "follow-up messages from that same sender within CONTINUATION_WINDOW_SECONDS skip the trigger requirement" | scattered |
| CONTINUATION_DEBOUNCE_SECONDS | 2.0 s | pipeline/chat_runner.py:118 | coalesce rapid corrections inside the continuation window | comment "a quick correction ('thanks — wait, no, do Y instead') collapses into a single forwarded prompt (the last one)" | scattered |
| _permreq_safety_timeout_s | 125.0 s | pipeline/chat_runner.py:250 | defensive ceiling past the hook's own 120s | comment "slightly past the hook's own 120s ceiling — defensive cleanup if the hook self-denied without ChatRunner being notified" | scattered |
| join wait timeout | LOBBY_WAIT_SECONDS + 60 | pipeline/chat_runner.py:367 | total time to wait for connector.join | derived (config + fixed 60s pad); pad rationale unknown — no comment | derived |
| pending-sends drain cap | 16 per call | pipeline/chat_runner.py:1063 | bounded drain so a flood doesn't starve caller | comment "Bounded per call so a flood doesn't starve the caller" | scattered |
| permreq summary truncate | 200 chars | pipeline/chat_runner.py:1342/1346/1351 | per-tool input summary truncated at 200 chars | unknown — no rationale in code | scattered |
| Classifier _BRACKET_OPEN_DELAY | 0.05 s | pipeline/classifier.py:74 | bracketed-paste sequencing delay | comment "same as ClaudeCLIProvider; proven against the 14.22 spike's tough-input sweep" | scattered (duplicated) |
| Classifier _BRACKET_BODY_DELAY | 0.1 s | pipeline/classifier.py:75 | bracketed-paste sequencing delay | (same block) | scattered (duplicated) |
| Classifier _BRACKET_CLOSE_DELAY | 0.2 s | pipeline/classifier.py:76 | bracketed-paste sequencing delay | (same block) | scattered (duplicated) |
| Classifier _PTY_ROWS / _PTY_COLS | 40 / 120 | pipeline/classifier.py:78-79 | TUI window size for the classifier PTY | "Cosmetic only" (per the matching block in claude_cli) | scattered (duplicated) |
| Classifier _SETTLE_SECONDS | 6.0 s | pipeline/classifier.py:84 | initial settle wait after classifier spawn | comment "PTY settles in well under 6s in practice (matches the 14_26 spike's boot latency). Hidden inside the meeting-join window" | scattered |
| Classifier _CLASSIFY_TIMEOUT | 30.0 s | pipeline/classifier.py:89 | per-classification turn ceiling | comment "14_26 spike measured 2.1-5.0s end-to-end; 30s is a generous ceiling" | scattered |
| Classifier _POLL_SECONDS | 0.15 s | pipeline/classifier.py:93 | reply-tail polling cadence | comment "Same value the main provider uses; in the noise floor of the meeting-chat send path" | scattered (duplicated) |
| Classifier settle inner sleep | 0.1 s | pipeline/classifier.py:281 | poll cadence inside _SETTLE_SECONDS wait | unknown — no rationale in code | scattered |
| Classifier pty_reader.join | 2 s | pipeline/classifier.py:290 | bound on classifier PTY reader join | unknown — no rationale in code | scattered |
| Classifier proc.wait (SIGTERM) | 5 s | pipeline/classifier.py:299 | bound on classifier proc.wait after SIGTERM | unknown — no rationale in code | scattered |
| Classifier proc.wait (SIGKILL) | 5 s | pipeline/classifier.py:306 | bound after SIGKILL on classifier | unknown — no rationale in code | scattered |
| Classifier select.select timeout | 0.2 s | pipeline/classifier.py:135 | PTY drain thread select timeout | unknown — no rationale in code | scattered (duplicated) |
| Classifier os.read chunk | 4096 B | pipeline/classifier.py:141 | per-call chunk size from master fd | conventional | scattered (duplicated) |
| LLM max_tokens | config.MAX_TOKENS (2000) | pipeline/llm.py:34 | passed to provider.complete | config-driven | config.py |
| _BRACKET_OPEN_DELAY | 0.05 s | pipeline/providers/claude_cli.py:104 | bracketed-paste sequencing delay | comment "Bracketed-paste timings from spike_finalize.py — proven against the T1 tough-inputs sweep… Shortening any of these will eventually drop bytes on long messages; don't tune without re-running T1" | scattered (duplicated) |
| _BRACKET_BODY_DELAY | 0.1 s | pipeline/providers/claude_cli.py:105 | bracketed-paste sequencing delay | (same block) | scattered (duplicated) |
| _BRACKET_CLOSE_DELAY | 0.2 s | pipeline/providers/claude_cli.py:106 | bracketed-paste sequencing delay | (same block) | scattered (duplicated) |
| _PTY_ROWS | 40 | pipeline/providers/claude_cli.py:111 | PTY window rows for inner-claude TUI | comment "Cosmetic only; events come out via hooks regardless" | scattered (duplicated) |
| _PTY_COLS | 120 | pipeline/providers/claude_cli.py:112 | PTY window cols for inner-claude TUI | (same block) | scattered (duplicated) |
| _BOOT_CEILING_SECONDS | 180.0 s | pipeline/providers/claude_cli.py:126 | hard ceiling across whole boot (spawn → ready.flag → briefing) | extensive comment: "A healthy boot is fast: ready.flag lands in well under a second… 180s is generous enough that a slow-but-healthy boot is never false-flagged, while bounding the wait" | scattered |
| _READY_FLAG_POLL_SECONDS | 0.1 s | pipeline/providers/claude_cli.py:127 | poll cadence for ready.flag during boot | unknown — no rationale in code | scattered |
| _READY_FLAG_SLOW_WARN_SECONDS | 15.0 s | pipeline/providers/claude_cli.py:131 | log "slower than a healthy boot" warning threshold | comment "One-time internal log breadcrumb if ready.flag is slower than a healthy boot — no behaviour change, just a forensic marker" | scattered |
| _PTY_QUIET_BLOCKED_SECONDS | 5.0 s | pipeline/providers/claude_cli.py:141 | structural "blocked on a prompt" signal threshold | comment "A booting claude reaches ready.flag in under a second; if it instead renders terminal output and then goes SILENT with the flag still absent, it has stopped emitting and is WAITING" | scattered |
| _REPLIES_POLL_SECONDS | 0.15 s | pipeline/providers/claude_cli.py:161 | replies.jsonl tail-loop polling cadence | comment "Tail-loop polling cadence for replies.jsonl. 0.15s matches the spike and is short enough that p50 turn TTFR (Stop hook fires → reply posted) stays in the noise floor of the meeting-chat send path" | scattered |
| _TRANSCRIPT_FINAL_DRAIN_SETTLE | 0.3 s | pipeline/providers/claude_cli.py:166 | settle before one final transcript drain after Stop fires | comment "After the Stop hook fires, the turn's final assistant block may still be a write-beat behind in the transcript JSONL. Settle this long, then do one last transcript drain" | scattered |
| _FOREIGN_HOOK_DELAY_WARN_SECONDS | 5.0 s | pipeline/providers/claude_cli.py:178 | log-only foreign-hook delay threshold | comment "the turn-end delay below is a noisier proxy signal — logged only. If the gap between the final assistant block landing and the Stop row appearing exceeds this, foreign hooks may have run in between" | scattered |
| PTY drain select timeout | 0.2 s | pipeline/providers/claude_cli.py:227 | drain-thread select() period | unknown — no rationale in code | scattered (duplicated) |
| PTY drain chunk | 4096 B | pipeline/providers/claude_cli.py:233 | per-call chunk size from master fd | conventional | scattered (duplicated) |
| _pty_tail default n_bytes | 2000 | pipeline/providers/claude_cli.py:803 | tail bytes captured for diagnostics | default arg; matches _FAILURE_PTY_TAIL_MAX (potential duplicate constant) | scattered |
| inner-claude SIGTERM wait | 5 s | pipeline/providers/claude_cli.py:772 | wait after killpg SIGTERM in _terminate_inner | unknown — no rationale in code | scattered |
| inner-claude SIGKILL wait | 5 s | pipeline/providers/claude_cli.py:779 | wait after killpg SIGKILL in _terminate_inner | unknown — no rationale in code | scattered |
| pty_reader_thread.join | 2 s | pipeline/providers/claude_cli.py:763 | bound on PTY-drain thread join | unknown — no rationale in code | scattered |
| boot_done.wait inside _run_turn | _BOOT_CEILING_SECONDS + 30 | pipeline/providers/claude_cli.py:1402 | wait for boot completion when an @mention races boot | comment "Bounded by the boot ceiling + margin" | derived |
| per-turn reply timeout | 600.0 s | pipeline/providers/claude_cli.py:1462 | wait_for_next_reply timeout for the in-turn Stop hook | comment "Generous per-turn timeout — claude tool loops can run minutes legitimately. The user cancels via /operator:hangup if a tool chain wedges; no operator-imposed deadline" | scattered |
| DisclaimedProcess wait poll interval | 0.05 s | pipeline/_disclaimed_spawn.py:156 | poll cadence in custom wait(timeout=) impl | "posix_spawn'd processes can't use waitpid timeout natively, and threads avoid SIGCHLD complications" | scattered |
| AEC _MAX_FRAME_BYTES | 1 << 20 | pipeline/aec_cleaner.py:40 | frame-length cap reading binary stdout | comment "matches the binary's own cap" | scattered (duplicated with attach_adapter) |
| AEC _HEADER_LEN | 5 | pipeline/aec_cleaner.py:39 | 1B tag + 4B BE length | protocol constant | scattered (duplicated) |
| AEC stop() default timeout | 2.0 s | pipeline/aec_cleaner.py:130 | wait for AEC subprocess to drain on stdin close | unknown — no rationale in code | scattered |
| AEC kill-wait | 1.0 s | pipeline/aec_cleaner.py:152 | wait after kill before giving up | unknown — no rationale in code | scattered |
| AEC stderr/stdout reader join | 1.0 s | pipeline/aec_cleaner.py:158 | bound on reader-thread join | unknown — no rationale in code | scattered |
| SAMPLE_RATE | 16000 Hz | pipeline/audio.py:40 | whisper input sample rate | matches whisper standard | scattered |
| UTTERANCE_CHECK_INTERVAL | 0.5 s | pipeline/audio.py:47 | utterance-detection loop cadence | comment "Tuned against real meeting audio; don't loosen without re-tuning. SILENCE_THRESHOLD=2 checks @ 0.5s = ~1s of trailing silence to call an utterance done" | scattered |
| UTTERANCE_SILENCE_THRESHOLD | 2 | pipeline/audio.py:48 | consecutive silent ticks to call utterance done | (same block) | scattered |
| UTTERANCE_MAX_DURATION | 10 s | pipeline/audio.py:49 | forced cut for runaway utterances | comment "MAX_DURATION=10s caps runaway utterances (long speakers get chunked)" | scattered |
| UTTERANCE_SILENCE_RMS | 0.02 | pipeline/audio.py:50 | RMS silence threshold | comment "RMS=0.02 is the floor that rejects HVAC / fan noise but catches normal speech" | scattered |
| Whisper warmup silence pad | 0.5 s × 16000 Hz | pipeline/audio.py:274 | prepend silence so whisper doesn't drop first word | comment "without it whisper drops the first word of short utterances. Carried over from the mlx-whisper era verbatim" | scattered |
| Whisper beam_size | 5 | pipeline/audio.py:124, 281 | faster-whisper decoder beam | unknown — convention (also passed in doctor's warmup) | scattered (duplicated) |
| Whisper repetition-hallucination word threshold | 0.5 | pipeline/audio.py:257 | mostly-repeated single-token cutoff | unknown — no rationale in code | scattered |
| Whisper repetition-hallucination bigram threshold | 0.5 | pipeline/audio.py:263 | mostly-repeated bigram cutoff | unknown — no rationale in code | scattered |
| _chat_tail deque maxlen | 200 | pipeline/meeting_record.py:68 | in-memory chat-tail size for LLM context | unknown — no rationale in code | scattered |
| RESULT_BYTE_CEILING | 80000 B | mcp_servers/record_server.py:93 | per-tool response ceiling | comment "A typical 1-hour meeting with ~500 caption events renders to ~50KB; 80KB fits most meetings in one call… The ceiling still bites for unusually long meetings; when it does, the truncation notice from _enforce_byte_ceiling makes paging explicit" | scattered |
| DEFAULT_LIST_LIMIT | 100 | mcp_servers/record_server.py:94 | default `limit` for list-*-style tools | unknown — no rationale in code | scattered |
| DEFAULT_SEARCH_LIMIT | 20 | mcp_servers/record_server.py:95 | default `limit` for search-*-style tools | unknown — no rationale in code | scattered |
| DEFAULT_RECORD_LIMIT | 200 | mcp_servers/record_server.py:566 | default `limit` for meeting-record list tool | unknown — no rationale in code | scattered |
| Result truncation overhead | 400 B | mcp_servers/record_server.py:234 | byte budget reserved for the truncation notice | unknown — no rationale in code | scattered |
| _FETCH_TIMEOUT_SECONDS | 5 s | pipeline/update_check.py:29 | HTTPS GET of marketplace.json | unknown — no rationale in code (chat_runner comment refers to it as "5s timeout") | scattered |
| doctor git --version timeout | 2 s | pipeline/doctor.py:134 | bound on git version probe | unknown — no rationale in code | scattered |
| doctor audio-helper probe timeout | 5 s | pipeline/doctor.py:219 | bound on helper --probe call | unknown — no rationale in code (matches __main__ probe) | scattered (duplicated) |
| Permreq hook timeout (default) | 120 s | operator-plugin/hooks/scripts/permission_request.sh:40 | round-trip ceiling waiting for chat answer | comment "Generous for an attentive meeting participant but well below the hook's own command timeout (600s default), so we always emit a clean JSON deny rather than getting killed by Claude Code mid-poll" | env-overridable |
| Permreq hook poll interval | 0.2 s | operator-plugin/hooks/scripts/permission_request.sh:110 | python poll cadence for the answer file | unknown — no rationale in code | scattered |
| Operator session-dir path | ~/.operator/sessions/<uuid> | pipeline/providers/claude_cli.py:278 | per-meeting state dir (replies.jsonl, ready.flag, etc.) | constructor default; comment "fresh ~/.operator/sessions/<uuid>/ created on construction" | scattered (path) |
| LOG path | /tmp/operator.log | __main__.py:781,1010, others | operator's own logging destination | hardcoded; comment in __main__.py "operator log (/tmp/operator.log) keeps detailed activity" | scattered |
| MIN_PY_MAJOR / MIN_PY_MINOR | 3 / 10 | install.sh:25-26 | minimum host Python version for operator install | install-time constant; falls back to uv-managed Python 3.12 if missing | scattered |
| install playwright skip path | n/a | install.sh body | (no Playwright runtime download step found despite header comment line 12) | comment-only — out of scope | scattered |
| install TCC warmup `open -W -n -a` | (no explicit timeout) | install.sh:335 | macOS opens helper bundle to drive perm prompts | helper exits via its own 10s watchdog (comment line 332) | scattered |
| Swift helper Screen-Recording prompt sleep | 3 s | swift/operator-audio-capture.swift:124 | sleep after CGRequestScreenCaptureAccess | unknown — no rationale in code | scattered |
| Swift helper mic prompt sema timeout | 10 s | swift/operator-audio-capture.swift:147 | wait for AVCaptureDevice.requestAccess callback | unknown — no rationale in code | scattered |
| Swift helper SCK target sampleRate | 48000 Hz | swift/operator-audio-capture.swift:452 | required by macOS 15 SCStream | comment "macOS 15 (Sequoia) SCStream silently denies audio callbacks when sampleRate/channelCount don't match the system's preferred audio format. Apple's docs note 48000/2 as the working config" | scattered |
| Swift helper SCK target channelCount | 2 | swift/operator-audio-capture.swift:453 | same | (same block) | scattered |
| Swift helper SCK queueDepth | 5 | swift/operator-audio-capture.swift:454 | SCStream callback queue depth | unknown — no rationale in code | scattered |
| Swift helper target output rate | 16000 Hz | swift/operator-audio-capture.swift:219 | whisper-compatible PCM emitted by helper | comment "matches the mic path — Float32 mono 16kHz. Whisper downstream expects this" | scattered |
| Swift helper restart stopCapture wait | 3 s | swift/operator-audio-capture.swift:503 | wait for old SCStream to stop before restart | unknown — no rationale in code | scattered |
| Swift helper restart startCapture wait | 3 s | swift/operator-audio-capture.swift:517 | wait for new SCStream to start during restart | unknown — no rationale in code | scattered |
| Swift helper periodic-stats schedule | 2..12 s step 2 | swift/operator-audio-capture.swift:594-598 | stderr telemetry beats for first 12s | comment "Time-series visibility every 2s for the first 12s — surfaces SCK startup patterns" | scattered |
| Swift helper watchdog | 10 s | swift/operator-audio-capture.swift:608 | FATAL if mic 0 callbacks in 10s; WARN if system 0 in 10s | comment "Mic silent at 10s is unrecoverable — exit so parent fails fast" | scattered |
| Swift helper stdin-EOF stopCapture wait | 2 s | swift/operator-audio-capture.swift:635 | wait for clean SCStream stop on shutdown | unknown — no rationale in code | scattered |
| Swift helper TCC-fail exit codes | 3 / 5 / 7 | swift/operator-audio-capture.swift:128,151,156,222 | screen-recording deny / mic deny / target-format build fail | "exit code 4 = system silent-failure" referenced in comment but not seen in scanned region | scattered |

Total constants tabulated: **110**.

## Per-component sections

### Audit 3 · Component 1 (CLI entry & lifecycle)

Files in scope: `src/_1_800_operator/__main__.py`, `src/_1_800_operator/config.py`, slip.pid handling, shutdown teardown.

Findings:
- `config.py` is the only intentionally-centralized constants file. Holds: `ALONE_EXIT_GRACE_SECONDS=60`, `LOBBY_WAIT_SECONDS=600`, `MAX_TOKENS=2000`, `BLEED_DEDUPE_WINDOW_SECONDS=4.0`, `BLEED_DEDUPE_SIMILARITY=0.75`. Plus 4 path constants (`ENV_FILE`, `DEBUG_DIR`, `LAST_FAILURE_PATH`, `CURRENT_MEETING_PARTICIPANTS_PATH`).
- `__main__.py` has six inline subprocess `timeout=` literals (3, 1, 5, 30, 2, 2) for child-reap / probe / TCC warmup / ps liveness paths — none named, none in config.py.
- Hangup polling: `deadline = monotonic() + 3.0` plus `_time.sleep(0.2)` inline at lines 618 + 624.
- Daemonization, signal handling, lockfile paths (`~/.operator/slip.pid`, `~/.operator/.current_meeting`) are inline literals.
- LOG path `/tmp/operator.log` is hardcoded in `logging.basicConfig` in both `_run_slip` and `_run_wiretap` — duplicated.

### Audit 3 · Component 2 (Slip Chrome connector)

Files in scope: `connectors/attach_adapter.py`, `connectors/session.py`, `connectors/chat_dom_js.py`, `connectors/base.py`.

Findings:
- `CDP_PORT=9222` and derived `CDP_URL` at attach_adapter.py:83-84 (module-level constants — good shape; just not in `config.py`).
- `CDP_READY_TIMEOUT_SECONDS=30` (top-level constant). Inner socket-probe timeouts 0.5/1.0s are unnamed defaults.
- `SLIP_PROFILE_DIR` is hardcoded to `~/.operator/slip_profile` at attach_adapter.py:90.
- `_SPEAKING_RESCAN_INTERVAL_S=2.0` named constant at line 143.
- Deques: `_recent_s_captions` maxlen=16 (line 431, no rationale), `_speaking_history` maxlen=512 (line 463, well-documented).
- Browser-thread queue.get timeouts: send/read at 10s, three roster lookups at 5s — four unnamed literals at lines 736, 747, 758, 769, 785. **All five could collapse to one or two named constants.**
- Playwright timeouts inside the connector are mixed units (ms vs s) — `wait_for(timeout=5000)`, `wait_for(timeout=3000)`, `wait_for(timeout=2000)`, `page.goto(timeout=30000)`. None named.
- Audio-helper teardown waits 2.0/1.0/1.5s in three different `wait(timeout=…)` / `join(timeout=…)` lines — none named.
- `MAX_FRAME_BYTES = 1 << 20` is **duplicated** in attach_adapter.py:1586 and aec_cleaner.py:40 (`_MAX_FRAME_BYTES`). Frame header length 5 also duplicated (`_FRAME_HEADER_LEN` vs `_HEADER_LEN`).
- The bleed-dedupe `window` and `threshold` ARE read from `config.py` — one of the few cross-file references.

### Audit 3 · Component 3 (Chat runner & trigger logic)

Files in scope: `pipeline/chat_runner.py`, `pipeline/classifier.py` (no `pipeline/confirmation.py` — not present in tree).

Findings:
- Three named module-level constants for failure snapshot caps (`_FAILURE_MESSAGE_MAX=2000`, `_FAILURE_PTY_TAIL_MAX=2000`, `_FAILURE_LOG_TAIL_LINES=30`).
- Four named runtime knobs at module top: `POLL_INTERVAL=0.1`, `PARTICIPANT_CHECK_INTERVAL=3`, `STREAM_PARAGRAPH_MIN_INTERVAL=0.25`, `CONTINUATION_WINDOW_SECONDS=90.0`, `CONTINUATION_DEBOUNCE_SECONDS=2.0`. **Strong candidates for `config.py` — all are tuned-once runtime behavior.**
- `_permreq_safety_timeout_s=125.0` is an instance attr — derived from the hook's own 120s ceiling.
- `pending-sends drain cap = 16` inline literal at line 1063.
- `200`-char truncation for permreq summaries hard-coded three times at lines 1342/1346/1351.
- Classifier file (`pipeline/classifier.py`) **duplicates** `_BRACKET_OPEN_DELAY` / `_BRACKET_BODY_DELAY` / `_BRACKET_CLOSE_DELAY` / `_PTY_ROWS` / `_PTY_COLS` / `_POLL_SECONDS` from `pipeline/providers/claude_cli.py`. The comment explicitly says "same as ClaudeCLIProvider" — known duplication, not centralized. Five constants duplicated across two files.
- Classifier-specific: `_SETTLE_SECONDS=6.0`, `_CLASSIFY_TIMEOUT=30.0`.

### Audit 3 · Component 4 (LLM provider & PTY)

Files in scope: `pipeline/llm.py`, `pipeline/providers/claude_cli.py`, `pipeline/providers/base.py`, `pipeline/_disclaimed_spawn.py`, `bridges/claude.py`.

Findings:
- `llm.py` reads `config.MAX_TOKENS`. No other constants worth noting.
- `claude_cli.py` is the densest file for runtime tuning: 12 named module-level constants. All thoughtfully commented.
  - Boot ceilings: `_BOOT_CEILING_SECONDS=180.0`, `_READY_FLAG_POLL_SECONDS=0.1`, `_READY_FLAG_SLOW_WARN_SECONDS=15.0`, `_PTY_QUIET_BLOCKED_SECONDS=5.0`.
  - Reply tail: `_REPLIES_POLL_SECONDS=0.15`, `_TRANSCRIPT_FINAL_DRAIN_SETTLE=0.3`, `_FOREIGN_HOOK_DELAY_WARN_SECONDS=5.0`.
  - PTY: `_PTY_ROWS=40`, `_PTY_COLS=120`, bracket-paste 0.05/0.1/0.2.
  - Per-turn reply timeout `600.0` is an inline literal at line 1462 (not named).
  - `_pty_tail` default n_bytes=2000 is an inline default arg — same magnitude as `_FAILURE_PTY_TAIL_MAX` (potential duplicate).
- `bridges/claude.py` holds two non-numeric constants only: `TRIGGER_PHRASE`, `REPLY_PREFIX_SLIP`.
- `_disclaimed_spawn.py` has one inline `_time.sleep(0.05)` in the custom `wait(timeout=)` polling impl.

### Audit 3 · Component 5 (Audio pipeline)

Files in scope: `pipeline/audio.py`, `pipeline/aec_cleaner.py`, `pipeline/transcript.py` (not present in tree — appears to have been replaced by `pipeline/meeting_record.py`), Swift helper interface only (full Swift code goes under Component 8).

Findings:
- `audio.py` has 5 named constants: `SAMPLE_RATE=16000`, `UTTERANCE_CHECK_INTERVAL=0.5`, `UTTERANCE_SILENCE_THRESHOLD=2`, `UTTERANCE_MAX_DURATION=10`, `UTTERANCE_SILENCE_RMS=0.02`. Plus `WHISPER_HALLUCINATIONS` set, `_FW_MODEL_REPO`, `_FW_COMPUTE_TYPE`. All well-documented as voice-preserved heritage.
- `silence_pad = SAMPLE_RATE * 0.5` inline at line 274 — 0.5 is unnamed.
- `beam_size=5` passed verbatim in two places (line 124, line 281) — **duplicated** also in `pipeline/doctor.py:334`. Three call-sites, no named constant.
- Repetition-hallucination thresholds (0.5/0.5) inline at lines 257/263 — unnamed.
- `aec_cleaner.py`: `_TAG_RENDER=b"S"`, `_TAG_CAPTURE=b"M"`, `_HEADER_LEN=5`, `_MAX_FRAME_BYTES=1<<20`. **The frame protocol tags and header length are duplicated in `attach_adapter.py`** (`_FRAME_TAG_SYSTEM`/`_FRAME_TAG_MIC`/`_FRAME_HEADER_LEN`). Same values, two definitions.
- Subprocess teardown waits 2.0/1.0/1.0/1.0s — unnamed.

### Audit 3 · Component 6 (Meeting record & bundled MCP)

Files in scope: `pipeline/meeting_record.py`, `mcp_servers/record_server.py`.

Findings:
- `meeting_record.py`: only one constant — `_chat_tail` deque maxlen=200 (line 68). No rationale in code. Plus `DEFAULT_ROOT = ~/.operator/history` (path).
- `record_server.py`: three named constants at lines 93-95 (`RESULT_BYTE_CEILING=80000`, `DEFAULT_LIST_LIMIT=100`, `DEFAULT_SEARCH_LIMIT=20`) plus `DEFAULT_RECORD_LIMIT=200` at line 566. The 80KB ceiling has a long comment block. Other three have none.
- `RESULT_BYTE_CEILING - 400` reserved overhead inline at line 234 (truncation-notice room).

### Audit 3 · Component 7 (Hooks)

Files in scope: `operator-plugin/hooks/scripts/*.sh`.

Findings:
- `permission_request.sh`: env-overridable `TIMEOUT_S="${OPERATOR_PERMREQ_TIMEOUT_S:-120}"` at line 40. The 120s default is the floor that drives operator's own `_permreq_safety_timeout_s=125.0` defensive ceiling in chat_runner.py — that pair MUST stay in sync.
- `permission_request.sh:110`: inline `time.sleep(0.2)` for answer-file polling.
- `session_start.sh` and `stop.sh`: no timeouts, no caps — purely IO append-and-exit scripts.
- `_common.sh`: no numeric constants.
- The hook's command-timeout reference of "600s default" in the comments tracks Claude Code's own hook-timeout default, not an operator constant.

### Audit 3 · Component 8 (Install / packaging / setup)

Files in scope: `install.sh`, `scripts/build_signed_helper.sh`, `src/_1_800_operator/swift/operator-audio-capture.swift`, `pipeline/doctor.py`, `pipeline/update_check.py`, plugin marketplace files.

Findings:
- `install.sh`: `MIN_PY_MAJOR=3`, `MIN_PY_MINOR=10` at lines 25-26. Repo URL, env path, signing identity (in build_signed_helper.sh:26 — `Developer ID Application: Jojo Shapiro (DSW7V72HT7)` is hardcoded).
- `install.sh`: no explicit timeout on the `open -W -n -a` TCC warmup; relies on the Swift helper's 10s watchdog.
- `update_check.py`: `_FETCH_TIMEOUT_SECONDS=5` (named, line 29). Two hardcoded URLs (local marketplace cache path, remote marketplace.json URL).
- `doctor.py`: subprocess timeouts `2` (git), `5` (audio probe) — inline literals. `_TCC_STATUS_DETAIL` is a static dict. `WhisperModel(beam_size=5)` duplicated from `audio.py`. Faster-whisper repo/compute_type strings duplicated from `audio.py` (`"deepdml/faster-whisper-large-v3-turbo-ct2"`, `"int8"`).
- `swift/operator-audio-capture.swift`:
  - SCK config (`sampleRate=48000`, `channelCount=2`, `queueDepth=5`) at lines 452-454.
  - Target output (`sampleRate=16000`, mono Float32) at line 219.
  - Permission-request waits: `Thread.sleep(forTimeInterval: 3)` for screen recording, `sema.wait(timeout: .now() + 10)` for mic, `sema.wait(timeout: .now() + 3)` × 2 for restart stop/start, `sema.wait(timeout: .now() + 2)` for shutdown.
  - Watchdog at 10s (line 608) — pairs with the 12s stats schedule.
  - Stats schedule `stride(from: 2, through: 12, by: 2)` at line 594.
  - Exit codes 3 / 5 / 7 / and (per comment) 4 — no named enum.
- `scripts/build_signed_helper.sh`: notarytool keychain-profile name `notarytool-password` (line 28), bundle id `com.1-800-operator.audio-capture` (line 25), out paths.

## A3 Summary observations

**Centralization shape.** Only 5 numeric constants live in `config.py` (`ALONE_EXIT_GRACE_SECONDS`, `LOBBY_WAIT_SECONDS`, `MAX_TOKENS`, `BLEED_DEDUPE_WINDOW_SECONDS`, `BLEED_DEDUPE_SIMILARITY`). Every other tunable is module-local or inline. The remaining ~90 constants are scattered. The pattern that *is* consistent: each module that owns a behavior owns its constants at module top with a comment block. Hot exceptions: `__main__.py` inline subprocess timeouts and `connectors/attach_adapter.py` browser-queue timeouts, both of which are bare literals with no name.

**Documented duplications.**
1. Bracketed-paste delays + PTY winsize + `_POLL_SECONDS` are duplicated between `pipeline/classifier.py` (5 constants) and `pipeline/providers/claude_cli.py`. The classifier comment explicitly acknowledges "same as ClaudeCLIProvider". Six related constants in two files.
2. Audio frame protocol (`_TAG_RENDER/_TAG_CAPTURE`, `_HEADER_LEN`, `_MAX_FRAME_BYTES`) is duplicated between `pipeline/aec_cleaner.py` and `connectors/attach_adapter.py` under slightly different names (`_FRAME_TAG_SYSTEM`/`_FRAME_TAG_MIC`/`_FRAME_HEADER_LEN`). Same byte values, two files, two naming schemes. Both files acknowledge the helper Swift source as source-of-truth.
3. `WhisperModel` instantiation parameters (`"deepdml/faster-whisper-large-v3-turbo-ct2"`, `device="cpu"`, `compute_type="int8"`, `beam_size=5`) appear verbatim in `pipeline/audio.py` (the production path) and `pipeline/doctor.py:_check_faster_whisper_warm` (the diagnostic warmup). The doctor comment says it runs "the same faster-whisper warmup operator does" — but the constants are typed twice. A drift here would silently degrade doctor's coverage.
4. `_FAILURE_PTY_TAIL_MAX=2000` (chat_runner) and `_pty_tail` default `n_bytes=2000` (claude_cli) are the same number for the same purpose — no shared constant.
5. The 120s permreq timeout in `operator-plugin/hooks/scripts/permission_request.sh` and the 125s safety ceiling in `chat_runner.py` are intentionally paired but live in different repos with no documentation linking them. Drift here would either cause spurious early-cleanup or hidden hangs.
6. Hardcoded path `/tmp/operator.log` appears in `__main__.py` (twice — `_run_slip` and `_run_wiretap`), and is read by `pipeline/chat_runner.py:_operator_log_tail`. Three references to the same string, no named constant.
7. Audio-helper install path `~/.operator/bin/Operator.app/Contents/MacOS/Operator` appears in `__main__.py:118-119`, `pipeline/doctor.py:42-45`, and `connectors/attach_adapter.py:103-106`. Three files, three definitions, all using identical path construction.

**Strong candidates for promotion to `config.py`.** These are runtime knobs the user would tune if anyone tunes them:
- `POLL_INTERVAL`, `PARTICIPANT_CHECK_INTERVAL`, `STREAM_PARAGRAPH_MIN_INTERVAL`, `CONTINUATION_WINDOW_SECONDS`, `CONTINUATION_DEBOUNCE_SECONDS` (chat_runner — already comment-block-documented).
- `_BOOT_CEILING_SECONDS`, `_REPLIES_POLL_SECONDS`, per-turn reply `600.0` (claude_cli — operator's ceiling on the LLM brain).
- `CDP_READY_TIMEOUT_SECONDS` (attach_adapter — Chrome boot bound).

**Magic numbers with no rationale at all.** Roughly half the entries flagged "unknown — no rationale in code" are subprocess teardown timings (`wait(timeout=2)` / `join(timeout=1.5)` etc.). Most are in the 1-5s range and look like "feels-right" defaults. Not obviously wrong, but they're an easy maintenance trap — a future reviewer can't tell whether a value is load-bearing or aspirational.

**Numbers that look obviously off.** None spotted. The two numbers most likely to be over-tuned are `STREAM_PARAGRAPH_MIN_INTERVAL=0.25` (extremely fast paragraph cadence — Meet rate-limits may or may not actually require this aggressive a value) and the permreq summary 200-char truncation hardcoded 3 times in chat_runner.py:1342-1351 (looks copy-pasted). Both deserve a triage pass, not a blanket recommendation.

**Pathnames.** `~/.operator/...` paths are universally inline. `config.py` defines four of them (env, debug, last-failure, participants) but a dozen more (slip_profile, slip.pid, sessions, .current_meeting, history, bin/Operator.app, bin/aec3) are constructed in-place by the modules that use them. Worth a separate "where do operator's on-disk state files live?" consolidation pass.

---

# Audit 5 — Hardcoded secrets / credentials

## Verdict

**CLEAN.** No real API keys, OAuth secrets, tokens, private certs, or signing material in HEAD or in git history. Two minor non-blocking hygiene notes (gitignore defensive globs, naming drift); both are future-leak-prevention suggestions, not current leaks.

## Tree scan

Scope: 313 tracked files (per `git ls-files`). Patterns scanned:

- `api[_-]?key|secret|token|password|bearer` (case-insensitive)
- Real key shapes: `sk-[a-zA-Z0-9_-]{30+}`, `sk-(ant|proj|live)-…`, `AIza[0-9A-Za-z_-]{35}`, `gh[ps]_[a-zA-Z0-9]{30+}`, `xox[bp]-…`
- `-----BEGIN (PRIVATE|RSA|EC|OPENSSH|CERTIFICATE)`
- High-entropy runs (≥40 alnum chars) in core source files

126 files matched the broad keyword filter. Per-component classification follows.

### Audit 5 · Component 1 (CLI entry & lifecycle)

Files: `src/_1_800_operator/__main__.py`, `src/_1_800_operator/config.py`.

Clean. All hits are env-var **names** or unrelated tokens:

- `__main__.py:931` — the word "disk-resident" in a comment, false positive on the `secret/...` keyword regex (substring of nothing sensitive; was actually noise-matched on adjacent file context).
- `config.py:44` — `MAX_TOKENS = 2000`, the LLM output cap. Not a credential.

No `.env` parsing leaks values; secrets are read from `~/.operator/.env` at runtime via `python-dotenv`, never embedded.

### Audit 5 · Component 2 (Slip Chrome connector)

Files: `src/_1_800_operator/connectors/{attach_adapter.py, session.py, chat_dom_js.py, base.py}`.

Clean.

- `attach_adapter.py:14` — comment reading "malware harvesting OAuth tokens via DevTools (Chromium issue 40066423, …)". Reference to a known Chromium bug, not a credential. The CDP attack-surface explanation is the security narrative.

No cookies, session tokens, or profile material in the tree. Slip profile lives at `~/.operator/slip_profile/` — outside the repo, gitignored category irrelevant.

### Audit 5 · Component 3 (Chat runner & trigger logic)

Files: `src/_1_800_operator/pipeline/{chat_runner.py, classifier.py}`. (`pipeline/confirmation.py` does not exist in tree.)

Clean. All hits are:

- `classifier.py:97`, `:413`, `:495` — the word "token" used in the YES/NO classifier prose (literally "tokens", "token list"). Not credentials.
- `classifier.py:225` — `env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}` — this is the strip-key-from-spawn-env safeguard (per the `feedback_no_direct_llm_api` memory). The env-var name is a reference, not a key value.
- `chat_runner.py:868` — comment explaining why we don't shovel `str(e)` into chat: "it can carry response payloads / tokens / upstream secrets". Documentation of the safety rule, not a key.

### Audit 5 · Component 4 (LLM provider & PTY)

Files: `src/_1_800_operator/pipeline/{llm.py, providers/claude_cli.py, providers/base.py, _disclaimed_spawn.py}`, `bridges/claude.py`.

Clean. All hits are:

- `claude_cli.py:68-69`, `:511` — docstring + code that strips `ANTHROPIC_API_KEY` from the inner-claude spawn env unconditionally. Env-var name; no value embedded.
- `claude_cli.py:1294`, `:1305`, `:1319` — `max_tokens` parameter on `complete()` signature. Not a credential.
- `providers/base.py:71`, `:94`, `:101`, `:112`, `:126`, `:129` — same `max_tokens` parameter naming + a comment "Fire a 1-token request to warm…".
- `llm.py:34`, `:67`, `:79`, `:88`, `:109` — `_max_tokens` plumbing through `LLMClient.ask()`.

No API keys, OAuth secrets, or bearer values in any provider file. Inner-claude inherits its credential (the user's `claude` CLI OAuth) from `~/.claude/`; operator passes nothing.

### Audit 5 · Component 5 (Audio pipeline)

Files: `src/_1_800_operator/pipeline/{audio.py, aec_cleaner.py, transcript.py}`, `src/_1_800_operator/swift/`.

Clean. No keyword hits in `audio.py` / `aec_cleaner.py` / `transcript.py`. The Swift surface contains:

- `swift/Info.plist` — bundle metadata + TCC usage strings only.
- `swift/helper.entitlements` — single entitlement `com.apple.security.device.audio-input`.
- `swift/operator-audio-capture.swift` — pure source code.
- `swift/Operator` — Mach-O binary in the working tree but **not tracked by git** (the previous `.app` bundle layout was deleted from the working tree per `git status`; the new Mach-O is untracked).
- `swift/operator-audio-capture.app/Contents/{Info.plist, MacOS/operator-audio-capture, _CodeSignature/CodeResources}` — tracked in HEAD but deleted from working tree. The CodeResources file is a file-manifest plist (no private keys); embedded code signatures in Mach-O carry only the public cert chain. No private key, p12, or notary credential is committed.

### Audit 5 · Component 6 (Meeting record & bundled MCP)

Files: `src/_1_800_operator/pipeline/meeting_record.py`, `src/_1_800_operator/mcp_servers/record_server.py`.

Clean.

- `meeting_record.py:130` — docstring comment mentioning "post-meeting lookup needs disk-resident JSONLs" (noise-matched on "secret/...").

No tokens or auth material handled at this layer.

### Audit 5 · Component 7 (Hooks)

The `operator-plugin/` repo (which holds the slash-command-shipped hook scripts) is **not in this repository**; per CLAUDE.md it's a separate plugin published via the marketplace. Within this repo:

- `.claude-plugin/marketplace.json` — only public metadata: plugin name, GitHub source repo, version, license. No secrets.

No hook scripts to scan in this tree.

### Audit 5 · Component 8 (Install / packaging / setup)

Files: `install.sh`, `scripts/build_signed_helper.sh`, `src/_1_800_operator/swift/`, `src/_1_800_operator/pipeline/doctor.py`, `src/_1_800_operator/pipeline/update_check.py`, `pyproject.toml`, `pypi-stub/pyproject.toml`, `.claude-plugin/marketplace.json`, `.github/workflows/publish.yml`, `.github/ISSUE_TEMPLATE/*.yml`.

Clean.

- `install.sh:98-105` — writes a **placeholder** `~/.operator/.env` template with a single commented-out example `# GITHUB_TOKEN=ghp_...` (literal ellipsis, not a real token). File is created with `chmod 600`. No key value embedded.
- `scripts/build_signed_helper.sh` — references the signing identity by **name** only:
  - `SIGN_IDENTITY="Developer ID Application: Jojo Shapiro (DSW7V72HT7)"` — identity name + TEAMID. TEAMID is not secret (explicitly per the audit prompt); the actual private key lives in Keychain.
  - `NOTARY_PROFILE="notarytool-password"` — the **name** of a Keychain-stored credential profile (`xcrun notarytool store-credentials`). The credential itself never appears in the script; the script just asks Keychain for it by profile name.
  - No `.p12`, `.cer`, `.mobileprovision`, or app-specific-password value is in the tree.
- `docs/apple-dev-setup.md` — procedural guide. Mentions `shapirojojo@gmail.com` and `DSW7V72HT7`; both are public (email is already in `pyproject.toml` as author, TEAMID is explicitly non-secret).
- `.github/workflows/publish.yml` — uses PyPI trusted publishing (`uv publish --trusted-publishing always`) — OIDC-based, no token at all. The job declares `permissions: id-token: write`. No secret pulled from env, no inline secret.
- `.github/ISSUE_TEMPLATE/bug_report.yml` — actively warns submitters to **scrub** their pasted logs of `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GITHUB_TOKEN`. Reference, not key.
- `pyproject.toml`, `pypi-stub/pyproject.toml`, `.vscode/settings.json` — no secrets.

## Git history scan

Commands run:

```
git log --all -p -G 'sk-(ant|proj|live)-[a-zA-Z0-9_]{20,}'
git log --all -p -G 'AIza[0-9A-Za-z_-]{35}'
git log --all -p -G 'gh[ps]_[a-zA-Z0-9]{30,}'
git log --all -p -G 'Bearer [a-zA-Z0-9]{20,}'
git log --all -p -G '-----BEGIN (PRIVATE|RSA|EC|OPENSSH|CERTIFICATE)'
git log --all -p -G 'sk-[a-zA-Z0-9_-]{30,}'
git log --all -p -S 'ANTHROPIC_API_KEY=sk-'
git log --all -p -S 'OPENAI_API_KEY=sk-'
git log --all -p -- '*.env' '*.env.*' '*.pem' '*.key' '*.p12' '*.mobileprovision' 'credentials.json' 'token.json'
git log --all --diff-filter=A --name-only   # all files ever added
git log --all --diff-filter=D --name-only   # all files ever deleted
```

History size: 744 commits across all refs.

Surfaced matches (all benign):

1. **`AIzaSyCOb4us-UcQ-UzbCGLOL5axXsDxIJ2R5Do`** — appeared in deleted `debug/admit_diagnostic.html`, `debug/post_admit_pill_persisted.html`, `debug/post_admit_success.html` page dumps (commits `e7f5240` deletion, `1d8feff` addition). This is **Google Meet's own public web-app API key**, embedded by Google in their meet.google.com HTML and visible to any anonymous visitor. Not a user secret. Not a finding.
2. **`sk-gemini-onboarding-promo-header-tag-text-cross-fade`** — CSS class name in the same Meet HTML dumps. Not a key. Not a finding.
3. **`.env.example` files** at `src/_1_800_operator/agents/{designer,engineer,pm}/.env.example` (deleted in `0c22b42`, session 180). Diff shows the **entirety** of every version of those files was:
   ```
   ANTHROPIC_API_KEY=
   GITHUB_TOKEN=
   FIGMA_TOKEN=    # designer only
   ```
   Empty placeholders. No real values ever committed. Not a finding.
4. **`oauth_cache.py`** (deleted in `51b69f3`, session 206) — pure helper code for checking presence of mcp-remote OAuth cache files in `~/.mcp-auth/`. No tokens. Not a finding.
5. Zero hits on `gh[ps]_…`, `Bearer …`, `-----BEGIN …`, `sk-ant-…`, `sk-proj-…`, `sk-live-…`, `xox[bp]-…` patterns anywhere in history (excluding the CSS class noise above).
6. Zero `.p12`, `.pem`, `.key`, `.mobileprovision`, `.cer`, `credentials.json`, or `token.json` files ever committed.

## .gitignore audit

Currently covered:

- `.env`, `credentials.json`, `token.json`, `auth_state.json`, `browser_profile/` — defensive coverage for legacy user-scoped paths (the active equivalents now live under `~/.operator/`, outside the repo).
- Python build artifacts: `__pycache__/`, `*.py[cod]`, `*.egg-info/`, `.eggs/`, `dist/`, `build/`, `.venv/`, `venv/`.
- macOS noise: `.DS_Store`.
- Swift compiled helper: `src/_1_800_operator/swift/operator-audio-capture` (and per-machine Rust target dir).

Not covered (hygiene gaps — non-blocking for launch since none of these currently exist in the tree, but worth adding as future-leak insurance):

- **Apple signing material globs**: `*.p12`, `*.pem`, `*.key`, `*.cer`, `*.mobileprovision`, `*.certSigningRequest`. None currently in tree, but the build_signed_helper.sh workflow involves generating CSRs and downloading `.cer` files on the dev machine — a misclick `git add` could leak.
- **`slip_profile/`** glob — current naming the codebase uses (the gitignore has the older `browser_profile/`). Operator's slip profile lives in `~/.operator/slip_profile/` so this can only land via symlink-or-copy mistake, but a defensive entry costs nothing.
- **`Operator`** raw Mach-O at `src/_1_800_operator/swift/Operator` — currently untracked; gitignore handles the old `operator-audio-capture` name. Add `src/_1_800_operator/swift/Operator` to mirror.

## A5 Recommendations

No launch-blocking actions required.

Non-blocking hygiene (do whenever convenient):

1. Extend `.gitignore` with defensive globs:
   ```
   *.p12
   *.pem
   *.key
   *.cer
   *.mobileprovision
   *.certSigningRequest
   slip_profile/
   src/_1_800_operator/swift/Operator
   ```
2. The deleted `.app` bundle at `src/_1_800_operator/swift/operator-audio-capture.app/...` is currently shown as deleted in `git status` but still tracked in HEAD. A future commit will need to remove it from the index (`git rm`). No security implication — the contents (signing manifest plist, public cert chain in Mach-O) were never secret — but cleanliness for the public flip.

No rotations required. No history rewrite required.
