"""
Claude Code CLI LLM provider — interactive PTY-driven `claude` + hook events.

Phase 14.22 pivot (May 2026): replaces the previous per-@mention
`claude -p` shellouts. Background in `debug/14_22_pty_spike/DECISION.md`.

Trigger: starting 2026-06-15 Anthropic stops counting `claude -p` and
Agent SDK usage toward Claude subscription limits; both draw from a
small per-plan Agent SDK credit, then API rates. Operator's per-meeting
`claude -p --resume` flow burns through that bucket in days for any
non-trivial user. Interactive `claude` stays on the subscription pool —
the documented home for interactive usage.

How it works:
  - One long-lived `claude --dangerously-skip-permissions` subprocess
    per meeting, driven over a PTY (pty.openpty + os.setsid).
  - Input: bracketed-paste wrap + CR written to the PTY master.
    Universal — survives quotes, backslashes, multi-line, emoji, code
    fences (proven in spike_finalize.py T1, SHA-256 byte-for-byte).
    Char-by-char typing silently dropped chars on long messages.
  - Output: hook events. The operator-plugin's Stop hook appends each
    completed reply as JSONL to $OPERATOR_SESSION_DIR/replies.jsonl;
    PreToolUse → tools.jsonl; PostToolUseFailure / PermissionDenied /
    StopFailure → errors.jsonl. The provider tails these files for
    structured events — no screen scraping, no TUI parsing.

Why not Stop-block input (return decision=block from Stop hook to
inject next turn): claude's prompt-injection defense fires on it.
Spike_framing proved every hook-injected message gets refused as a
suspected prompt-injection attempt, even with a counter-instruction at
session start. Filtering "Stop hook feedback:" at an API proxy would
bypass an Anthropic safety feature — strategic non-starter.

Why bracketed-paste (and not `claude -p` over stdin): the new spawn
is interactive, so it owns the TTY and there is no `--input-format
stream-json` envelope path. Bracketed-paste is what a human pasting
into the TUI emits, and the TUI accepts it as a normal user turn.

cwd: inner-claude must spawn in `<user-project-dir>` because
`claude --resume` is cwd-scoped — the session JSONL lives under a
project dir derived from the creator's cwd, and `--resume <id>` from
the wrong cwd returns "No conversation found with session ID". No
`--working-directory` flag exists (probed via `claude --help`); the
process's actual cwd is the only knob. Side effect: the user's
project-level `.claude/settings.json` hooks fire inside meetings — same
as the prior `-p` behavior, not a regression.

Subscription auth: ANTHROPIC_API_KEY is stripped from the spawn env
unconditionally. There is no per-spawn apiKeySource assertion now
because the interactive TUI doesn't emit a system_init event we can
read — the equivalent guard is doctor's preflight (`claude auth
status --json`) and a SessionStart-hook anomaly check later.
"""
import fcntl
import json
import logging
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
import uuid
from pathlib import Path

from _1_800_operator.pipeline.providers.base import (
    LLMProvider,
    ProviderResponse,
    flush_paragraphs,
)

log = logging.getLogger(__name__)


# Bracketed-paste timings from spike_finalize.py — proven against the
# T1 tough-inputs sweep (quotes/backslash/multi-line/emoji/code fences,
# SHA-256 round-trip). Shortening any of these will eventually drop
# bytes on long messages; don't tune without re-running T1.
_BRACKET_OPEN_DELAY = 0.05
_BRACKET_BODY_DELAY = 0.1
_BRACKET_CLOSE_DELAY = 0.2

# PTY window — set on the master fd so claude's TUI lays out at a
# reasonable size rather than the default 80x24 that some renderers
# pin to. Cosmetic only; events come out via hooks regardless.
_PTY_ROWS = 40
_PTY_COLS = 120

# Spawn-ready handshake. Operator-plugin's SessionStart hook writes
# `ready.flag` into the session dir; the provider polls for it after
# spawn. The fallback settle is only hit when the plugin isn't installed
# — in that case the reply tail will time out anyway with a clearer
# error, so the settle just keeps the first send from racing the TUI.
_READY_FLAG_TIMEOUT_SECONDS = 30.0
_READY_FLAG_POLL_SECONDS = 0.1
_SPAWN_FALLBACK_SETTLE_SECONDS = 5.0

# Tail-loop polling cadence for replies.jsonl. 0.15s matches the spike
# and is short enough that p50 turn TTFR (Stop hook fires → reply
# posted) stays in the noise floor of the meeting-chat send path.
_REPLIES_POLL_SECONDS = 0.15


