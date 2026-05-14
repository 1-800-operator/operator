"""
Instrumented cold-boot run for inner-claude — answers "what made boot slow?"

Replicates ClaudeCLIProvider._spawn_inner faithfully (same cmd, env, PTY
setup) but WITHOUT operator's 30s ready-timeout / 5s settle / 60s
briefing-timeout — so the real timeline is observed instead of truncated
by operator's impatience. Captures, with timestamps relative to spawn:

  - t0           : Popen returns (process exists)
  - ready.flag   : the instant the SessionStart hook's flag appears
  - briefing sent: when the bracketed-paste briefing hits the PTY
  - turn-0 Stop  : when the Stop hook writes the briefing's reply row
  - PTY output   : every chunk inner-claude emits, timestamped, to a log

The PTY log lets us SEE what claude is doing during the dead time
(MCP connection spinners, plugin load, etc.). Pass --debug to add
`claude --debug` for verbose boot logging (noisier, may add overhead).

Run from the repo root:
    source venv/bin/activate
    python debug/14_22_pty_spike/instrument_boot.py [--debug] [--resume <id>]

Spawns a real `--dangerously-skip-permissions` claude. Costs ~one
briefing turn of subscription tokens. Kills the process group on exit.
"""
import argparse
import fcntl
import json
import os
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import uuid
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO / "src"))

_PTY_ROWS, _PTY_COLS = 40, 120
_OBSERVE_MAX_SECONDS = 300.0          # hard ceiling on the whole run
_READY_HARD_CEILING = 240.0           # how long we'll wait for ready.flag
_BRIEFING_REPLY_CEILING = 120.0       # how long we'll wait for turn-0 Stop
_POLL = 0.05

_ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][AB012]|\r")

_BRIEFING = (
    "Quick context: this is an instrumented boot test. Don't reply to this "
    "message — it's just setup. Wait for the next message."
)


