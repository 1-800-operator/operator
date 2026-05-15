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
  - Output, two channels:
      * The operator-plugin's Stop hook appends each completed turn as
        JSONL to $OPERATOR_SESSION_DIR/replies.jsonl. The provider tails
        that file purely as the turn-boundary signal — a new row means
        "the turn is done."
      * The actual reply *text* comes from the Claude Code transcript
        JSONL (transcript_path, captured from turn 0's Stop payload).
        During a turn the provider tails the transcript and posts each
        assistant text block the moment it lands — so Claude's
        self-narration ("let me grab that file") reaches meeting chat in
        real time instead of being batched at end-of-turn. No screen
        scraping, no TUI parsing. (The plugin also writes tools.jsonl /
        errors.jsonl; the provider no longer reads them — Phase 14.22
        section G's operator-side tool narration was dropped in favour
        of Claude self-narrating, briefed via the first paste.)

Briefing: the first bracketed-paste operator sends is an
operator-authored context message (see _BRIEFING) telling inner-claude
it's in a live meeting and to narrate its tool calls in its own voice.
This rides the same channel a human types on, so it does NOT change the
spawn signature — the naked-spawn invariant constrains spawn *flags*
(no -p, no --append-system-prompt, no --mcp-config), not the message
stream. Turn 0's reply is consumed by _send_briefing and never posted.

Why not Stop-block input (return decision=block from Stop hook to
inject next turn): claude's prompt-injection defense fires on it.
Spike_framing proved every hook-injected message gets refused as a
suspected prompt-injection attempt, even with a counter-instruction at
session start. Filtering "Stop hook feedback:" at an API proxy would
bypass an Anthropic safety feature — strategic non-starter. (Note: a
*first user-turn paste* is not hook-injected and is not refused — the
prompt-injection defense is specific to Stop-block feedback.)

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

# Boot ceiling — ONE hard ceiling across the whole boot: spawn →
# ready.flag (SessionStart hook) → briefing (turn 0) consumed. A healthy
# boot is fast: ready.flag lands in well under a second (instrumented:
# 0.37s fresh, 0.37s resuming a 13 MB session — SessionStart fires
# before MCP connects), and the briefing round-trip is a few seconds
# more. 180s is generous enough that a slow-but-healthy boot is never
# false-flagged, while bounding the wait: past 3 minutes, claude is
# wedged on something operator can't see (a huge resume, a hung MCP
# attach, an interactive prompt) and isn't worth waiting on. There is NO
# blind-settle fallback — either boot completes, the process dies / the
# PTY hits EOF (fail now), or the ceiling is hit (fail). On any failure
# pre_warm kills the proc, so "alive but wedged" never persists.
_BOOT_CEILING_SECONDS = 180.0
_READY_FLAG_POLL_SECONDS = 0.1
# One-time internal log breadcrumb if ready.flag is slower than a healthy
# boot — no behaviour change, just a forensic marker so the next slow
# boot leaves a trail (the 95s boot that motivated this was unreproducible).
_READY_FLAG_SLOW_WARN_SECONDS = 15.0

# Structural "blocked on an interactive prompt" signal. A booting claude
# reaches ready.flag in under a second; if it instead renders terminal
# output and then goes SILENT with the flag still absent, it has stopped
# emitting and is WAITING — almost always on an interactive prompt (the
# workspace-trust dialog, an onboarding step, a future Anthropic prompt).
# The signal is structural — it doesn't care which prompt — so it
# generalises to prompts that don't exist yet. This is the quiet window
# that marks "stopped emitting, now waiting."
_PTY_QUIET_BLOCKED_SECONDS = 5.0

# Soft text-heuristic needles for ENRICHING a stuck-boot diagnosis — never
# load-bearing (the idle TUI input box itself renders prompt-ish glyphs,
# so text matching false-positives if trusted). The raw PTY stream
# positions words with cursor-move escapes instead of spaces, so the
# matcher strips ALL ANSI and ALL whitespace first — hence these needles
# are space-free. Generic prompt affordances first; the workspace-trust
# label is an optional nicety on top. We classify and report — never
# parse-to-answer.
_PROMPT_AFFORDANCE_NEEDLES = (
    "entertoconfirm", "esctocancel", "esctoexit",
    "(y/n)", "[y/n]", "(yes/no)", "pressenter",
)
_TRUST_DIALOG_NEEDLES = ("trustthisfolder", "doyoutrust", "isthisaproject")

# Tail-loop polling cadence for replies.jsonl. 0.15s matches the spike
# and is short enough that p50 turn TTFR (Stop hook fires → reply
# posted) stays in the noise floor of the meeting-chat send path. The
# same cadence drives the in-turn transcript tail (real-time narration).
_REPLIES_POLL_SECONDS = 0.15

# After the Stop hook fires, the turn's final assistant block may still
# be a write-beat behind in the transcript JSONL. Settle this long, then
# do one last transcript drain before closing the turn.
_TRANSCRIPT_FINAL_DRAIN_SETTLE = 0.3

# A foreign Stop hook (the user's own ~/.claude or project
# settings.json) can run decision=block and inject "Stop hook
# feedback: …", redirecting inner-claude mid-meeting. DECISION.md
# section I: observable, not preventable — operator spawns in the
# user's project dir for --resume, so that dir's hooks fire inside
# meetings, and operator won't mutate the user's config to stop them.
# The detection is surfaced to chat; the turn-end delay below is a
# noisier proxy signal — logged only. If the gap between the final
# assistant block landing and the Stop row appearing exceeds this,
# foreign hooks may have run in between.
_FOREIGN_HOOK_DELAY_WARN_SECONDS = 5.0

# Operator's briefing — the first bracketed-paste sent to a freshly
# spawned inner-claude, before any real meeting turn. Operator-authored
# context, deliberately conversational (not a rigid preamble): it rides
# the same input channel a human types on, so it carries no spawn-
# signature weight. See the module docstring on the naked-spawn
# invariant. _send_briefing consumes turn 0's reply so this never
# reaches meeting chat.
_BRIEFING = """Quick context before we start: you're in a live Google Meet right now, and whatever you say goes straight into the meeting chat for everyone on the call to read. So keep replies short and conversational — chat-length, not essay-length.

Before you run a tool — reading a file, searching, running a command — say what you're about to do in a quick line, like "let me grab that file" or "searching for it now." Then post the result when you have it. The point is the room sees what you're doing instead of staring at a blank screen while a tool runs.

Don't use any tool that pops up a UI for the user to click — specifically AskUserQuestion and plan mode (EnterPlanMode / ExitPlanMode). Both will hang the meeting because participants can't click anything here. If you need to ask something, type it as a normal chat message and wait for a reply. If you'd normally use plan mode, just write the plan inline as chat text instead.

Don't reply to this message — it's just setup. Wait for the first real message from the meeting."""

# Appended to _BRIEFING when guarded mode is on. Operator's
# PermissionRequest hook will bridge each ask into meeting chat;
# claude should know to expect that and keep its tool calls clean.
_BRIEFING_GUARDED_SUFFIX = """

One more thing for this meeting: every tool call you make that the user hasn't pre-approved will pop up in chat as a yes/no question to the room, and the meeting waits while a participant answers. Keep your tool calls focused and easy to evaluate at a glance — that makes the question easy for someone to answer fast. If you get denied, that's the user's call; just narrate the denial and move on."""


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
    fires or the PTY closes (read() hits EOF or raises OSError).
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

    def __init__(self, *, cwd=None, resume_session_id=None, session_dir=None, guarded=False):
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
        # When True, spawn with `--permission-mode default` instead of
        # `--dangerously-skip-permissions` and append a guarded-mode
        # line to the briefing so claude knows the room will be asked
        # to approve uncategorised tool calls. The operator-plugin's
        # PermissionRequest hook fires under this mode (it's inert
        # under bypass) and ChatRunner's PermissionClassifier sidecar
        # interprets each chat reply.
        self._guarded = bool(guarded)

        if session_dir is None:
            session_dir = Path.home() / ".operator" / "sessions" / uuid.uuid4().hex
        self._session_dir = Path(session_dir)
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._replies_path = self._session_dir / "replies.jsonl"
        self._ready_flag_path = self._session_dir / "ready.flag"
        self._metadata_path = self._session_dir / "metadata.json"

        self._proc: subprocess.Popen | None = None
        self._master_fd: int | None = None
        self._pty_reader_stop = threading.Event()
        self._pty_reader_thread: threading.Thread | None = None
        self._pty_dump: list[bytes] = []

        self._spawn_lock = threading.Lock()
        self._spawn_in_progress = False
        self._spawn_exc: Exception | None = None
        self._stopping = False

        # Boot-complete signal. pre_warm runs on a background thread
        # (kicked off by __main__ during the meeting-join window) and sets
        # _proc *early* — when Popen returns, before _wait_for_ready and
        # the briefing — so "_proc is alive" does NOT mean "ready for a
        # turn". _boot_done is set only when pre_warm has fully finished,
        # success or failure (paired with _spawn_exc, which says which).
        # _run_turn gates on it so an @mention that races the boot waits
        # for readiness instead of pasting into a half-booted TUI.
        self._boot_done = threading.Event()

        # Per-meeting "claude is dead and a retry already failed" latch.
        # Set in _run_turn when an attempt fails AND its one retry also
        # fails; once latched, every further turn short-circuits. A
        # successful turn never sets it, so it only ever latches on a
        # genuine give-up. The meeting recovers via /operator:hangup +
        # rejoin, not by accumulating retries.
        self._unavailable = False

        # Best-effort phase tag for the post-failure snapshot doctor
        # reads: "boot" if the failure latched while inner-claude was
        # still starting up, "turn" if it latched mid-turn. Set when
        # _run_turn latches _unavailable; consumed by
        # snapshot_failure_context().
        self._last_failure_phase: str | None = None

        # Shared boot deadline — the whole boot (spawn → ready.flag →
        # briefing consumed) races one ceiling. _spawn_inner re-stamps it
        # at the real boot start; initialised here so _wait_for_ready can
        # also be exercised standalone.
        self._boot_deadline = time.monotonic() + _BOOT_CEILING_SECONDS

        # Tick callback: ChatRunner drains its off-thread send queue on
        # every reply-tail poll. The progress/denial/connection callbacks
        # the prior provider shape carried are gone — Claude self-narrates
        # its tool calls now (briefed via the first paste), so operator
        # no longer tails tools.jsonl / errors.jsonl.
        self._tick_callback = None

        # PermissionRequest callback (yolo-off mode). Fires when the
        # operator-plugin permission_request.sh hook writes a new line
        # to permreq_requests.jsonl mid-turn — ChatRunner posts the
        # question to meeting chat, watches for a yes/no reply, and
        # atomically writes the answer file the hook is polling. None
        # in yolo-on (the hook is inert under --dangerously-skip-permissions
        # because PermissionRequest never fires under bypass mode).
        # Offset/buf are reset per-spawn in _spawn_inner so a respawn
        # does not re-fire stale requests from before the crash.
        self._permreq_callback = None
        self._permreq_offset = 0
        self._permreq_buf = b""

        # Captured `session_id` for archival. The Stop hook payload
        # includes `transcript_path` and `session_id`; we record the
        # first one we see so `metadata.json` carries it.
        self._captured_session_id: str | None = None

        # Claude Code transcript JSONL for the live session — captured
        # from turn 0's Stop payload in _send_briefing. Real turns tail
        # this file for real-time assistant-text narration. None until
        # the briefing reply lands (or a real turn backfills it).
        self._transcript_path: Path | None = None

    # --- callback wiring (set by ChatRunner._wire_provider) -----------

    def set_tick_callback(self, callback):
        """Per-iteration tick during the reply tail loop. Used by
        ChatRunner to drain its off-thread send queue while the
        polling thread is parked here. Signature: () -> None.
        """
        self._tick_callback = callback

    def set_permission_request_callback(self, callback):
        """Called when a new PermissionRequest from the operator-plugin
        hook lands in permreq_requests.jsonl mid-turn. Fired on the
        same thread as the tick callback (the polling thread, which is
        operator's main Playwright-owning thread), so the callback may
        call `connector.send_chat` directly.

        Signature:
            cb({
                "request_id": str,
                "ts": float,
                "tool_name": str | None,
                "tool_input": dict | None,
                "answer_path": Path,
            }) -> None

        Set to None to disable (yolo-on never invokes this; the hook
        does not fire under --dangerously-skip-permissions anyway).
        """
        self._permreq_callback = callback

    def snapshot_failure_context(self) -> dict:
        """Provider-side pieces of the post-failure snapshot.

        ChatRunner calls this when it's about to announce "claude is
        unavailable" and writes the combined record to
        config.LAST_FAILURE_PATH for doctor to read. The provider
        contributes what only it knows: which boot phase failed and the
        tail of inner-claude's PTY (useful when a startup crash printed
        an error to the terminal before exiting).
        """
        return {
            "phase": self._last_failure_phase or "unknown",
            "pty_tail": self._pty_tail(),
        }

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
            # Committing to a (re)spawn — boot is no longer "done" until
            # this run finishes. Cleared here, set in the finally below,
            # so _run_turn's gate sees an accurate signal across respawns.
            # _spawn_exc is cleared too: a fresh spawn makes the prior
            # run's failure moot (a retry must not surface a stale exc).
            self._boot_done.clear()
            self._spawn_exc = None
        try:
            self._spawn_inner()
            self._spawn_exc = None
        except Exception as exc:
            log.warning(f"ClaudeCLI: pre_warm spawn failed: {exc}")
            self._spawn_exc = exc
            # Kill the proc on any boot failure so it can't linger
            # "alive but wedged" — after this, a failed boot always
            # leaves _proc dead, the one state _run_turn reasons about.
            self._terminate_inner()
        finally:
            with self._spawn_lock:
                self._spawn_in_progress = False
                self._boot_done.set()

    # --- spawn --------------------------------------------------------

    def _build_cmd(self):
        claude = shutil.which("claude")
        if not claude:
            raise ClaudeCLINotFoundError(
                "`claude` CLI not found on PATH. Install it from "
                "https://docs.anthropic.com/en/docs/claude-code and ensure it is "
                "logged in (`claude auth status`)."
            )
        # Spawn permission mode is the only operator-controlled
        # difference between yolo-on (default — every tool runs without
        # asking) and yolo-off (`/operator:slip-guarded`, runs with
        # Claude Code's normal permission rules and the operator-plugin
        # PermissionRequest hook bridges each ask to meeting chat).
        #
        # `--permission-mode default` is explicit so a user-side
        # `permissions.defaultMode: "bypassPermissions"` in their
        # ~/.claude/settings.json can't silently flip guarded mode back
        # into yolo without us noticing.
        if self._guarded:
            cmd = [claude, "--permission-mode", "default"]
        else:
            cmd = [claude, "--dangerously-skip-permissions"]
        if self._resume_session_id:
            cmd += ["--resume", self._resume_session_id]
        return cmd

    def _spawn_inner(self):
        """Open the PTY, fork claude into it, start the drain thread."""
        cmd = self._build_cmd()

        # Clear a stale ready.flag before spawning. The flag is written
        # once per claude process by the plugin's SessionStart hook; on a
        # respawn (inner-claude crashed, next turn relaunches it) the
        # previous process's flag is still on disk, so _wait_for_ready
        # would return instantly and the first bracketed-paste would race
        # the new TUI's startup and be lost. Deleting it here forces
        # _wait_for_ready to block for the *new* hook.
        try:
            self._ready_flag_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning(f"ClaudeCLI: could not clear stale ready.flag: {exc}")

        # Reset the permreq tail to start of the (current) end of file —
        # on a respawn after a crash, any unanswered requests in
        # permreq_requests.jsonl are stale (the prior hook either got an
        # answer or self-denied at its own 120s timeout). Skipping past
        # them prevents stale-request callbacks fire-storming ChatRunner.
        try:
            self._permreq_offset = (
                self._session_dir / "permreq_requests.jsonl"
            ).stat().st_size
        except (FileNotFoundError, OSError):
            self._permreq_offset = 0
        self._permreq_buf = b""

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
            # start_new_session=True is the in-process default via the
            # Popen monkey-patch in __main__.py, which also satisfies our
            # need for the child to be its own process group (so killpg
            # in _terminate_inner only hits inner-claude, not us). We
            # MUST NOT also pass preexec_fn=os.setsid here — that calls
            # setsid in the child a second time and fails with EPERM
            # because the child is already a session leader.
            proc = subprocess.Popen(
                cmd,
                cwd=self._cwd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
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

        # One shared deadline across the whole boot — ready.flag AND the
        # briefing round-trip both race the same _BOOT_CEILING_SECONDS.
        self._boot_deadline = time.monotonic() + _BOOT_CEILING_SECONDS
        self._wait_for_ready()
        log.info(f"ClaudeCLI: inner-claude live (pid={proc.pid})")
        self._send_briefing()

    def _wait_for_ready(self):
        """Block until the SessionStart hook writes ready.flag.

        Three terminal outcomes, no fourth "settle and hope" path:
          - the flag appears              → record boot timing, return (ready)
          - the process dies / PTY EOFs   → raise (inner-claude is gone)
          - the hard ceiling is hit       → raise (inner-claude is hung)

        Raising propagates to pre_warm, which records it on `_spawn_exc`
        WITHOUT posting anything to chat — the never-post-unprompted
        invariant. The held failure surfaces only if a participant later
        @mentions claude (handled by _run_turn). If no @mention ever
        comes, operator stays silent and just leaves on auto-leave.
        """
        started = time.monotonic()
        # Shared boot deadline — ready.flag and the briefing race the
        # same ceiling (set in _spawn_inner).
        deadline = self._boot_deadline
        slow_warned = False
        # PTY-activity tracking for the structural stuck-boot signal.
        # The drain thread appends one entry per chunk, so chunk-count
        # growth == new output arrived — an O(1) check per poll. "Output
        # then sustained silence" is the generic "blocked on a prompt"
        # signature; see _diagnose_stuck_boot.
        pty_chunks_seen = 0
        pty_last_change = started
        while time.monotonic() < deadline:
            if self._ready_flag_path.exists():
                self._record_ready(started)
                return
            # Hard signals that inner-claude is gone — fail now, don't
            # wait out the ceiling.
            if self._proc is not None and self._proc.poll() is not None:
                raise ClaudeCLIProtocolError(
                    f"inner-claude exited during startup (rc={self._proc.returncode}).\n"
                    f"PTY tail:\n{self._pty_tail()}"
                )
            # The PTY drain thread only exits on EOF / OSError on the
            # master fd while we're in startup (stop_event isn't set
            # until _terminate_inner). A dead drain thread therefore
            # means the PTY closed under us — inner-claude is gone even
            # if poll() hasn't caught up yet.
            if (
                self._pty_reader_thread is not None
                and not self._pty_reader_thread.is_alive()
            ):
                raise ClaudeCLIProtocolError(
                    "inner-claude's PTY hit EOF during startup "
                    "(the process closed its tty).\n"
                    f"PTY tail:\n{self._pty_tail()}"
                )
            if len(self._pty_dump) != pty_chunks_seen:
                pty_chunks_seen = len(self._pty_dump)
                pty_last_change = time.monotonic()
            elapsed = time.monotonic() - started
            if not slow_warned and elapsed >= _READY_FLAG_SLOW_WARN_SECONDS:
                slow_warned = True
                log.warning(
                    f"ClaudeCLI: ready.flag not yet seen after "
                    f"{elapsed:.0f}s — slower than a healthy boot "
                    f"(~0.4s). Still waiting (boot ceiling "
                    f"{_BOOT_CEILING_SECONDS:.0f}s)."
                )
            time.sleep(_READY_FLAG_POLL_SECONDS)
        # Ceiling hit, process still alive — classify why, structurally.
        had_output = pty_chunks_seen > 0
        quiet_secs = (time.monotonic() - pty_last_change) if had_output else None
        diagnosis = self._diagnose_stuck_boot(
            had_output=had_output, quiet_secs=quiet_secs
        )
        raise ClaudeCLIProtocolError(
            f"inner-claude never became ready within the "
            f"{_BOOT_CEILING_SECONDS:.0f}s boot ceiling — {diagnosis}.\n"
            f"PTY tail:\n{self._pty_tail()}"
        )

    def _record_ready(self, started):
        """ready.flag appeared — log boot timing and capture what the
        SessionStart payload carries.

        Best-effort: the file's *existence* is the readiness signal; its
        JSON content is enrichment. A parse failure (an older plugin
        writes an empty ready.flag; a fallback path wrote one) just costs
        the forensic detail, never the boot. The TIMING line lands on
        EVERY run, so the next slow/cold boot leaves a trail with no
        special instrumentation — which is the whole point of giving the
        flag a payload (the original 95s boot was unreproducible because
        nothing recorded it).
        """
        lag = time.monotonic() - started
        payload = {}
        try:
            raw = self._ready_flag_path.read_text(encoding="utf-8")
            parsed = json.loads(raw) if raw.strip() else {}
            if isinstance(parsed, dict):
                payload = parsed
        except (OSError, ValueError):
            pass
        log.info(
            f"TIMING ClaudeCLI boot_to_ready={lag:.2f}s "
            f"source={payload.get('source', '?')} "
            f"session={payload.get('session_id', '?')}"
        )
        # The SessionStart payload carries transcript_path + session_id —
        # capture them now so real turns get the transcript tail without
        # waiting for turn 0's Stop payload to backfill them. The existing
        # backfill in _send_briefing / _run_turn stays as the fallback for
        # an older plugin that writes an empty ready.flag.
        tp = payload.get("transcript_path")
        if isinstance(tp, str) and tp and self._transcript_path is None:
            self._transcript_path = Path(tp)
        sid = payload.get("session_id")
        if isinstance(sid, str) and sid and not self._captured_session_id:
            self._captured_session_id = sid

    def _diagnose_stuck_boot(self, *, had_output, quiet_secs):
        """Classify why _wait_for_ready is about to fail, structurally.

        The load-bearing signal is structural and text-free: inner-claude
        is alive, ready.flag never came, and (the strong case) it
        produced terminal output and then went quiet — it rendered
        something and is now WAITING. That pattern means "blocked on an
        interactive prompt" whatever the prompt is, so it generalises to
        prompts Anthropic hasn't shipped yet.

        A soft text heuristic over the PTY tail only *enriches* the
        message ("looks like a y/n prompt", maybe "looks like the
        workspace-trust dialog"). It is unreliable by nature — the idle
        TUI input box itself renders prompt-ish glyphs — so it never
        gates behaviour, only decorates the report. We classify and
        report; we never parse-to-answer (that would be both
        screen-scraping and programmatically defeating a security
        prompt — see project_anthropic_detection_vector).
        """
        blocked = (
            had_output
            and quiet_secs is not None
            and quiet_secs >= _PTY_QUIET_BLOCKED_SECONDS
        )
        if blocked:
            msg = (
                f"inner-claude rendered terminal output then went silent "
                f"for {quiet_secs:.0f}s with ready.flag still absent — it "
                f"appears blocked on an interactive prompt during startup"
            )
        elif not had_output:
            msg = (
                "inner-claude produced no terminal output at all — it may "
                "have failed to start, or operator-plugin's SessionStart "
                "hook is not firing"
            )
        else:
            msg = (
                "inner-claude kept producing output but never signaled "
                "ready — an unusually slow or looping startup"
            )
        # Soft enrichment — best-effort, never load-bearing. The raw PTY
        # stream positions words with cursor-move escapes instead of
        # spaces, so strip ALL ANSI and ALL whitespace before matching
        # the (space-free) needles.
        compact = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", self._pty_tail()).lower()
        compact = re.sub(r"\s+", "", compact)
        if any(n in compact for n in _TRUST_DIALOG_NEEDLES):
            msg += (
                " — the PTY tail looks like Claude Code's workspace-trust "
                "dialog; open this folder in Claude Code directly and "
                "accept it once"
            )
        elif any(n in compact for n in _PROMPT_AFFORDANCE_NEEDLES):
            msg += " — the PTY tail looks like an interactive y/n or selection prompt"
        return msg

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

    # --- callback firing helpers -------------------------------------

    def _fire_tick(self):
        cb = self._tick_callback
        if cb is None:
            return
        try:
            cb()
        except Exception as e:
            log.warning(f"ClaudeCLI: tick callback raised: {e}")

    # --- briefing -----------------------------------------------------

    def _send_briefing(self):
        """Send the operator briefing (turn 0), consume its reply, and
        capture the transcript path.

        The briefing tells inner-claude it's in a live meeting and to
        narrate its tool calls — see _BRIEFING and the module docstring.
        Whatever Claude says back is consumed here and never reaches
        meeting chat: the first *real* turn is what the room sees.

        Turn 0's Stop payload carries `transcript_path` — captured here
        so real turns can tail the transcript for real-time narration.
        This blocks until turn 0's reply row lands: a real turn must not
        start while turn 0 is in flight (see the inline comment). A
        briefing that never lands raises — caught by pre_warm onto
        _spawn_exc and surfaced on the next @mention, the deferred half
        of never-post-unprompted.
        """
        prev = self._count_replies()
        briefing = _BRIEFING + (_BRIEFING_GUARDED_SUFFIX if self._guarded else "")
        self._send_message(briefing)
        # Block until turn 0's reply row lands — no soft "proceed
        # anyway". If a real turn starts while turn 0 is still in
        # flight, turn 0's reply row and trailing transcript blocks get
        # misattributed to that turn and leak into meeting chat. The
        # only non-arrival paths are inner-claude dying (raised inside
        # _wait_for_next_reply) or a genuinely wedged claude (the shared
        # boot ceiling running out) — both abort the spawn, the same
        # fail-loud shape as _wait_for_ready. A briefing hiccup is a
        # boot failure, not a "carry on". The budget is whatever's left
        # of the shared boot deadline after _wait_for_ready spent its
        # share.
        budget = max(0.0, self._boot_deadline - time.monotonic())
        reply = self._wait_for_next_reply(prev, timeout=budget)
        if reply is None:
            raise ClaudeCLIProtocolError(
                f"briefing (turn 0) produced no reply before the "
                f"{_BOOT_CEILING_SECONDS:.0f}s boot ceiling — inner-claude is "
                f"wedged, or the operator-plugin Stop hook is not writing "
                f"replies.jsonl. Verify with `ls {self._session_dir}`.\n"
                f"PTY tail:\n{self._pty_tail()}"
            )
        tp = self._extract_transcript_path(reply)
        if tp:
            self._transcript_path = tp
        sid = self._extract_session_id(reply)
        if sid and not self._captured_session_id:
            self._captured_session_id = sid
        # Let turn 0's trailing assistant block flush to the transcript
        # before boot completes — otherwise it can land a write-beat
        # after the next turn snapshots its tail offset and bleed in.
        time.sleep(_TRANSCRIPT_FINAL_DRAIN_SETTLE)
        log.info(
            f"ClaudeCLI: briefing acknowledged; turn 0 consumed "
            f"(transcript={'captured' if tp else 'MISSING'})"
        )

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

    def _wait_for_next_reply(self, prev_count, timeout, on_poll=None):
        """Tail replies.jsonl until count > prev_count or timeout.

        Returns the parsed reply object (the Stop hook's payload), or
        None on timeout. Fires the tick callback on every poll so
        ChatRunner can drain its off-thread send queue. `on_poll`, if
        given, is called once per poll iteration — _run_turn uses it to
        tail the transcript for real-time narration; _send_briefing
        passes nothing so turn 0's narration is not posted.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # Clean exit on teardown: stop() sets _stopping then SIGTERMs
            # the group. Catching the flag here returns quietly instead
            # of letting the imminent proc death raise the alarming
            # "inner-claude exited unexpectedly" crash dump for what is
            # really an orderly shutdown.
            if self._stopping:
                return None
            self._fire_tick()
            if on_poll is not None:
                on_poll()
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

    # --- transcript tail (real-time narration) -----------------------

    def _transcript_size(self):
        """Current byte size of the transcript JSONL, or 0 if unknown.
        Used as the per-turn tail start offset, captured before the send.
        """
        if self._transcript_path is None:
            return 0
        try:
            return self._transcript_path.stat().st_size
        except OSError:
            return 0

    def _read_transcript_lines(self, offset, buf):
        """Read new complete JSONL events from the transcript past `offset`.

        Returns (new_offset, leftover_buf, [parsed_events]). Tolerates a
        missing file and a partial trailing line (held in buf for the
        next call) — same seek-and-buffer discipline replies tailing uses.
        """
        path = self._transcript_path
        if path is None:
            return offset, buf, []
        try:
            size = path.stat().st_size
        except OSError:
            return offset, buf, []
        if size <= offset:
            return offset, buf, []
        try:
            with path.open("rb") as f:
                f.seek(offset)
                chunk = f.read()
        except OSError:
            return offset, buf, []
        offset += len(chunk)
        buf += chunk
        parts = buf.split(b"\n")
        buf = parts.pop()
        events = []
        for line in parts:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line.decode("utf-8")))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
        return offset, buf, events

    # --- permreq tail (yolo-off mode) -------------------------------

    def _poll_permreqs(self):
        """Read new request lines from permreq_requests.jsonl and fire
        the PermissionRequest callback for each.

        Same seek-and-buffer discipline the transcript tail uses; offset
        is reset per-spawn in _spawn_inner so a respawn doesnt re-fire
        stale requests. Best-effort: missing file, parse error, or a
        callback exception is silently skipped — the operator-plugin
        hook self-denies on its 120s timeout if we never write the
        answer file, so a missed request degrades to "tool denied,
        meeting moves on" rather than a hang.

        No-op when no callback is registered (yolo-on path).
        """
        cb = self._permreq_callback
        if cb is None:
            return
        path = self._session_dir / "permreq_requests.jsonl"
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size <= self._permreq_offset:
            return
        try:
            with path.open("rb") as f:
                f.seek(self._permreq_offset)
                chunk = f.read()
        except OSError:
            return
        self._permreq_offset += len(chunk)
        self._permreq_buf += chunk
        parts = self._permreq_buf.split(b"\n")
        self._permreq_buf = parts.pop()
        for line in parts:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(req, dict) or "request_id" not in req:
                continue
            # Augment with the answer path so ChatRunner doesn't need
            # to know our session-dir layout.
            try:
                cb({
                    "request_id": req["request_id"],
                    "ts": req.get("ts"),
                    "tool_name": req.get("tool_name"),
                    "tool_input": req.get("tool_input"),
                    "answer_path": (
                        self._session_dir / "permreq_answers"
                        / (req["request_id"] + ".json")
                    ),
                })
            except Exception as e:
                log.warning(f"ClaudeCLI: permission_request callback raised: {e}")

    @staticmethod
    def _assistant_texts(events):
        """Extract assistant text blocks from transcript events, in order.

        Transcript event shape: {"type": "assistant", "message":
        {"content": [{"type": "text", "text": ...}, {"type": "tool_use",
        ...}]}}. tool_use blocks and non-assistant events are skipped;
        `content` is also tolerated as a bare string. Returns the list of
        non-empty text strings.
        """
        texts = []
        for ev in events:
            if not isinstance(ev, dict) or ev.get("type") != "assistant":
                continue
            msg = ev.get("message")
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, str):
                blocks = [{"type": "text", "text": content}]
            elif isinstance(content, list):
                blocks = content
            else:
                continue
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "text":
                    t = block.get("text")
                    if isinstance(t, str) and t.strip():
                        texts.append(t)
        return texts

    @staticmethod
    def _has_foreign_hook_feedback(events):
        """True if a user-role transcript event carries the literal
        'Stop hook feedback:' marker — a foreign Stop hook ran
        decision=block and injected a redirect this turn (DECISION.md
        section I: observable, not preventable).
        """
        MARKER = "Stop hook feedback:"
        for ev in events:
            if not isinstance(ev, dict) or ev.get("type") != "user":
                continue
            msg = ev.get("message")
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, str):
                if MARKER in content:
                    return True
            elif isinstance(content, list):
                for block in content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "text"
                        and isinstance(block.get("text"), str)
                        and MARKER in block["text"]
                    ):
                        return True
        return False

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

    @staticmethod
    def _extract_transcript_path(reply):
        """Pull `transcript_path` from a Stop hook payload, as a Path."""
        if not isinstance(reply, dict):
            return None
        inner = reply.get("input") if isinstance(reply.get("input"), dict) else reply
        tp = inner.get("transcript_path")
        return Path(tp) if isinstance(tp, str) and tp else None

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
        """Same as complete(), but posts assistant text as it lands.

        _run_turn tails the Claude Code transcript JSONL during the turn
        and calls on_paragraph for each assistant text block the moment
        it appears — so Claude's self-narration ("let me grab that
        file") reaches chat in real time, not batched at end-of-turn.
        This restores the streaming behaviour the early PTY pivot lost,
        without screen-scraping the TUI (the transcript is structured
        JSONL Claude Code writes itself).
        """
        if on_paragraph is None:
            return self.complete(system, messages, model, max_tokens, tools=tools)
        return self._run_turn(messages, on_paragraph=on_paragraph)

    def warmup(self, model):
        """No-op. pre_warm() is the meaningful warmup for this provider."""
        return None

    def _run_turn(self, messages, on_paragraph):
        """One turn, with a single retry on failure.

        Per-incident retry: each call gets exactly one retry, structural
        in the nested try/except — no counter. The first attempt fails →
        terminate the (now-dead-or-wedged) inner-claude → one fresh
        attempt → if that also fails, latch `_unavailable` and raise.
        A successful turn never latches. Recovery from a latched meeting
        is /operator:hangup + rejoin, not accumulating retries.
        """
        if self._unavailable:
            raise ClaudeCLIProtocolError("claude is unavailable")
        try:
            return self._attempt_turn(messages, on_paragraph)
        except ClaudeCLIProtocolError as first_exc:
            if self._stopping:
                # Teardown, not a failure — let the orderly-shutdown
                # path raise without burning the retry.
                raise
            log.warning(f"ClaudeCLI: turn failed ({first_exc}) — retrying once")
            self._terminate_inner()
            try:
                return self._attempt_turn(messages, on_paragraph)
            except ClaudeCLIProtocolError as second_exc:
                self._unavailable = True
                # Tag the phase for the post-failure snapshot. _spawn_exc
                # is set by pre_warm whenever boot itself failed; if it's
                # set, the retry attempted a fresh boot and that boot
                # failed. Otherwise the retry got past boot and the turn
                # itself raised.
                self._last_failure_phase = "boot" if self._spawn_exc is not None else "turn"
                log.error(
                    f"ClaudeCLI: claude unavailable — retry also failed "
                    f"({second_exc})"
                )
                raise

    def _attempt_turn(self, messages, on_paragraph):
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

        # --- ensure inner-claude is booted and ready ---------------------
        # If a previous spawn died (inner-claude crashed, the PTY hit EOF),
        # tear its threads + master_fd down before respawning — otherwise
        # the dead spawn's PTY-drain / tail threads leak against the same
        # files.
        if self._proc is not None and self._proc.poll() is not None:
            log.info("ClaudeCLI: inner-claude died — tearing down before respawn")
            self._terminate_inner()
        # Boot not running and not already up → start it. Normally
        # __main__ already kicked pre_warm off on a background thread
        # during the join window, so this is a no-op; it covers a missed
        # pre_warm and the post-crash respawn above.
        if self._proc is None and not self._spawn_in_progress:
            self.pre_warm()
        # pre_warm sets _proc EARLY — when Popen returns, before
        # _wait_for_ready and the briefing — so "_proc is alive" is not
        # "ready for a turn". Block until the boot has actually finished.
        # The user @mentioned, so a one-line "still coming up" is an
        # authorized reply: never-post-unprompted forbids *unsolicited*
        # chat, and this turn was solicited.
        if not self._boot_done.is_set():
            if on_paragraph is not None:
                on_paragraph("still getting set up — give me a moment…")
            # Bounded by the boot ceiling + margin; if pre_warm never
            # finishes, the checks below turn the timeout into a clear
            # failure rather than a hang.
            self._boot_done.wait(timeout=_BOOT_CEILING_SECONDS + 30)
        # Boot finished (or we gave up waiting). A boot failure recorded
        # silently on _spawn_exc now reaches the room — the deferred half
        # of never-post-unprompted: logged at boot, surfaced only here, on
        # an actual @mention. The raise propagates to _run_turn, which
        # gives it one retry before latching _unavailable.
        if self._spawn_exc is not None:
            raise ClaudeCLIProtocolError(str(self._spawn_exc))
        if self._proc is None or not self._boot_done.is_set():
            raise ClaudeCLIProtocolError(
                "inner-claude did not finish starting up in time"
            )

        t_start = time.monotonic()
        prev_count = self._count_replies()

        # Transcript tail state — captured BEFORE the send so the user
        # message and every assistant block this turn falls past the
        # offset. Closure mutables because _wait_for_next_reply drives
        # the poll loop and calls _poll_transcript back in. `posted`
        # accumulates the text blocks already surfaced this turn.
        tx_offset = [self._transcript_size()]
        tx_buf = [b""]
        posted: list[str] = []
        last_block_ts = [None]   # monotonic time operator last saw an assistant block
        foreign_hook = [False]   # a foreign Stop hook redirected this turn

        def _poll_transcript():
            tx_offset[0], tx_buf[0], events = self._read_transcript_lines(
                tx_offset[0], tx_buf[0]
            )
            for block in self._assistant_texts(events):
                posted.append(block)
                last_block_ts[0] = time.monotonic()
                if on_paragraph is not None:
                    flush_paragraphs(block, on_paragraph, force_final=True)
            if not foreign_hook[0] and self._has_foreign_hook_feedback(events):
                foreign_hook[0] = True

        # Compose the per-iteration poll: transcript tail (real-time
        # narration) plus permreq tail (yolo-off mode — fires
        # PermissionRequest callback for each new request the hook
        # writes). _poll_permreqs is a no-op when no callback is
        # registered (yolo-on path).
        def _poll():
            _poll_transcript()
            self._poll_permreqs()

        self._send_message(user_text)

        # Generous per-turn timeout — claude tool loops can run minutes
        # legitimately. The user cancels via /operator:hangup if a tool
        # chain wedges; no operator-imposed deadline.
        reply = self._wait_for_next_reply(
            prev_count, timeout=600.0, on_poll=_poll
        )
        t_reply = time.monotonic()
        elapsed = t_reply - t_start
        # Snapshot before the final drain — last_block_ts keeps advancing
        # if the drain picks up a late block, but for the Stop-lag signal
        # we want "last block operator had seen when Stop fired".
        last_block_at_stop = last_block_ts[0]

        if reply is None:
            if self._stopping:
                # _wait_for_next_reply bailed on the teardown flag, not a
                # timeout — a clean "winding down", not a crash.
                raise ClaudeCLIProtocolError("provider is stopping")
            raise ClaudeCLIProtocolError(
                f"timed out after {elapsed:.0f}s waiting for Stop hook reply.\n"
                "Likely cause: operator-plugin's hooks are not installed (no "
                "replies.jsonl was written). Verify with `ls "
                f"{self._session_dir}`.\nPTY tail:\n{self._pty_tail()}"
            )

        # The final assistant block may land in the transcript a beat
        # after the Stop hook fires — settle, then drain once more.
        time.sleep(_TRANSCRIPT_FINAL_DRAIN_SETTLE)
        _poll_transcript()

        sid = self._extract_session_id(reply)
        if sid and not self._captured_session_id:
            self._captured_session_id = sid
        # Backfill the transcript path if the briefing missed it — the
        # next turn then gets real-time narration.
        if self._transcript_path is None:
            tp = self._extract_transcript_path(reply)
            if tp:
                self._transcript_path = tp

        # Backstop: if the Stop payload's last_assistant_message never
        # came through the transcript tail (no transcript path captured,
        # or a write race), post it now so the turn isn't silent.
        stop_text = self._extract_assistant_text(reply)
        if stop_text and stop_text not in posted:
            posted.append(stop_text)
            if on_paragraph is not None:
                flush_paragraphs(stop_text, on_paragraph, force_final=True)

        text = "\n\n".join(posted) if posted else stop_text

        # Section I — foreign-hook observability. A foreign Stop hook
        # that ran decision=block injects "Stop hook feedback:" as a
        # user turn; surface that to the room as a notice. We only know
        # the hook *interrupted* the turn — not whether claude acted on
        # it (claude's prompt-injection defense ignores adversarial
        # reasons; a benign-looking project hook it may well follow), so
        # the notice claims only the interruption. The Stop-lag gap
        # (final visible block → Stop row) is a noisier proxy — log
        # only, not chat-worthy.
        notices: list[str] = []
        if foreign_hook[0]:
            log.warning("ClaudeCLI: foreign Stop-hook feedback detected this turn")
            notices.append(
                "heads up — a hook outside this meeting interrupted my last turn"
            )
        if last_block_at_stop is not None:
            stop_gap = t_reply - last_block_at_stop
            if stop_gap > _FOREIGN_HOOK_DELAY_WARN_SECONDS:
                log.warning(
                    f"ClaudeCLI: {stop_gap:.1f}s gap between final assistant "
                    f"block and Stop hook — foreign hooks may have run"
                )

        log.info(
            f"TIMING claude_cli_turn={elapsed:.1f}s "
            f"reply_blocks={len(posted)} "
            f"reply_chars={len(text) if text else 0} "
            f"foreign_hook={foreign_hook[0]} "
            f"session={sid or '?'}"
        )

        return ProviderResponse(
            text=text or None,
            tool_calls=[],
            stop_reason="end",
            notices=notices,
        )