class ClaudeCLINotFoundError(RuntimeError):
    """Raised when the `claude` CLI is missing from PATH."""


class ClaudeCLIProtocolError(RuntimeError):
    """Inner-claude died, the PTY broke, or the reply tail timed out."""


def _set_winsize(fd, rows, cols):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _drain_pty_thread(master_fd, dump_buf, stop_event):
    """Drain master_fd into a rolling buffer for diagnostics on death.

    The TUI emits cursor-positioned bytes that aren't useful to parse —
    we capture the tail purely so a crashed-claude failure has something
    to surface in the error message. The reader runs until stop_event
    fires or read() raises OSError (PTY closed).
    """
    while not stop_event.is_set():
        try:
            r, _, _ = select.select([master_fd], [], [], 0.2)
        except (OSError, ValueError):
            return
        if not r:
            continue
        try:
            chunk = os.read(master_fd, 4096)
        except OSError:
            return
        if not chunk:
            return
        dump_buf.append(chunk)


class ClaudeCLIProvider(LLMProvider):
    """Long-lived interactive `claude` driven via PTY + hook events.

    One subprocess per meeting; `pre_warm` opens it, `complete_streaming`
    sends a turn and tails for the matching reply, `stop` tears it down.
    The hook-side scaffolding (operator-plugin's hooks/scripts/*.sh)
    must be installed in the user's Claude Code plugin list — without
    it, replies.jsonl never appears and turns time out.
    """

    def __init__(self, *, cwd=None, resume_session_id=None, session_dir=None):
        """
        Args:
          cwd: working dir for the inner-claude spawn. Defaults to the
            invoking process's cwd. The plugin slash command runs from
            the user's project dir, which is what we want for
            `--resume` to find the session JSONL.
          resume_session_id: optional Claude Code session id to bridge
            into the meeting. Passed as `--resume <id>` on spawn.
          session_dir: optional override for the per-session state dir
            (where the plugin hooks write replies.jsonl etc.). Defaults
            to a fresh ~/.operator/sessions/<uuid>/ created on
            construction; the env-var contract OPERATOR_SESSION_DIR is
            set here so the spawn inherits it.
        """
        self._cwd = cwd or os.getcwd()
        self._resume_session_id = resume_session_id

        if session_dir is None:
            session_dir = Path.home() / ".operator" / "sessions" / uuid.uuid4().hex
        self._session_dir = Path(session_dir)
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._replies_path = self._session_dir / "replies.jsonl"
        self._tools_path = self._session_dir / "tools.jsonl"
        self._errors_path = self._session_dir / "errors.jsonl"
        self._ready_flag_path = self._session_dir / "ready.flag"
        self._metadata_path = self._session_dir / "metadata.json"

        self._proc: subprocess.Popen | None = None
        self._master_fd: int | None = None
        self._pty_reader_stop = threading.Event()
        self._pty_reader_thread: threading.Thread | None = None
        self._pty_dump: list[bytes] = []

        self._spawn_lock = threading.Lock()
        self._spawn_in_progress = False
        self._stopping = False

        # Callback slots — preserved from the prior provider shape so
        # ChatRunner._wire_provider keeps working. progress/denial/
        # connection are no-ops in this commit; section G will wire
        # them off tools.jsonl + errors.jsonl + PTY EOF.
        self._progress_callback = None
        self._tick_callback = None
        self._denial_callback = None
        self._connection_callback = None

        # Captured `session_id` for archival. The Stop hook payload
        # includes `transcript_path` and `session_id`; we record the
        # first one we see so `metadata.json` carries it.
        self._captured_session_id: str | None = None

    # --- callback wiring (set by ChatRunner._wire_provider) -----------

    def set_progress_callback(self, callback):
        """Tool-use narrator. No-op until section G wires tools.jsonl tail."""
        self._progress_callback = callback

    def set_tick_callback(self, callback):
        """Per-iteration tick during the reply tail loop. Used by
        ChatRunner to drain its off-thread send queue while the
        polling thread is parked here. Signature: () -> None.
        """
        self._tick_callback = callback

    def set_denial_callback(self, callback):
        """Permission-denial narrator. No-op until section G wires errors.jsonl tail."""
        self._denial_callback = callback

    def set_connection_callback(self, callback):
        """Connection-status narrator. No-op until section G wires PTY EOF watch."""
        self._connection_callback = callback

    # --- lifecycle ----------------------------------------------------

    def stop(self):
        """Tear down the inner-claude PTY. Idempotent."""
        if self._stopping:
            return
        log.info("ClaudeCLI stop() called")
        self._stopping = True
        self._terminate_inner()

    def pre_warm(self):
        """Spawn the long-lived inner-claude subprocess.

        Called once per meeting (from __main__.py after the meeting
        join sequence). Idempotent: a second call while a spawn is
        in-flight returns; a call after the subprocess is already
        alive returns. Best-effort — failure logs and leaves the
        provider unspawned; the next complete() call will retry.
        """
        if self._stopping:
            return
        with self._spawn_lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            if self._spawn_in_progress:
                return
            self._spawn_in_progress = True
        try:
            self._spawn_inner()
        except Exception as exc:
            log.warning(f"ClaudeCLI: pre_warm spawn failed: {exc}")
        finally:
            with self._spawn_lock:
                self._spawn_in_progress = False

    # --- spawn --------------------------------------------------------

    def _build_cmd(self):
        claude = shutil.which("claude")
        if not claude:
            raise ClaudeCLINotFoundError(
                "`claude` CLI not found on PATH. Install it from "
                "https://docs.anthropic.com/en/docs/claude-code and ensure it is "
                "logged in (`claude auth status`)."
            )
        # --dangerously-skip-permissions is unconditional now. Operator
        # has no permission layer of its own and the meeting flow needs
        # tools to run without per-call prompts. The user-facing `--yolo`
        # flag becomes a no-op (kept in argv parsing for back-compat
        # with the plugin slash command; nothing reads it here anymore).
        cmd = [claude, "--dangerously-skip-permissions"]
        if self._resume_session_id:
            cmd += ["--resume", self._resume_session_id]
        return cmd

    def _spawn_inner(self):
        """Open the PTY, fork claude into it, start the drain thread."""
        cmd = self._build_cmd()
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        # Load-bearing: hook scripts read this to know where to write.
        # Setting it here as well as in __main__.py is belt-and-suspenders
        # — provider construction is the authoritative place because the
        # session dir is owned here.
        env["OPERATOR_SESSION_DIR"] = str(self._session_dir)

        master_fd, slave_fd = pty.openpty()
        _set_winsize(master_fd, _PTY_ROWS, _PTY_COLS)

        log.info(
            f"ClaudeCLI spawning interactive claude: cwd={self._cwd} "
            f"session_dir={self._session_dir} "
            f"resume={'<bridged>' if self._resume_session_id else 'none'}"
        )
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=self._cwd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                preexec_fn=os.setsid,
                env=env,
                close_fds=True,
            )
        except OSError as exc:
            os.close(master_fd)
            os.close(slave_fd)
            raise ClaudeCLIProtocolError(f"failed to launch claude CLI: {exc}") from exc

        os.close(slave_fd)

        self._proc = proc
        self._master_fd = master_fd
        self._pty_reader_stop = threading.Event()
        self._pty_dump = []
        self._pty_reader_thread = threading.Thread(
            target=_drain_pty_thread,
            args=(master_fd, self._pty_dump, self._pty_reader_stop),
            daemon=True,
        )
        self._pty_reader_thread.start()

        # Record metadata up-front so it survives a crash before any
        # reply lands.
        try:
            self._metadata_path.write_text(
                json.dumps({
                    "started_at": time.time(),
                    "cwd": self._cwd,
                    "resume_session_id": self._resume_session_id,
                    "pid": proc.pid,
                }),
                encoding="utf-8",
            )
        except OSError:
            pass

        self._wait_for_ready()
        log.info(f"ClaudeCLI: inner-claude live (pid={proc.pid})")

    def _wait_for_ready(self):
        """Block until the SessionStart hook writes ready.flag, with timeout.

        Falls back to a hardcoded settle if the flag never appears, which
        almost always means operator-plugin isn't installed in the user's
        Claude Code. The fallback at least lets the TUI initialize before
        the first paste — the actual failure surfaces clearly when the
        Stop hook tail times out on the first turn.
        """
        deadline = time.monotonic() + _READY_FLAG_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._ready_flag_path.exists():
                return
            if self._proc is not None and self._proc.poll() is not None:
                raise ClaudeCLIProtocolError(
                    f"inner-claude exited during startup (rc={self._proc.returncode}).\n"
                    f"PTY tail:\n{self._pty_tail()}"
                )
            time.sleep(_READY_FLAG_POLL_SECONDS)
        log.warning(
            f"ClaudeCLI: ready.flag never appeared after "
            f"{_READY_FLAG_TIMEOUT_SECONDS:.0f}s — operator-plugin likely not "
            f"installed. Falling back to time-based settle; expect the first "
            f"turn to time out with a clearer hook-side error."
        )
        time.sleep(_SPAWN_FALLBACK_SETTLE_SECONDS)

    def _terminate_inner(self):
        """Best-effort tear-down. Idempotent."""
        proc = self._proc
        master_fd = self._master_fd

        if self._pty_reader_thread is not None:
            self._pty_reader_stop.set()
            self._pty_reader_thread.join(timeout=2)
            self._pty_reader_thread = None

        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass

        if master_fd is not None:
            try:
                os.close(master_fd)
            except OSError:
                pass

        # Stamp ended_at into metadata for archival.
        try:
            if self._metadata_path.exists():
                meta = json.loads(self._metadata_path.read_text(encoding="utf-8"))
                meta["ended_at"] = time.time()
                if self._captured_session_id:
                    meta["session_id"] = self._captured_session_id
                self._metadata_path.write_text(json.dumps(meta), encoding="utf-8")
        except (OSError, ValueError):
            pass

        self._proc = None
        self._master_fd = None

    def _pty_tail(self, n_bytes=2000):
        """Return the last n_bytes of captured PTY output as a string."""
        joined = b"".join(self._pty_dump)
        tail = joined[-n_bytes:]
        try:
            return tail.decode("utf-8", errors="replace")
        except Exception:
            return "<undecodable>"

    # --- callback firing helpers (tick is the only live one for now) --

    def _fire_tick(self):
        cb = self._tick_callback
        if cb is None:
            return
        try:
            cb()
        except Exception as e:
            log.warning(f"ClaudeCLI: tick callback raised: {e}")

    # --- send + tail --------------------------------------------------

    def _send_message(self, msg):
        """Bracketed-paste wrap + CR. Universal input strategy from
        spike_finalize.py T1.
        """
        if self._master_fd is None:
            raise ClaudeCLIProtocolError(
                "inner-claude is not running; pre_warm or complete() must spawn first"
            )
        payload = msg.encode("utf-8")
        try:
            os.write(self._master_fd, b"\x1b[200~")
            time.sleep(_BRACKET_OPEN_DELAY)
            os.write(self._master_fd, payload)
            time.sleep(_BRACKET_BODY_DELAY)
            os.write(self._master_fd, b"\x1b[201~")
            time.sleep(_BRACKET_CLOSE_DELAY)
            os.write(self._master_fd, b"\r")
        except OSError as exc:
            raise ClaudeCLIProtocolError(
                f"PTY write failed: {exc}\nPTY tail:\n{self._pty_tail()}"
            ) from exc

    def _count_replies(self):
        """Count completed reply rows in replies.jsonl. 0 if the file
        doesn't exist yet — the plugin hooks may not have fired.
        """
        try:
            with self._replies_path.open("rb") as f:
                return sum(1 for _ in f)
        except FileNotFoundError:
            return 0
        except OSError:
            return 0

    def _read_reply_at(self, index):
        """Read the JSON object at line `index` of replies.jsonl.

        Returns None if the line is unreadable or doesn't parse — the
        hook script may still be mid-flush, but our caller has already
        confirmed line count grew, so it should be valid by now.
        """
        try:
            with self._replies_path.open("r", encoding="utf-8") as f:
                for i, line in enumerate(f):
                    if i == index:
                        return json.loads(line)
        except (OSError, json.JSONDecodeError):
            return None
        return None

    def _wait_for_next_reply(self, prev_count, timeout):
        """Tail replies.jsonl until count > prev_count or timeout.

        Returns the parsed reply object (the Stop hook's payload), or
        None on timeout. Fires the tick callback on every poll so
        ChatRunner can drain its off-thread send queue.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._fire_tick()
            if self._proc is not None and self._proc.poll() is not None:
                raise ClaudeCLIProtocolError(
                    f"inner-claude exited unexpectedly (rc={self._proc.returncode}).\n"
                    f"PTY tail:\n{self._pty_tail()}"
                )
            current = self._count_replies()
            if current > prev_count:
                reply = self._read_reply_at(prev_count)
                if reply is not None:
                    return reply
            time.sleep(_REPLIES_POLL_SECONDS)
        return None

    @staticmethod
    def _extract_assistant_text(reply):
        """Pull `last_assistant_message` from a Stop hook payload.

        Hook payload shape (from spike_finalize bench scripts):
            {"ts": <float>, "kind": "stop", "input": {
                "hook_event_name": "Stop",
                "last_assistant_message": "<final text>",
                "session_id": "<id>",
                "transcript_path": "<path>",
                ...
            }}

        The wrapping `{ts, kind, input: ...}` shape is the operator-plugin
        script's convention; Claude Code's actual hook payload is the
        `input` sub-object. We tolerate both shapes so plugin-script
        changes don't break the provider.
        """
        if not isinstance(reply, dict):
            return None
        inner = reply.get("input") if isinstance(reply.get("input"), dict) else reply
        text = inner.get("last_assistant_message")
        if isinstance(text, str):
            return text
        return None

    @staticmethod
    def _extract_session_id(reply):
        if not isinstance(reply, dict):
            return None
        inner = reply.get("input") if isinstance(reply.get("input"), dict) else reply
        sid = inner.get("session_id")
        return sid if isinstance(sid, str) else None

    # --- LLMProvider interface ----------------------------------------

    def complete(self, system, messages, model, max_tokens, tools=None, retry_rate_limits=True):
        """Send the latest user turn, wait for Stop hook, return reply.

        Ignored args (system / model / max_tokens / tools): inner-claude
        owns its own system prompt, model selection, and tool loop
        natively. Only the latest user turn is forwarded — claude has
        its own conversation memory.
        """
        return self._run_turn(messages, on_paragraph=None)

    def complete_streaming(
        self, system, messages, model, max_tokens, tools=None, on_paragraph=None,
        retry_rate_limits=True,
    ):
        """Same as complete(), but splits the reply into paragraphs.

        Known regression vs. the prior `claude -p` shape: paragraphs
        flush only once the Stop hook fires (i.e. at end-of-turn).
        Mid-generation streaming is gone because hooks are end-of-event
        only — the TUI does emit partial text, but parsing the
        positional TUI bytes for partials is the screen-scraping path
        we explicitly chose not to ship (DECISION.md "Why send-keys +
        hooks"). Users see paragraph-by-paragraph chat messages
        delivered as a batch instead of as a drip-feed.
        """
        if on_paragraph is None:
            return self.complete(system, messages, model, max_tokens, tools=tools)
        return self._run_turn(messages, on_paragraph=on_paragraph)

    def warmup(self, model):
        """No-op. pre_warm() is the meaningful warmup for this provider."""
        return None

    def _run_turn(self, messages, on_paragraph):
        if self._stopping:
            raise ClaudeCLIProtocolError("provider is stopping")
        if not messages:
            raise ValueError("complete() requires at least one message")
        last = messages[-1]
        if last.get("role") != "user":
            raise ValueError(
                f"claude_cli expects the last message to be a user turn; "
                f"got role={last.get('role')!r}"
            )
        user_text = last.get("content") or ""

        # Lazy spawn: pre_warm should already have run from __main__,
        # but a slow join or a missed pre_warm shouldn't break the turn.
        if self._proc is None or self._proc.poll() is not None:
            self.pre_warm()
            if self._proc is None:
                raise ClaudeCLIProtocolError(
                    "inner-claude failed to spawn; check operator-plugin install"
                )

        t_start = time.monotonic()
        prev_count = self._count_replies()
        self._send_message(user_text)

        # Generous per-turn timeout — claude tool loops can run minutes
        # legitimately. The user cancels via /operator:hangup if a tool
        # chain wedges; no operator-imposed deadline.
        reply = self._wait_for_next_reply(prev_count, timeout=600.0)
        elapsed = time.monotonic() - t_start

        if reply is None:
            raise ClaudeCLIProtocolError(
                f"timed out after {elapsed:.0f}s waiting for Stop hook reply.\n"
                "Likely cause: operator-plugin's hooks are not installed (no "
                "replies.jsonl was written). Verify with `ls "
                f"{self._session_dir}`.\nPTY tail:\n{self._pty_tail()}"
            )

        text = self._extract_assistant_text(reply)
        sid = self._extract_session_id(reply)
        if sid and not self._captured_session_id:
            self._captured_session_id = sid

        log.info(
            f"TIMING claude_cli_turn={elapsed:.1f}s "
            f"reply_chars={len(text) if text else 0} "
            f"session={sid or '?'}"
        )

        if on_paragraph is not None and text:
            # Batch-flush paragraphs through the existing splitter so
            # the chat layer sees the same shape it did with streaming.
            flush_paragraphs(text, on_paragraph, force_final=True)

        return ProviderResponse(
            text=text or None,
            tool_calls=[],
            stop_reason="end",
        )
