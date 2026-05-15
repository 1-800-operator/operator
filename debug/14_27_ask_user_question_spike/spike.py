"""
14.27 spike — fire AskUserQuestion at a PTY-driven claude and observe.

Spawns `claude --dangerously-skip-permissions` the same way
src/_1_800_operator/pipeline/providers/claude_cli.py does (bracketed-paste,
120x40 winsize, ANTHROPIC_API_KEY stripped), in a fresh tmpdir so no
project hooks / CLAUDE.md interfere. Sends the confirmed trigger:

    Use the AskUserQuestion tool to ask me whether X or Y, then ask
    whether A or B.

For OBSERVE_SECONDS after sending, captures:
  - PTY bytes  -> out/run_<ts>/pty.bin (+ pty.txt with ANSI stripped)
  - transcript -> out/run_<ts>/transcript.jsonl
  - events.log -> timeline of tool_use / assistant text / timing

The spike sends NO answer. The point is to observe what claude does when
the question goes unanswered (block? timeout? self-cancel?). A follow-up
run can try injecting a digit / arrow keys via the PTY.
"""
import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path

# Spawn shape mirrors claude_cli.py.
PTY_ROWS = 40
PTY_COLS = 120
BRACKET_OPEN_DELAY = 0.05
BRACKET_BODY_DELAY = 0.1
BRACKET_CLOSE_DELAY = 0.2

# How long to wait for boot before sending (no plugin ready.flag here —
# we just sleep, then check the transcript file exists).
BOOT_SETTLE_SECONDS = 6.0

# Default observation window after sending. Override via --observe.
DEFAULT_OBSERVE_SECONDS = 180.0

DEFAULT_TRIGGER = (
    "Use the AskUserQuestion tool to ask me whether X or Y, then ask "
    "whether A or B."
)

ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[=>]")


def set_winsize(fd, rows, cols):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def encode_project_dir(cwd: Path) -> str:
    """Claude Code encodes cwd into ~/.claude/projects/<encoded>/ by
    dropping the leading slash and replacing / with -."""
    s = str(cwd)
    if s.startswith("/"):
        s = s[1:]
    return "-" + s.replace("/", "-")


def find_new_transcript(projects_dir: Path, baseline: set[Path]) -> Path | None:
    """Return the newest .jsonl in projects_dir that wasn't in baseline."""
    if not projects_dir.exists():
        return None
    candidates = [
        p for p in projects_dir.glob("*.jsonl") if p not in baseline
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


class PTYDrain:
    """Background thread that drains the master PTY into a bytes buffer."""

    def __init__(self, master_fd: int):
        self.master_fd = master_fd
        self.buf = bytearray()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=2)

    def _run(self):
        while not self.stop_event.is_set():
            try:
                r, _, _ = select.select([self.master_fd], [], [], 0.2)
            except (OSError, ValueError):
                return
            if not r:
                continue
            try:
                chunk = os.read(self.master_fd, 4096)
            except OSError:
                return
            if not chunk:
                return
            self.buf.extend(chunk)


def bracketed_paste(master_fd: int, msg: str):
    """Bracketed-paste wrap + CR — same timings as claude_cli._send_message."""
    payload = msg.encode("utf-8")
    os.write(master_fd, b"\x1b[200~")
    time.sleep(BRACKET_OPEN_DELAY)
    os.write(master_fd, payload)
    time.sleep(BRACKET_BODY_DELAY)
    os.write(master_fd, b"\x1b[201~")
    time.sleep(BRACKET_CLOSE_DELAY)
    os.write(master_fd, b"\r")


def read_jsonl(path: Path) -> list[dict]:
    """Best-effort read of a JSONL file (tolerate partial last line)."""
    if not path.exists():
        return []
    out = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def summarize_event(ev: dict) -> str | None:
    """One-line summary of an interesting transcript event, or None to skip."""
    t = ev.get("type")
    if t == "assistant":
        msg = ev.get("message") or {}
        content = msg.get("content")
        blocks = content if isinstance(content, list) else (
            [{"type": "text", "text": content}] if isinstance(content, str) else []
        )
        parts = []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                txt = (b.get("text") or "").strip().replace("\n", " ")
                if txt:
                    parts.append(f"text[{len(txt)}c]={txt[:120]!r}")
            elif bt == "tool_use":
                name = b.get("name")
                inp = b.get("input")
                # AskUserQuestion is the one we care about — dump its full input.
                if name == "AskUserQuestion":
                    parts.append(
                        f"tool_use=AskUserQuestion input={json.dumps(inp)[:800]}"
                    )
                else:
                    parts.append(f"tool_use={name}")
        if parts:
            return "ASSISTANT " + " | ".join(parts)
        return None
    if t == "user":
        msg = ev.get("message") or {}
        content = msg.get("content")
        blocks = content if isinstance(content, list) else (
            [{"type": "text", "text": content}] if isinstance(content, str) else []
        )
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                tu_id = b.get("tool_use_id")
                rc = b.get("content")
                rc_str = json.dumps(rc) if not isinstance(rc, str) else rc
                return f"USER tool_result[{tu_id}]={rc_str[:300]}"
        return None
    if t == "system":
        sub = ev.get("subtype")
        return f"SYSTEM subtype={sub}" if sub else None
    return None


