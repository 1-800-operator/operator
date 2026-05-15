"""
PermissionClassifier — interactive sidecar claude that interprets a
participant's chat reply to a permission question as YES or NO.

WHY THIS EXISTS
---------------
Operator's "yolo off" mode posts a permission question into meeting
chat ("Claude wants to use Bash to run X. Reply yes or no.") and waits
for a participant to answer. Mapping a free-form reply ("sure", "go
ahead", "nah, skip it", "👍", "sí adelante") to allow/deny shouldn't
be done by hardcoded word-matching — it's exactly the kind of natural-
language interpretation the model is good at.

The hook contract for PermissionRequest is binary, so we can't
roundtrip the user's words through the *main* inner-claude session via
the deny `message` field — claude's prompt-injection defense fires on
text arriving through the tool-result channel and refuses to act on it
(spike 14_25 confirmed this with 5/8 common approvals misclassified,
including 'yes' and '👍'). The fix is a SEPARATE long-lived claude
session that receives the user's reply as a normal user-turn input,
where no safety quarantine fires (spike 14_26 confirmed: 19/19 clean).

DESIGN
------
One long-lived `claude --dangerously-skip-permissions` subprocess per
meeting, driven over a PTY exactly like the main inner-claude session.
Stays on the subscription pool — naked spawn (no `-p`, no
`--append-system-prompt`, no `--mcp-config`) per the 14.22 invariant.

Lifecycle:
  - `pre_warm()` spawns the subprocess in parallel with the main
    provider's pre_warm during the meeting-join window (~6s settle is
    hidden inside the existing join latency).
  - `classify(reply, question)` sends one tiny turn — a focused prompt
    asking for YES or NO — and blocks for the Stop hook to fire.
    Returns True (allow) / False (deny). Defaults to False on any
    failure (timeout, parse error, subprocess crash) — the operator-
    side hook contract is "deny is the safe default."
  - `stop()` tears down the PTY. Idempotent.

State directory: separate from the main session_dir so the classifier's
own SessionStart / Stop hooks don't collide with the meeting's.
Defaults to ~/.operator/sessions/<uuid>-classifier/.

`--dangerously-skip-permissions` for the classifier itself: it
shouldn't ever try to use a tool (the prompt explicitly tells it not
to), but if it ever did, skip-permissions prevents a tool prompt from
hanging the classifier turn.
"""
from __future__ import annotations

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

log = logging.getLogger(__name__)


# Bracketed-paste timings — same as ClaudeCLIProvider; proven against
# the 14.22 spike's tough-input sweep.
_BRACKET_OPEN_DELAY = 0.05
_BRACKET_BODY_DELAY = 0.1
_BRACKET_CLOSE_DELAY = 0.2

_PTY_ROWS = 40
_PTY_COLS = 120

# How long to wait after spawn before sending the first turn. The PTY
# settles in well under 6s in practice (matches the 14_26 spike's boot
# latency). Hidden inside the meeting-join window so users never feel it.
_SETTLE_SECONDS = 6.0

# Per-classification turn timeout. The 14_26 spike measured 2.1-5.0s
# end-to-end; 30s is a generous ceiling for a single yes/no turn so a
# slow LLM moment doesn't trip the timeout. Beyond this we deny.
_CLASSIFY_TIMEOUT = 30.0

# Polling cadence for the classifier's reply tail. Same value the main
# provider uses; in the noise floor of the meeting-chat send path.
_POLL_SECONDS = 0.15


# Single classifier prompt template. Plain English, asks for one of two
# tokens, gives a fail-safe default ("if unsure, NO"). Same structure
# the 14_26 spike validated 19/19 against.
_PROMPT_TEMPLATE = """You are helping me interpret a participant's reply in a Google Meet chat. The bot just asked them a permission question. I need to know whether they approved.

The bot asked:
> {question}

The participant replied:
> {reply!r}

Did they approve the request? Reply with exactly one word: YES if they approved, NO if they declined or were unclear. If you're unsure, reply NO (deny is the safe default)."""


_YESNO_RE = re.compile(r"\b(YES|NO)\b")


class PermissionClassifierError(RuntimeError):
    """Classifier subprocess died, the PTY broke, or a turn timed out."""


def _set_winsize(fd, rows, cols):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _drain_pty_thread(master_fd, dump_buf, stop_event):
    """Drain master_fd into a rolling buffer for diagnostics on death.
    Same pattern ClaudeCLIProvider uses — the TUI emits cursor-positioned
    bytes that aren't useful to parse, but the tail is helpful for a
    crash dump."""
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