def _set_winsize(fd, rows, cols):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _stamp(t0):
    return f"+{time.monotonic() - t0:7.2f}s"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true", help="add `claude --debug`")
    ap.add_argument("--resume", default=None, help="resume an existing session id")
    ap.add_argument("--cwd", default=None,
                    help="spawn cwd (required for --resume — must match the "
                         "session's original project dir, else claude can't find it)")
    ap.add_argument("--no-briefing", action="store_true",
                    help="skip the briefing paste — pure ready.flag timing, "
                         "zero turns appended to the resumed session (zero pollution)")
    ap.add_argument("--observe-after-ready", type=float, default=0.0, metavar="SECONDS",
                    help="after ready.flag, keep draining the PTY this long and "
                         "report when output settles — proxy for 'TUI actually "
                         "finished rendering / resume JSONL finished parsing'")
    args = ap.parse_args()

    import shutil
    claude = shutil.which("claude")
    if not claude:
        print("ABORT: `claude` not on PATH")
        sys.exit(2)

    # --- session dir + env (mirrors the provider) ---------------------
    session_dir = Path.home() / ".operator" / "sessions" / f"instr_{uuid.uuid4().hex[:12]}"
    session_dir.mkdir(parents=True, exist_ok=True)
    ready_flag = session_dir / "ready.flag"
    replies_path = session_dir / "replies.jsonl"
    pty_log = session_dir / "pty_boot.log"

    if args.cwd:
        cwd = Path(args.cwd)
        if not cwd.is_dir():
            print(f"ABORT: --cwd {cwd} does not exist (claude --resume is cwd-scoped)")
            sys.exit(2)
    else:
        cwd = Path("/tmp") / f"operator_instr_{uuid.uuid4().hex[:8]}"
        cwd.mkdir(parents=True, exist_ok=True)

    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["OPERATOR_SESSION_DIR"] = str(session_dir)

    cmd = [claude, "--dangerously-skip-permissions"]
    if args.debug:
        cmd.append("--debug")
    if args.resume:
        cmd += ["--resume", args.resume]

    print(f"session_dir : {session_dir}")
    print(f"cwd         : {cwd}")
    print(f"cmd         : {' '.join(cmd)}")
    # How many MCP servers are configured — the leading hypothesis for slow boot.
    try:
        mcp = subprocess.run([claude, "mcp", "list"], capture_output=True, text=True, timeout=30)
        n_mcp = len([ln for ln in mcp.stdout.splitlines() if ln.strip() and ":" in ln])
        print(f"MCP servers : ~{n_mcp} configured (from `claude mcp list`)")
    except Exception as e:  # noqa: BLE001
        print(f"MCP servers : (couldn't list — {e})")

    # --- PTY drain with timestamps ------------------------------------
    master_fd, slave_fd = os.openpty() if hasattr(os, "openpty") else __import__("pty").openpty()
    _set_winsize(master_fd, _PTY_ROWS, _PTY_COLS)

    pty_chunks = []           # (elapsed, nbytes)
    stop_drain = threading.Event()

    timeline = []             # (elapsed, label)

    def _drain(t0):
        logf = open(pty_log, "wb")
        try:
            while not stop_drain.is_set():
                try:
                    r, _, _ = select.select([master_fd], [], [], 0.2)
                except (OSError, ValueError):
                    return
                if not r:
                    continue
                try:
                    chunk = os.read(master_fd, 8192)
                except OSError:
                    return
                if not chunk:
                    timeline.append((time.monotonic() - t0, "PTY EOF (process closed its tty)"))
                    return
                el = time.monotonic() - t0
                pty_chunks.append((el, len(chunk)))
                logf.write(f"\n--- {el:7.2f}s (+{len(chunk)}B) ---\n".encode())
                logf.write(chunk)
                logf.flush()
        finally:
            logf.close()

    t0 = time.monotonic()
    proc = subprocess.Popen(
        cmd, cwd=str(cwd),
        stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        start_new_session=True, env=env, close_fds=True,
    )
    os.close(slave_fd)
    timeline.append((0.0, f"Popen returned (pid={proc.pid})"))
    print(f"\n{_stamp(t0)}  spawned pid={proc.pid} — observing...\n")

    drain = threading.Thread(target=_drain, args=(t0,), daemon=True)
    drain.start()

    # --- phase 1: wait for ready.flag ---------------------------------
    ready_at = None
    deadline = t0 + _READY_HARD_CEILING
    last_print = 0
    while time.monotonic() < deadline:
        if ready_flag.exists():
            ready_at = time.monotonic() - t0
            timeline.append((ready_at, "ready.flag appeared (SessionStart hook ran)"))
            print(f"{_stamp(t0)}  >>> ready.flag appeared")
            break
        rc = proc.poll()
        if rc is not None:
            timeline.append((time.monotonic() - t0, f"PROCESS DIED before ready (rc={rc})"))
            print(f"{_stamp(t0)}  !!! process died rc={rc} before ready.flag")
            break
        # progress ping every 10s with how much PTY output we've seen
        el = int(time.monotonic() - t0)
        if el >= last_print + 10:
            last_print = el
            total_b = sum(n for _, n in pty_chunks)
            print(f"{_stamp(t0)}  ...still waiting for ready.flag "
                  f"(PTY output so far: {total_b}B in {len(pty_chunks)} chunks)")
        time.sleep(_POLL)

    # --- phase 1b: observe how far behind the flag real readiness is --
    settled_at = None
    if ready_at is not None and args.observe_after_ready > 0 and proc.poll() is None:
        print(f"{_stamp(t0)}  observing PTY for {args.observe_after_ready:.0f}s "
              f"after ready.flag to find when output settles...")
        obs_deadline = time.monotonic() + args.observe_after_ready
        last_seen = len(pty_chunks)
        last_change = time.monotonic()
        while time.monotonic() < obs_deadline:
            if len(pty_chunks) != last_seen:
                last_seen = len(pty_chunks)
                last_change = time.monotonic()
            elif time.monotonic() - last_change > 2.0 and settled_at is None:
                # 2s of no new PTY output → the TUI/resume-parse churn is done
                settled_at = last_change - t0
                timeline.append((settled_at, "PTY output settled (TUI/resume parse done)"))
                print(f"{_stamp(t0)}  >>> PTY output settled "
                      f"(~{settled_at:.1f}s after spawn, "
                      f"{settled_at - ready_at:.1f}s AFTER ready.flag)")
                break
            time.sleep(_POLL)
        if settled_at is None:
            print(f"{_stamp(t0)}  PTY still churning at end of observation window "
                  f"— TUI not settled within {args.observe_after_ready:.0f}s")

    # --- phase 2: send briefing, wait for turn-0 Stop row -------------
    briefing_sent_at = None
    stop_at = None
    if args.no_briefing:
        if ready_at is not None:
            print(f"{_stamp(t0)}  --no-briefing: ready.flag timed, skipping the paste")
    elif ready_at is not None and proc.poll() is None:
        # tiny settle so the flag-write isn't racing the TUI's input readiness
        time.sleep(0.3)
        payload = _BRIEFING.encode("utf-8")
        os.write(master_fd, b"\x1b[200~")
        time.sleep(0.05)
        os.write(master_fd, payload)
        time.sleep(0.1)
        os.write(master_fd, b"\x1b[201~")
        time.sleep(0.2)
        os.write(master_fd, b"\r")
        briefing_sent_at = time.monotonic() - t0
        timeline.append((briefing_sent_at, "briefing bracketed-paste sent"))
        print(f"{_stamp(t0)}  briefing sent — waiting for turn-0 Stop row...")

        b_deadline = time.monotonic() + _BRIEFING_REPLY_CEILING
        while time.monotonic() < b_deadline:
            try:
                n = sum(1 for _ in replies_path.open("rb"))
            except OSError:
                n = 0
            if n >= 1:
                stop_at = time.monotonic() - t0
                timeline.append((stop_at, "turn-0 Stop row written (briefing was received)"))
                print(f"{_stamp(t0)}  >>> turn-0 Stop row landed")
                break
            if proc.poll() is not None:
                timeline.append((time.monotonic() - t0, "process died waiting for turn-0"))
                break
            time.sleep(_POLL)
        if stop_at is None:
            timeline.append((time.monotonic() - t0,
                             "turn-0 Stop row NEVER appeared — briefing lost"))
            print(f"{_stamp(t0)}  !!! turn-0 Stop never landed — briefing was lost")

    # --- teardown -----------------------------------------------------
    stop_drain.set()
    drain.join(timeout=2)
    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:  # noqa: BLE001
                pass
    try:
        os.close(master_fd)
    except OSError:
        pass

    # --- report -------------------------------------------------------
    print("\n" + "=" * 64)
    print("BOOT TIMELINE")
    print("=" * 64)
    for el, label in timeline:
        print(f"  +{el:7.2f}s  {label}")

    print("\nKEY INTERVALS")
    if ready_at is not None:
        print(f"  spawn → ready.flag       : {ready_at:7.2f}s")
    else:
        print(f"  spawn → ready.flag       : NEVER (within {_READY_HARD_CEILING:.0f}s ceiling)")
    if ready_at is not None and briefing_sent_at is not None and stop_at is not None:
        print(f"  briefing → turn-0 Stop   : {stop_at - briefing_sent_at:7.2f}s")
    elif briefing_sent_at is not None:
        print(f"  briefing → turn-0 Stop   : NEVER (briefing lost)")

    # PTY output rhythm — where did the bytes actually arrive?
    print("\nPTY OUTPUT RHYTHM (when inner-claude was actually emitting)")
    if pty_chunks:
        buckets = {}
        for el, n in pty_chunks:
            buckets.setdefault(int(el // 5) * 5, [0, 0])
            buckets[int(el // 5) * 5][0] += 1
            buckets[int(el // 5) * 5][1] += n
        for sec in sorted(buckets):
            cnt, nb = buckets[sec]
            bar = "#" * min(40, nb // 200 + 1)
            print(f"  +{sec:3d}-{sec+5:3d}s  {nb:7d}B  {cnt:3d} chunks  {bar}")
        gap_start = pty_chunks[0][0]
        biggest_gap = (0.0, 0.0, 0.0)
        prev = 0.0
        for el, _ in pty_chunks:
            if el - prev > biggest_gap[2]:
                biggest_gap = (prev, el, el - prev)
            prev = el
        print(f"  longest silence: +{biggest_gap[0]:.1f}s → +{biggest_gap[1]:.1f}s "
              f"({biggest_gap[2]:.1f}s of no output)")
    else:
        print("  (no PTY output captured at all)")

    print(f"\nfull timestamped PTY dump: {pty_log}")
    print(f"  readable: python -c \"import re,sys; "
          f"d=open('{pty_log}','rb').read(); "
          f"sys.stdout.buffer.write(re.sub(rb'\\x1b\\\\[[0-9;?]*[a-zA-Z]',b'',d))\"")


if __name__ == "__main__":
    main()