def parse_args():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--cwd", default="/Users/jojo/Desktop/operator",
                   help="cwd for the inner-claude spawn (must be pre-trusted)")
    p.add_argument("--prompt", default=DEFAULT_TRIGGER,
                   help="prompt to send via bracketed-paste")
    p.add_argument("--observe", type=float, default=DEFAULT_OBSERVE_SECONDS,
                   help="observation seconds after the send")
    p.add_argument("--label", default="",
                   help="label appended to the run output dir name")
    p.add_argument("--post-keystroke", default="",
                   help="raw bytes to write to the PTY after --post-delay seconds")
    p.add_argument("--post-delay", type=float, default=0.0,
                   help="seconds after the trigger send to write --post-keystroke")
    p.add_argument("--briefing", default="",
                   help="optional turn-0 briefing sent before the trigger")
    p.add_argument("--briefing-wait", type=float, default=60.0,
                   help="seconds to wait for the briefing's reply before sending trigger")
    return p.parse_args()


def run_spike():
    args = parse_args()
    claude = shutil.which("claude")
    if not claude:
        print("ERROR: `claude` not on PATH", file=sys.stderr)
        sys.exit(2)

    cwd = Path(args.cwd)
    if not cwd.exists():
        print(f"ERROR: cwd does not exist: {cwd}", file=sys.stderr)
        sys.exit(2)
    # macOS: /var is a symlink to /private/var, and Claude Code resolves
    # symlinks for the project-dir encoding. Resolve once up-front.
    cwd = cwd.resolve()
    projects_dir = Path.home() / ".claude" / "projects" / encode_project_dir(cwd)

    ts = time.strftime("%Y%m%d_%H%M%S")
    suffix = f"_{args.label}" if args.label else ""
    out_dir = Path(__file__).parent / "out" / f"run_{ts}{suffix}"
    out_dir.mkdir(parents=True, exist_ok=True)
    events_log = (out_dir / "events.log").open("w", encoding="utf-8")

    def event(line: str):
        stamped = f"[{time.monotonic():.2f}] {line}"
        print(stamped)
        events_log.write(stamped + "\n")
        events_log.flush()

    event(f"cwd={cwd}")
    event(f"projects_dir={projects_dir}")
    event(f"out_dir={out_dir}")

    # Baseline transcripts so we can identify "ours" later.
    baseline = set(projects_dir.glob("*.jsonl")) if projects_dir.exists() else set()

    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

    master_fd, slave_fd = pty.openpty()
    set_winsize(master_fd, PTY_ROWS, PTY_COLS)

    cmd = [claude, "--dangerously-skip-permissions"]
    event(f"spawn: {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        start_new_session=True,
        env=env,
        close_fds=True,
    )
    os.close(slave_fd)
    drain = PTYDrain(master_fd)
    drain.start()

    try:
        # Wait for boot. No SessionStart hook to signal — poll for the
        # transcript file appearing as the boot-complete proxy.
        boot_deadline = time.monotonic() + 30.0
        transcript_path = None
        while time.monotonic() < boot_deadline:
            if proc.poll() is not None:
                event(f"claude exited during boot rc={proc.returncode}")
                return
            # Trust-dialog guard. The bracketed-paste's trailing CR will
            # accidentally confirm the workspace-trust dialog and eat the
            # trigger if we let boot proceed. Bail loudly instead — the
            # user must pre-trust the chosen cwd in Claude Code first.
            compact = ANSI_RE.sub(b"", bytes(drain.buf)).decode("utf-8", "replace").lower()
            compact = re.sub(r"\s+", "", compact)
            if "doyoutrust" in compact or "isthisaproject" in compact or "trustthisfolder" in compact:
                event("ABORT: workspace-trust dialog appeared — cwd is not pre-trusted")
                event(f"open this folder in Claude Code directly first: {cwd}")
                return
            transcript_path = find_new_transcript(projects_dir, baseline)
            if transcript_path is not None:
                event(f"transcript appeared: {transcript_path}")
                break
            time.sleep(0.2)
        if transcript_path is None:
            event("no transcript file appeared within 30s — sending anyway")
        # Brief settle so the TUI is fully ready before the paste.
        time.sleep(BOOT_SETTLE_SECONDS)

        # Optional turn-0 briefing — sent first, wait for its reply (proxy:
        # a stop_hook_summary event from user-level hooks, OR a 3s window of
        # transcript quiet after at least one assistant event landed), then
        # send the actual trigger as turn 1.
        if args.briefing:
            event(f"sending briefing ({len(args.briefing)} chars): {args.briefing!r}")
            bracketed_paste(master_fd, args.briefing)
            briefing_sent_ts = time.monotonic()
            briefing_deadline = briefing_sent_ts + args.briefing_wait
            seen_assistant = False
            last_event_ts = briefing_sent_ts
            last_count = 0
            briefing_done = False
            while time.monotonic() < briefing_deadline:
                if transcript_path is None:
                    transcript_path = find_new_transcript(projects_dir, baseline)
                if transcript_path is not None:
                    events = read_jsonl(transcript_path)
                    if len(events) > last_count:
                        last_event_ts = time.monotonic()
                        for ev in events[last_count:]:
                            if ev.get("type") == "assistant":
                                seen_assistant = True
                            if ev.get("type") == "system" and ev.get("subtype") == "stop_hook_summary":
                                event("briefing reply: stop_hook_summary seen — turn 0 done")
                                briefing_done = True
                                break
                        last_count = len(events)
                    if briefing_done:
                        break
                    if seen_assistant and time.monotonic() - last_event_ts > 3.0:
                        event("briefing reply: 3s transcript-quiet after assistant — turn 0 done")
                        briefing_done = True
                        break
                time.sleep(0.3)
            if not briefing_done:
                event(f"WARN: briefing reply not confirmed within {args.briefing_wait}s — proceeding anyway")
            # Settle so turn-0's final assistant block lands before turn-1's send.
            time.sleep(1.0)

        event(f"sending trigger ({len(args.prompt)} chars): {args.prompt!r}")
        bracketed_paste(master_fd, args.prompt)
        send_ts = time.monotonic()
        event("trigger sent — observing")

        post_fired = not bool(args.post_keystroke)

        # Observe loop. Tail the transcript by tracking the seen-event count
        # and emitting any new event of interest.
        end_ts = time.monotonic() + args.observe
        seen_count = 0
        last_pty_len = 0
        last_pty_change = time.monotonic()
        while time.monotonic() < end_ts:
            if proc.poll() is not None:
                event(f"claude exited during observe rc={proc.returncode}")
                break
            # Look for the transcript if we didn't already.
            if transcript_path is None:
                transcript_path = find_new_transcript(projects_dir, baseline)
                if transcript_path is not None:
                    event(f"transcript appeared (late): {transcript_path}")
            if transcript_path is not None:
                events = read_jsonl(transcript_path)
                if len(events) > seen_count:
                    for ev in events[seen_count:]:
                        s = summarize_event(ev)
                        if s:
                            event(s)
                    seen_count = len(events)
            # Scheduled post-keystroke (e.g. Esc to cancel a wedged tool).
            if not post_fired and time.monotonic() - send_ts >= args.post_delay:
                payload = args.post_keystroke.encode("utf-8")
                event(f"writing post-keystroke {payload!r} to PTY")
                os.write(master_fd, payload)
                post_fired = True
            # PTY-quiet check — once a second log if the PTY hasn't changed
            # for a while (the "blocked on a prompt" signature).
            pty_len = len(drain.buf)
            if pty_len != last_pty_len:
                last_pty_len = pty_len
                last_pty_change = time.monotonic()
            quiet_for = time.monotonic() - last_pty_change
            since_send = time.monotonic() - send_ts
            if int(since_send) % 10 == 0 and since_send - int(since_send) < 0.3:
                event(
                    f"t+{since_send:.0f}s: pty_bytes={pty_len} "
                    f"transcript_events={seen_count} pty_quiet_for={quiet_for:.1f}s"
                )
            time.sleep(0.3)

        event(f"observe window ended (proc alive={proc.poll() is None})")

    finally:
        drain.stop()
        # Snapshot everything before teardown.
        (out_dir / "pty.bin").write_bytes(bytes(drain.buf))
        text = ANSI_RE.sub(b"", bytes(drain.buf)).decode("utf-8", errors="replace")
        (out_dir / "pty.txt").write_text(text, encoding="utf-8")
        if transcript_path is not None and transcript_path.exists():
            (out_dir / "transcript.jsonl").write_bytes(transcript_path.read_bytes())
            event(f"copied transcript ({transcript_path.stat().st_size} bytes)")
        else:
            event("no transcript to copy")
        events_log.close()

        # Tear down claude.
        if proc.poll() is None:
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
            os.close(master_fd)
        except OSError:
            pass
        print(f"\nartifacts: {out_dir}")


if __name__ == "__main__":
    run_spike()