class PermissionClassifier:
    """Long-lived sidecar claude that classifies chat replies as YES/NO.

    Constructed once per meeting, pre-warmed during the join window,
    used by ChatRunner per permission ask, torn down at meeting end.

    Thread-safe `classify()`: calls are serialised by an internal lock
    so two near-simultaneous classifications don't interleave bracketed-
    pastes into the same PTY. In practice ChatRunner serialises permreq
    handling already, so this is belt-and-suspenders.
    """

    def __init__(self, *, session_dir: Path | None = None):
        if session_dir is None:
            session_dir = (
                Path.home() / ".operator" / "sessions"
                / f"{uuid.uuid4().hex}-classifier"
            )
        self._session_dir = Path(session_dir)
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._replies_path = self._session_dir / "replies.jsonl"

        self._proc: subprocess.Popen | None = None
        self._master_fd: int | None = None
        self._pty_reader_stop = threading.Event()
        self._pty_reader_thread: threading.Thread | None = None
        self._pty_dump: list[bytes] = []

        self._spawn_lock = threading.Lock()
        self._spawn_in_progress = False
        self._spawn_exc: Exception | None = None
        self._stopping = False

        # Serialises classify() calls — bracketed-pastes mustn't
        # interleave on the same PTY.
        self._classify_lock = threading.Lock()

    # ---- lifecycle --------------------------------------------------

    def pre_warm(self) -> None:
        """Spawn the classifier subprocess. Idempotent. Best-effort —
        a spawn failure logs and leaves `_spawn_exc` set; the next
        classify() call sees an unspawned classifier and denies."""
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
            self._spawn_exc = None
        except Exception as exc:
            log.warning(f"PermissionClassifier: pre_warm spawn failed: {exc}")
            self._spawn_exc = exc
        finally:
            with self._spawn_lock:
                self._spawn_in_progress = False

    def stop(self) -> None:
        """Tear down the classifier PTY. Idempotent."""
        if self._stopping:
            return
        log.info("PermissionClassifier: stop() called")
        self._stopping = True
        self._terminate_inner()

    def _spawn_inner(self) -> None:
        claude = shutil.which("claude")
        if not claude:
            raise PermissionClassifierError(
                "`claude` CLI not found on PATH; classifier cannot start."
            )
        cmd = [claude, "--dangerously-skip-permissions"]
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        # Hooks running inside the classifier subprocess (e.g. the
        # operator-plugin SessionStart / Stop scripts) are gated on this
        # env var pointing at OUR session_dir — keeping them isolated
        # from the meeting's main session_dir.
        env["OPERATOR_SESSION_DIR"] = str(self._session_dir)

        master_fd, slave_fd = pty.openpty()
        _set_winsize(master_fd, _PTY_ROWS, _PTY_COLS)

        log.info(
            f"PermissionClassifier spawning sidecar claude: "
            f"cwd={self._session_dir} session_dir={self._session_dir}"
        )
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(self._session_dir),
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                start_new_session=True,
                env=env,
                close_fds=True,
            )
        except OSError as exc:
            os.close(master_fd)
            os.close(slave_fd)
            raise PermissionClassifierError(
                f"failed to launch classifier: {exc}"
            ) from exc

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

        # Settle: let the TUI come up before the first paste. The 14_26
        # spike's boot+settle landed at ~6s consistently. We deliberately
        # do NOT wait for ready.flag here — the classifier's session_dir
        # is separate from the main session, so the operator-plugin's
        # SessionStart hook (which writes ready.flag) fires INTO our
        # session_dir without us caring. A simple settle is sufficient
        # for a non-interactive Q&A workload.
        deadline = time.monotonic() + _SETTLE_SECONDS
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise PermissionClassifierError(
                    f"classifier exited during settle (rc={proc.returncode}). "
                    f"PTY tail:\n{self._pty_tail()}"
                )
            time.sleep(0.1)
        log.info(f"PermissionClassifier: sidecar ready (pid={proc.pid})")

    def _terminate_inner(self) -> None:
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

        self._proc = None
        self._master_fd = None

    def _pty_tail(self, n_bytes: int = 2000) -> str:
        joined = b"".join(self._pty_dump)
        tail = joined[-n_bytes:]
        try:
            return tail.decode("utf-8", errors="replace")
        except Exception:
            return "<undecodable>"

    # ---- send + reply tail -----------------------------------------

    def _send_message(self, msg: str) -> None:
        if self._master_fd is None:
            raise PermissionClassifierError(
                "classifier not running; pre_warm or classify must spawn first"
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
            raise PermissionClassifierError(
                f"PTY write failed: {exc}"
            ) from exc

    def _count_replies(self) -> int:
        try:
            with self._replies_path.open("rb") as f:
                return sum(1 for _ in f)
        except (FileNotFoundError, OSError):
            return 0

    def _read_reply_at(self, index: int) -> dict | None:
        try:
            with self._replies_path.open("r", encoding="utf-8") as f:
                for i, line in enumerate(f):
                    if i == index:
                        return json.loads(line)
        except (OSError, json.JSONDecodeError):
            return None
        return None

    def _wait_for_next_reply(self, prev_count: int, timeout: float) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._stopping:
                return None
            if self._proc is not None and self._proc.poll() is not None:
                raise PermissionClassifierError(
                    f"classifier exited unexpectedly (rc={self._proc.returncode}). "
                    f"PTY tail:\n{self._pty_tail()}"
                )
            current = self._count_replies()
            if current > prev_count:
                reply = self._read_reply_at(prev_count)
                if reply is not None:
                    return reply
            time.sleep(_POLL_SECONDS)
        return None

    @staticmethod
    def _extract_assistant_text(reply: dict) -> str:
        if not isinstance(reply, dict):
            return ""
        inner = reply.get("input") if isinstance(reply.get("input"), dict) else reply
        text = inner.get("last_assistant_message")
        return text if isinstance(text, str) else ""

    @staticmethod
    def _parse_yesno(text: str) -> bool | None:
        """Return True for YES, False for NO, None if neither token is
        present as a standalone word. Matches the 14_26 spike's
        parser."""
        if not text:
            return None
        m = _YESNO_RE.search(text.upper())
        if m is None:
            return None
        return m.group(1) == "YES"

    # ---- public API -------------------------------------------------

    def classify(self, reply_text: str, question_text: str) -> bool:
        """Classify a meeting-chat reply as approval (True) or refusal
        (False). Blocks for up to ~3s typical / 30s ceiling.

        Defaults to False (deny) on any failure: subprocess not
        spawned, classifier crashed mid-meeting, turn timed out, parse
        of the response was unclear. The operator hook contract is
        "deny is the safe default" — same principle here.
        """
        if self._stopping:
            log.warning("PermissionClassifier: classify() while stopping → deny")
            return False
        with self._classify_lock:
            # Lazy spawn: if pre_warm wasn't called or the subprocess
            # died, try once now. Failure → deny.
            if self._proc is None or self._proc.poll() is not None:
                log.info(
                    "PermissionClassifier: subprocess not alive at classify() — "
                    "attempting (re)spawn"
                )
                if self._proc is not None:
                    self._terminate_inner()
                self.pre_warm()
                if self._proc is None:
                    detail = (
                        f": {self._spawn_exc}" if self._spawn_exc else ""
                    )
                    log.warning(
                        f"PermissionClassifier: classifier unavailable{detail} → deny"
                    )
                    return False
            prompt = _PROMPT_TEMPLATE.format(
                question=question_text or "(no question text captured)",
                reply=reply_text or "",
            )
            t0 = time.monotonic()
            prev = self._count_replies()
            try:
                self._send_message(prompt)
                reply = self._wait_for_next_reply(prev, timeout=_CLASSIFY_TIMEOUT)
            except PermissionClassifierError as exc:
                log.warning(
                    f"PermissionClassifier: classify() raised {exc} → deny"
                )
                return False
            elapsed = time.monotonic() - t0
            if reply is None:
                log.warning(
                    f"PermissionClassifier: classify() timed out after "
                    f"{elapsed:.1f}s → deny"
                )
                return False
            text = self._extract_assistant_text(reply)
            verdict = self._parse_yesno(text)
            if verdict is None:
                snip = text[:120].replace("\n", " ") if text else ""
                log.warning(
                    f"PermissionClassifier: response had no YES/NO token "
                    f"({snip!r}) → deny"
                )
                return False
            log.info(
                f"TIMING classifier_turn={elapsed:.2f}s "
                f"verdict={'allow' if verdict else 'deny'}"
            )
            return verdict
