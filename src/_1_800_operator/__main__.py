"""
Operator — AI Meeting Participant
Slip mode entry point. CDP-attaches `claude` to a dedicated Chrome window
running the meeting.

Usage:
    operator slip claude <url>    Attach claude to a slip Chrome session
    operator status               Is operator currently in a meeting?
    operator hangup               Gracefully disconnect the running slip session
    operator doctor               Diagnostic check — is the world ready?
    operator                      Print usage
"""
import os
import subprocess
import sys
from pathlib import Path

from _1_800_operator import config

# Bot names operator's slip dispatch accepts as the first positional after
# `slip`. v1 ships claude only; a future codex bridge would add "codex"
# here. Keeping the set explicit (vs. checking against a registry import)
# keeps the dispatcher self-contained and makes the surface-inference
# behavior easy to reason about.
KNOWN_BOTS = {"claude"}

# Maps surface-marker env vars → the bot that surface implies. When the
# user (or, more often, a model improvising) invokes `operator slip <url>`
# without an explicit bot positional, the first env var present here
# selects the bot. Explicit positional always wins — surface inference
# only fires when the bot slot is missing AND the next arg looks like a
# meet URL (typos like `operator slip cluade <url>` still error cleanly).
# Adding codex later means one new row here (e.g. {"CODEX_RUNNER": "codex"})
# alongside one new entry in KNOWN_BOTS.
SURFACE_BOTS = {
    "CLAUDECODE": "claude",
}


# ── Prevent Ctrl+C from killing child processes ────────────────────
# Playwright's Node.js driver and Chrome are child processes in our
# terminal's foreground process group.  When the user presses Ctrl+C,
# the terminal sends SIGINT to the whole group — killing Chrome
# abruptly. That would yank the user out of the meeting and lose any
# other tabs they had open in slip Chrome.
#
# Fix: put every child in its own session (setsid) so SIGINT only
# reaches our Python process.  We then detach Playwright cleanly
# (browser.close() over CDP is a disconnect, not a process-kill, since
# Playwright didn't launch Chrome), and slip Chrome keeps running so
# the user can stay in the meeting / keep their other tabs.
_OriginalPopenInit = subprocess.Popen.__init__


def _detached_popen_init(self, *args, **kwargs):
    kwargs.setdefault("start_new_session", True)
    _OriginalPopenInit(self, *args, **kwargs)


subprocess.Popen.__init__ = _detached_popen_init


def _kill_orphaned_children():
    """Last-resort cleanup: kill any child processes that survived graceful shutdown."""
    import signal as _sig
    import subprocess as _sp
    import time as _time

    pid = os.getpid()
    try:
        result = _sp.run(
            ["pgrep", "-P", str(pid)],
            capture_output=True, text=True, timeout=3,
            start_new_session=False,
        )
    except Exception:
        return

    child_pids = [int(p) for p in result.stdout.strip().split("\n") if p.strip()]
    if not child_pids:
        return

    import logging
    log = logging.getLogger("operator")

    labeled = []
    for cpid in child_pids:
        try:
            r = _sp.run(
                ["ps", "-o", "command=", "-p", str(cpid)],
                capture_output=True, text=True, timeout=1,
                start_new_session=False,
            )
            cmd = r.stdout.strip().replace("\n", " ")
        except Exception:
            cmd = ""
        labeled.append(f"{cpid} ({cmd})" if cmd else str(cpid))
    log.warning(f"Safety net: killing {len(child_pids)} orphaned child process(es): [{', '.join(labeled)}]")

    for cpid in child_pids:
        try:
            os.kill(cpid, _sig.SIGTERM)
        except ProcessLookupError:
            pass

    _time.sleep(0.5)

    for cpid in child_pids:
        try:
            os.kill(cpid, 0)
            os.kill(cpid, _sig.SIGKILL)
            log.warning(f"Safety net: SIGKILL sent to pid {cpid}")
        except ProcessLookupError:
            pass


_SLIP_LOCK_PATH = Path.home() / ".operator" / "slip.pid"
_AUDIO_HELPER_APP = Path.home() / ".operator" / "bin" / "operator-audio-capture.app"
_AUDIO_HELPER_BIN = _AUDIO_HELPER_APP / "Contents" / "MacOS" / "operator-audio-capture"


def _probe_helper_tcc() -> str:
    """Return the helper's `--probe` JSON, or '' if unrunnable.

    Helper is short-lived (<200ms); probe is safe to call from anywhere.
    Falls back to '' so callers can treat parse failures as "unknown."
    """
    if not _AUDIO_HELPER_BIN.exists():
        return ""
    try:
        r = subprocess.run(
            [str(_AUDIO_HELPER_BIN), "--probe"],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def _preflight_audio_helper_tcc() -> None:
    """First-run TCC warmup for the signed audio helper.

    install.sh runs the equivalent warmup at install time, but TCC state
    can desync afterwards (OS upgrade, manual `tccutil reset`, the bundle
    being re-copied, etc.). This preflight catches that case so users
    don't hit a silent broken-audio slip when they're a minute away from
    a real meeting.

    Flow:
      1. Probe helper's current TCC state.
      2. If both perms granted → no-op (fast path, the common case).
      3. If either missing → invoke `open -W -a` on the helper bundle.
         macOS attributes the prompts to the bundle (not the parent
         IDE/terminal) so the user sees dialogs for "operator-audio-capture."
         The `-W` blocks until the helper exits (~10s via its watchdog).
      4. Re-probe. Warn but don't fail if the user denied — slip can
         still run chat-only; better to let them proceed than block the
         meeting they're trying to join.

    Helper isn't installed → no-op (dev fallback path or skipped install).
    Non-macOS → no-op (slip is mac-only anyway, but defensive).
    """
    if sys.platform != "darwin":
        return
    if not _AUDIO_HELPER_BIN.exists():
        return

    before = _probe_helper_tcc()
    if '"screen_recording":"ok"' in before and '"microphone":"ok"' in before:
        return  # fast path — both granted

    print(
        "macOS audio permissions needed — surfacing dialogs for the audio helper.\n"
        "  Click Allow on each as it appears (Screen Recording + Microphone).\n"
        "  This takes ~10 seconds. The helper exits on its own when done."
    )
    try:
        subprocess.run(
            ["open", "-W", "-n", "-a", str(_AUDIO_HELPER_APP)],
            capture_output=True, timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        pass

    after = _probe_helper_tcc()
    if '"screen_recording":"ok"' in after and '"microphone":"ok"' in after:
        print("✓ Audio permissions granted — proceeding.")
        return

    # Still missing. Most likely cause is user-deny (click Don't Allow)
    # or Apple's prompt cooldown after recent rapid-fire grants/denies.
    # Don't block the slip — degraded slip (silent captions) beats
    # refusing to join the meeting the user is about to attend.
    print(
        "Audio permissions not granted yet — slip will launch but captions will be silent.\n"
        f"  To fix: System Settings → Privacy & Security → Screen Recording (and Microphone)\n"
        f"          → '+' → {_AUDIO_HELPER_APP} → enable\n"
        f"          Then re-run /operator:slip."
    )


def _pid_is_operator(pid: int) -> bool:
    """True iff <pid> is a live operator-slip process.

    Liveness via `kill(pid, 0)` then identity via `ps` argv match. The
    argv check guards against PID reuse — without it, a long-dead
    daemon's reclaimed PID owned by some unrelated process would make
    the singleton guard false-positive forever.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # exists but owned by another user
    try:
        r = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return True  # can't verify identity — fail closed (treat as alive)
    cmd = r.stdout
    return "_1_800_operator" in cmd or "operator slip" in cmd


def _acquire_slip_lock():
    """Take the slip singleton lock at ~/.operator/slip.pid.

    Returns None on success (lock now held by this process). Returns
    the live PID of an existing operator-slip daemon when one is
    already running. Stale lockfiles — PID dead, PID reused by an
    unrelated process, or file contents corrupted — are reclaimed.

    Lockfile-based rather than `pgrep -f` because surface-detect can
    insert `claude` into operator's internal argv list without
    rewriting the OS-visible command line, so a cmdline-grep can miss
    a daemon launched via `operator slip <url>` (no positional). See
    S219 investigation.
    """
    _SLIP_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(
                str(_SLIP_LOCK_PATH),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            try:
                other_pid = int(_SLIP_LOCK_PATH.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                try:
                    _SLIP_LOCK_PATH.unlink()
                except OSError:
                    pass
                continue
            if other_pid == os.getpid():
                return None  # defensive — already ours
            if _pid_is_operator(other_pid):
                return other_pid
            try:
                _SLIP_LOCK_PATH.unlink()
            except OSError:
                pass
            continue
        try:
            os.write(fd, f"{os.getpid()}\n".encode())
        finally:
            os.close(fd)
        return None


def _write_slip_lock(pid: int) -> None:
    """Overwrite the slip lockfile with <pid>. Called by the daemonize
    parent after fork so the lockfile points at the surviving child."""
    try:
        _SLIP_LOCK_PATH.write_text(f"{pid}\n", encoding="utf-8")
    except OSError:
        pass


def _release_slip_lock() -> None:
    """Delete the slip lockfile iff this process owns it. Idempotent.

    Ownership check matters because the daemonize parent rewrites the
    lockfile to point at the surviving child before exiting — so a
    parent-side release would orphan the child. Only the recorded owner
    deletes the file. _run_hangup bypasses this by calling unlink
    directly after it's verified the holder is dead.
    """
    try:
        owner = int(_SLIP_LOCK_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    if owner != os.getpid():
        return
    try:
        _SLIP_LOCK_PATH.unlink()
    except OSError:
        pass


def _print_usage():
    print("Usage:")
    print("  operator slip claude <url>          Slip — guarded by default; permission asks bridged to meeting chat")
    print("  operator slip-strict claude <url>   Slip — guarded, every prompt requires @claude (no sticky window)")
    print("  operator slip-yolo claude <url>     Slip — no permission asks, every chat message goes to claude")
    print("  operator wiretap <url>              Passive recording — no bot, just capture chat + captions")
    print("  operator status                     Is operator currently in a meeting?")
    print("  operator hangup                     Gracefully disconnect the running slip session")
    print("  operator doctor                     Diagnostic check — is the world ready?")
    print()
    print("Flags:")
    print("  --resume-session <id>               Bridge an existing Claude Code session into slip")


def main():
    # Strip group/world bits from anything we create under ~/.operator/.
    # Files are born 0o600 and dirs 0o700 with this mask, closing the
    # mkdir → chmod race for callers that don't pass mode= explicitly.
    # Only touches files this process creates; existing files keep their
    # current perms.
    os.umask(0o077)
    argv = sys.argv[1:]

    if not argv or argv[0] in ("-h", "--help"):
        _print_usage()
        return 0

    first = argv[0]

    # User-facing argv errors exit 0 to stdout (rather than the natural
    # exit 2 to stderr). The Claude Code desktop-app harness classifies
    # any `!` block exiting non-zero as "Shell command failed" and wraps
    # the output in a DO-NOT-RESPOND caveat — invisible to the user.
    # stdout + exit 0 lets the message reach claude; the SKILL.md body
    # already discriminates success vs error by output shape, not exit
    # code. Trades terminal scriptability of $? for desktop-app visibility,
    # which is the primary surface.
    if first == "doctor":
        if len(argv) != 1:
            print("Usage: operator doctor\n")
            _print_usage()
            return 0
        from _1_800_operator.pipeline.doctor import run_doctor
        return run_doctor()

    if first == "status":
        if len(argv) != 1:
            print("Usage: operator status\n")
            _print_usage()
            return 0
        return _run_status()

    if first == "hangup":
        if len(argv) != 1:
            print("Usage: operator hangup\n")
            _print_usage()
            return 0
        return _run_hangup()

    if first == "wiretap":
        if len(argv) != 2:
            print("Usage: operator wiretap <https://meet.google.com/xxx-xxxx-xxx>\n")
            _print_usage()
            return 0
        url = argv[1]
        if not url.startswith(("http://", "https://")):
            print(f"wiretap requires a Meet URL: got {url!r}\n")
            _print_usage()
            return 0
        return _run_wiretap(url)

    if first in ("slip", "slip-strict", "slip-yolo"):
        # Default slip is now guarded (formerly /operator:slip-guarded
        # behavior); slip-strict is guarded + requires @claude every
        # turn; slip-yolo is unattended with no permission asks. The
        # pre-S238 `slip-guarded` name is gone — pre-launch hard cutover.
        mode = {"slip": "slip", "slip-strict": "strict", "slip-yolo": "yolo"}[first]
        if len(argv) < 2:
            print(f"Usage: operator {first} claude <https://meet.google.com/xxx-xxxx-xxx>\n")
            _print_usage()
            return 0
        name = argv[1]
        # Surface-detect inference: the desktop-app model regularly
        # drops the `claude` positional and invokes `operator slip <url>`
        # because in single-bot v1 it reads as ceremony. Instead of
        # fighting that improvisation in skill prose (we've tried — it
        # loses), absorb it here: if the bot slot is missing AND the
        # next arg looks like a URL AND we can identify the invoking
        # surface from an env var, insert the surface's implied bot.
        # Explicit positional always wins; this branch never fires when
        # the user/model supplied a bot. Typos like `slip cluade <url>`
        # fall through to the "Unknown bot" error because `cluade` doesn't
        # look URL-shaped. Same inference applies to slip-guarded.
        if name not in KNOWN_BOTS and name.startswith(("http://", "https://")):
            inferred = _infer_bot_from_surface()
            if inferred:
                argv.insert(1, inferred)
                name = inferred
        if name not in KNOWN_BOTS:
            supported = ", ".join(sorted(KNOWN_BOTS))
            print(f"Unknown bot: {name!r}. Supported: {supported}.\n")
            _print_usage()
            return 0
        rest = _consume_yolo(argv[2:])
        return _run_slip(name, rest, mode=mode)

    if first.startswith("-"):
        print(f"Unknown option: {first}\n")
        _print_usage()
        return 0

    print(f"Unknown subcommand: {first!r}\n")
    _print_usage()
    return 0


def _daemonize_and_announce(url):
    """Self-daemonize so the caller gets synchronous feedback.

    The Bash tool's response captures the foreground command's stdout
    up to exit. When the model fires `operator slip claude <url> &` the
    foreground exits with no output before operator has a chance to
    print anything, so the model sees "Bash completed with no output"
    and has no synchronous signal of success or failure. Same problem
    when the model omits the `&` — operator's long-running work blocks
    the tool call until the meeting ends, which is worse.

    Self-daemonize fixes both. After all synchronous validation passes
    (arg parse, singleton guard, claude preflight), fork. The parent
    prints one informative line to stdout and exits 0; the Bash tool
    captures that line as the command's response. The child detaches
    from the controlling session (setsid), redirects its stdio to
    /dev/null, and continues with the actual meeting work. The
    operator log (/tmp/operator.log) keeps detailed activity; the
    Bash response stays a single clean line.

    Only fires when stdout is captured (non-TTY). Terminal-direct
    users (someone running `operator slip claude <url>` in iTerm)
    keep their live console banners — isatty() == True takes the
    pre-existing in-process path. This preserves the debug-friendly
    foreground mode for development without sacrificing the desktop-
    app UX.
    """
    import os as _os
    if sys.stdout.isatty():
        return  # in-terminal: keep live banners, no fork
    sys.stdout.flush()
    sys.stderr.flush()
    pid = _os.fork()
    if pid > 0:
        # Parent — rewrite the slip lockfile so it points at the
        # surviving child (we acquired the lock pre-fork with our own
        # PID, which is about to die). Order matters: rewrite first so
        # any process arriving in the gap between our exit and our
        # rewrite still sees a live owner. Then emit the synchronous
        # status line and exit clean. The Bash tool's stdout capture
        # closes at this exit, so the model gets exactly this line as
        # the command's response.
        _write_slip_lock(pid)
        print(
            f"operator: joining {url} (pid {pid}) — "
            f"use /operator:status to check, /operator:hangup to end early"
        )
        sys.stdout.flush()
        _os._exit(0)
    # Child — detach from the controlling terminal/session so the
    # shell that spawned us (or its parent, in the Bash-tool case)
    # can exit without sending SIGHUP to us. Redirect stdio to
    # /dev/null since nothing should read them now — operator's own
    # log handler writes to /tmp/operator.log.
    _os.setsid()
    devnull = _os.open(_os.devnull, _os.O_RDWR)
    _os.dup2(devnull, 0)
    _os.dup2(devnull, 1)
    _os.dup2(devnull, 2)
    if devnull > 2:
        _os.close(devnull)


def _infer_bot_from_surface():
    """Return the bot implied by the surface that invoked operator, or None.

    Walks SURFACE_BOTS in order and returns the first match. The slip
    dispatcher calls this only when the user (most often, a model) omitted
    the bot positional AND the next arg looks like a meet URL — so a hit
    here cleanly absorbs the model's most common improvisation
    (`operator slip <url>` without `claude`). When more than one surface
    env is set simultaneously (e.g. nested invocations), the dict's
    iteration order picks the winner — deterministic on CPython 3.7+.
    """
    for env_var, bot in SURFACE_BOTS.items():
        if os.environ.get(env_var):
            return bot
    return None


def _consume_yolo(args):
    """Strip `--yolo` from the argv list and return the remainder.

    `--yolo` is a no-op now — the inner-claude spawn carries
    `--dangerously-skip-permissions` unconditionally (see
    providers/claude_cli.py:_build_cmd). The flag is still consumed here
    so the plugin slash command passing it doesn't trip "unknown option".
    """
    return [a for a in args if a != "--yolo"]


def _read_current_meeting_url():
    """Return the meet URL of the currently-active slip session, or None.

    Reads the .current_meeting marker (written at meeting-join, deleted at
    leave). Used by the singleton-guard error message so the user sees
    *which* meeting operator is in rather than just "already running (pid
    X)". Returns None on any error — the caller falls back to the pid form.
    """
    import json
    marker = Path.home() / ".operator" / ".current_meeting"
    if not marker.exists():
        return None
    try:
        jsonl_path = Path(marker.read_text(encoding="utf-8").strip())
        with jsonl_path.open("r", encoding="utf-8") as f:
            first = f.readline()
        meta = json.loads(first) if first else {}
        return meta.get("meet_url")
    except (OSError, json.JSONDecodeError):
        return None


def _run_status():
    """Print whether operator is currently in a meeting.

    Reads the ~/.operator/.current_meeting marker written by _run_slip at
    join and deleted at shutdown. If the marker exists and points at a
    JSONL whose meta line carries meet_url, print "in meeting <url>";
    otherwise "not in a meeting". No --verbose flavor — the plugin's
    status skill just wants a one-liner.
    """
    import json
    marker = Path.home() / ".operator" / ".current_meeting"
    if not marker.exists():
        print("not in a meeting")
        return 0
    try:
        jsonl_path = Path(marker.read_text(encoding="utf-8").strip())
        with jsonl_path.open("r", encoding="utf-8") as f:
            first = f.readline()
        meta = json.loads(first) if first else {}
        url = meta.get("meet_url")
    except (OSError, json.JSONDecodeError):
        url = None
    if url:
        print(f"in meeting {url}")
    else:
        print("in meeting")
    return 0


def _run_hangup():
    """Send SIGTERM to the running operator-slip daemon, if any.

    Source of truth is the slip lockfile (~/.operator/slip.pid) written
    at startup and rewritten post-fork to point at the surviving child.
    If the lockfile is missing, corrupt, or its PID isn't alive +
    identified as operator, treat as no-daemon-running and clean any
    stale .current_meeting marker so `operator status` doesn't lie.

    The slip process's signal handler does the graceful teardown:
    ChatRunner.stop(), connector.leave() (CDP detach — does NOT quit
    Chrome or close the chat panel, per spec), and releases the lock.
    """
    import signal as _sig
    import time as _time
    marker = Path.home() / ".operator" / ".current_meeting"

    pid = None
    if _SLIP_LOCK_PATH.exists():
        try:
            pid = int(_SLIP_LOCK_PATH.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = None
        if pid is not None and not _pid_is_operator(pid):
            # Stale — reclaim so subsequent slip attempts succeed.
            try:
                _SLIP_LOCK_PATH.unlink()
            except OSError:
                pass
            pid = None

    if pid is None:
        if marker.exists():
            try:
                marker.unlink()
            except OSError:
                pass
        print("not in a meeting")
        return 0

    try:
        os.kill(pid, _sig.SIGTERM)
    except ProcessLookupError:
        # Raced with the daemon's own exit between our liveness check
        # and the kill. Equivalent to clean shutdown — fall through.
        try:
            _SLIP_LOCK_PATH.unlink()
        except OSError:
            pass
        print("not in a meeting")
        return 0

    # Brief wait so the slip process can run its shutdown handler
    # (connector.leave waits up to 10s for the browser thread). We poll
    # only for ~3s — long enough to confirm exit on the happy path, not
    # so long that the plugin skill feels stuck.
    deadline = _time.monotonic() + 3.0
    while _time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        _time.sleep(0.2)
    print("hung up (1 session)")
    return 0


def _run_slip(name, rest, *, mode="slip"):
    """slip mode — launch a dedicated Chrome window for the meeting and
    CDP-attach claude to it.

    Slip Chrome lives at ~/.operator/slip_profile/ — operator-owned,
    separate from the user's main browser. First run: user signs into
    Google in slip Chrome once, cookies persist for future sessions.
    User's main Chrome is never touched.

    Caller must have already filtered for `name == "claude"` (the main
    dispatcher does this at argv parse time).

    `mode` is one of:
      - "slip"   default. Guarded — operator intercepts permission asks
                  via the PreToolUse hook and bridges them to meeting
                  chat ending in "— OK?". PermissionClassifier sidecar
                  active. ChatRunner runs with a sticky conversation
                  window (`?` keeps it open indefinitely).
      - "strict" guarded + every prompt requires @claude. No
                  continuation window.
      - "yolo"   no permission asks — inner-claude runs unattended. No
                  classifier sidecar. ChatRunner forwards every chat
                  message to claude (no trigger gating).
    """
    guarded = mode in ("slip", "strict")
    # Resume-session resolution has two tiers (see logic below):
    #   1. --resume-session <id> on the command line.
    #   2. CLAUDE_CODE_SESSION_ID env var (set by both terminal Claude Code
    #      and the desktop app — verified S224 with CLAUDE_CODE_ENTRYPOINT=
    #      claude-desktop). Neither → spawn fresh; the inner-claude inherits
    #      no prior context, which is the predictable safe default.
    # An older third tier scanned the desktop app's
    # ~/Library/Application Support/Claude/claude-code-sessions/ catalog for
    # a recent cliSessionId. It was load-bearing in S217 when the desktop
    # app neither propagated the env var nor honored the slash command's
    # --resume-session flag. Both of those have since started working, so
    # the scan became dead weight — and a footgun (S224 stale-session bug:
    # the catalog kept pointing at a conversation the CLI store no longer
    # had, and every meeting silently failed with `error_during_execution`
    # until we tracked it down).
    # See comment block on user-facing errors in main(): pre-daemonize
    # rejections exit 0 to stdout so the desktop-app harness surfaces them.
    url = None
    resume_session_id = None
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--resume-session":
            if i + 1 >= len(rest):
                print("--resume-session requires a session id")
                return 0
            resume_session_id = rest[i + 1]
            i += 2
            continue
        if arg.startswith("--resume-session="):
            resume_session_id = arg.split("=", 1)[1]
            i += 1
            continue
        if arg.startswith("-"):
            print(f"Unknown flag: {arg}")
            return 0
        if url is None:
            url = arg
            i += 1
            continue
        print(f"Unexpected argument: {arg}")
        return 0

    if not url:
        print(
            "slip requires a Meet URL: operator slip claude <https://meet.google.com/xxx-xxxx-xxx>"
        )
        return 0

    if sys.platform != "darwin":
        print("slip mode is currently macOS-only.")
        return 0

    # Singleton guard — refuse to start if another operator slip is already
    # running. Without this, the desktop app can stack operators on the same
    # slip Chrome (e.g. when the model retries a failed dispatch), each
    # spawning its own audio helper and writing to the same meeting JSONL.
    #
    # Output goes to STDOUT and exits 0 on purpose. The Claude Code desktop-
    # app harness classifies any `!` block that exits non-zero as "Shell
    # command failed" and wraps the output in a DO-NOT-RESPOND caveat — so
    # exit-2-to-stderr (the natural CLI shape) makes the rejection invisible
    # to the user. stdout + exit 0 lets the harness inject the message
    # normally, and the SKILL.md's "Error" branch matches by the line shape
    # ("operator slip is already running…") regardless of exit code. The
    # success-path output shape is what callers parse anyway.
    other_pid = _acquire_slip_lock()
    if other_pid is not None:
        existing_url = _read_current_meeting_url()
        if existing_url:
            print(
                f"operator slip is already running — already in {existing_url}. "
                f"Use /operator:hangup to leave that meeting first, then retry."
            )
        else:
            print(
                f"operator slip is already running (pid {other_pid}). "
                f"Use /operator:hangup to leave that meeting first, then retry."
            )
        return 0

    # claude binary preflight — fail loud and early; no browser dance,
    # no config load if claude isn't installed, logged out, or too old.
    # `reason` is self-contained (it names the specific fix), so the
    # message just relays it.
    from _1_800_operator.pipeline.claude_code_import import (
        claude_code_installed_and_logged_in,
    )
    ok, reason = claude_code_installed_and_logged_in()
    if not ok:
        _release_slip_lock()
        print(
            f"slip claude can't start:\n"
            f"  {reason}\n"
            f"Fix that and re-run. (`operator doctor` runs the full check.)"
        )
        return 0

    # First-run TCC warmup for the signed audio helper. No-op when both
    # perms are already granted (common case after install.sh's warmup);
    # surfaces dialogs synchronously when not, so the user clicks Allow
    # BEFORE we daemonize + join the meeting (otherwise the audio helper
    # dies silently on first use). Best-effort: prints a warn and
    # continues if user denies — degraded slip (silent captions) beats
    # refusing to join the meeting they're about to attend.
    _preflight_audio_helper_tcc()

    # All synchronous validation has passed. Hand the caller (Bash tool,
    # shell, etc.) a one-line success acknowledgement, then detach so
    # the long-running meeting work doesn't block the response. See
    # _daemonize_and_announce docstring for why.
    #
    # Clear last_failure.json — a new slip is the user's "try again";
    # doctor's "last meeting failure" section means "since you started
    # the most recent slip", not "ever". A fresh failure during this
    # meeting will overwrite the (now-absent) file.
    try:
        Path(config.LAST_FAILURE_PATH).unlink(missing_ok=True)
    except OSError:
        pass
    _daemonize_and_announce(url)

    import logging
    import signal
    import time as _time

    logging.basicConfig(
        filename="/tmp/operator.log",
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = logging.getLogger("operator")

    from _1_800_operator.bridges import claude as claude_bridge
    from _1_800_operator.connectors.attach_adapter import AttachAdapter, SlipAttachError
    from _1_800_operator.pipeline import ui
    from _1_800_operator.pipeline.chat_runner import ChatRunner
    from _1_800_operator.pipeline.llm import LLMClient
    from _1_800_operator.pipeline.meeting_record import MeetingRecord, slug_from_url
    from _1_800_operator.pipeline.providers import build_provider

    t_start = _time.monotonic()

    # Build meeting record up-front — URL is known, no meet.new resolution
    # gymnastics needed. The transcript MCP server (spawned by claude via
    # --mcp-config) reads from this path.
    slug = slug_from_url(url)
    meeting_record = MeetingRecord(slug=slug, meta={"meet_url": url, "mode": mode})

    # Two-tier resume-session resolution (see comment block at top of _run_slip).
    resume_source = None
    if resume_session_id:
        resume_source = "flag"
    if not resume_session_id:
        resume_session_id = os.environ.get("CLAUDE_CODE_SESSION_ID") or None
        if resume_session_id:
            resume_source = "env"

    provider = build_provider(resume_session_id=resume_session_id, guarded=guarded)
    # Fire pre_warm now, before the join sequence (Chrome attach + lobby
    # wait + whisper model load — typically ~30s). pre_warm spawns the
    # interactive claude and runs the briefing round-trip; doing it now
    # means claude's Node boot + MCP attach + --resume JSONL parse +
    # briefing land during the join window instead of being charged to
    # the first @mention. Without this, a user who @mentions within ~2s
    # of meeting entry pays a cold-init tax (observed S221).
    import threading as _threading
    _threading.Thread(target=provider.pre_warm, daemon=True).start()

    # Guarded mode: spin up the PermissionClassifier sidecar in
    # parallel. Its own ~6s boot+settle hides inside the same join
    # window as the main provider's pre_warm, so the first chat reply
    # to a permission question doesn't pay an init tax. Off in default
    # (yolo-on) mode — the main provider spawns with
    # --dangerously-skip-permissions, PermissionRequest never fires,
    # the classifier would be dead weight.
    classifier = None
    if guarded:
        from _1_800_operator.pipeline.classifier import PermissionClassifier
        classifier = PermissionClassifier()
        _threading.Thread(target=classifier.pre_warm, daemon=True).start()
        log.info("slip: guarded mode — classifier sidecar spawning in parallel")

    llm = LLMClient(provider)
    llm.set_record(meeting_record)
    if resume_session_id:
        log.info(
            f"slip: bridging existing Claude Code session {resume_session_id} "
            f"into meeting (source={resume_source})"
        )
    else:
        log.info("slip: no resume session — starting fresh")

    # Active-meeting marker — useful for any static-config MCPs that need
    # the active meeting JSONL path.
    try:
        marker = Path.home() / ".operator" / ".current_meeting"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(meeting_record.path), encoding="utf-8")
    except OSError as e:
        log.warning(f"could not write current-meeting marker: {e}")

    connector = AttachAdapter(reply_prefix=claude_bridge.REPLY_PREFIX_SLIP)

    # Wire whisper utterances → meeting record. Direct-write: each
    # callback delivers ONE finalized utterance, not a streaming partial.
    def _on_utterance(speaker: str, text: str, timestamp: float) -> None:
        try:
            meeting_record.append(speaker, text, kind="caption", timestamp=timestamp)
        except Exception as exc:
            log.warning(f"slip: append caption failed: {exc}")

    connector.set_caption_callback(_on_utterance)

    ui.say("Launching slip Chrome…")
    try:
        connector.join(url)
    except SlipAttachError as e:
        ui.err(str(e))
        # Reap the pre-warmed subprocesses — this fail path bypasses
        # _shutdown(), so without explicit stop() calls the parked
        # claude (and the classifier sidecar in guarded mode) would
        # survive the parent's exit (start_new_session=True children
        # are not killed on parent death).
        try:
            provider.stop()
        except Exception:
            pass
        if classifier is not None:
            try:
                classifier.stop()
            except Exception:
                pass
        return 2

    log.info(f"TIMING setup={_time.monotonic() - t_start:.1f}s")
    runner = ChatRunner(
        connector,
        llm,
        meeting_record=meeting_record,
        permission_classifier=classifier,
        mode=mode,
    )

    _shutdown_called = False

    def _shutdown(signum=None, frame=None):
        nonlocal _shutdown_called
        if _shutdown_called:
            return
        _shutdown_called = True
        if signum:
            log.info(f"Received signal {signum} — shutting down")
        # Order matters here. `runner.stop()` blocks 5-12s waiting for
        # the inner-claude PTY to drain + the classifier sidecar to
        # exit. We don't want /operator:status to lie or /operator:slip
        # to refuse with "already running" during that window. The
        # marker and the slip lockfile are pure lookup signals (nothing
        # in the teardown path reads them), so release them FIRST —
        # subsequent slip/status calls then see truth immediately. The
        # roster file is different: chat_runner writes it every
        # PARTICIPANT_CHECK_INTERVAL, so unlinking it before stop()
        # races with a re-write. Unlink it AFTER runner.stop() to be
        # safe.
        try:
            marker = Path.home() / ".operator" / ".current_meeting"
            if marker.exists():
                marker.unlink()
        except OSError:
            pass
        _release_slip_lock()
        runner.stop()
        # Bake the cumulative attendee list + the meeting_end terminator
        # into the JSONL so post-meeting lookup tools (find_meetings,
        # list_meetings) can answer "who was on the X meeting?" against
        # disk-resident state. After runner.stop() the chat thread is
        # joined so _attended_participants is stable. Best-effort — never
        # let close() failures block teardown.
        try:
            attended = sorted(getattr(runner, "_attended_participants", set()))
            self_name = getattr(runner, "_last_self_name", "") or ""
            meeting_record.close(attended=attended, self_name=self_name)
        except Exception as e:
            log.warning(f"MeetingRecord.close() failed: {e}")
        try:
            roster = Path(config.CURRENT_MEETING_PARTICIPANTS_PATH)
            if roster.exists():
                roster.unlink()
        except OSError:
            pass
        connector.leave()
        _kill_orphaned_children()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        log.info(f"Starting Operator slip mode — attached to {url}")
        runner.run(url)
    except KeyboardInterrupt:
        log.info("Interrupted — detaching")
    finally:
        _shutdown()
        ui.ok("Detached — slip Chrome stays open so the meeting can continue. Goodbye.")
    return 0


def _run_wiretap(url):
    """wiretap mode — passive meeting recording, no inner-claude.

    Attaches to slip Chrome, joins the meeting, captures chat + captions
    + participant roster into the meeting JSONL, exits when the meeting
    ends (auto-leave / hangup / Chrome close). The MCP `find_meetings`
    and `list_meeting_record` tools can then be used from any Claude
    Code session to recall what was said.

    No bot positional, no permission UI, no chat sends. Shares the
    singleton lock with the speak-modes (one operator per host).
    """
    if sys.platform != "darwin":
        print("wiretap mode is currently macOS-only.")
        return 0

    other_pid = _acquire_slip_lock()
    if other_pid is not None:
        existing_url = _read_current_meeting_url()
        if existing_url:
            print(
                f"operator is already running — already in {existing_url}. "
                f"Use /operator:hangup to leave that meeting first, then retry."
            )
        else:
            print(
                f"operator is already running (pid {other_pid}). "
                f"Use /operator:hangup to leave that meeting first, then retry."
            )
        return 0

    # Wiretap captures captions, so the audio helper TCC perms still
    # matter. Same warmup path as slip.
    _preflight_audio_helper_tcc()

    try:
        Path(config.LAST_FAILURE_PATH).unlink(missing_ok=True)
    except OSError:
        pass
    _daemonize_and_announce(url)

    import logging
    import signal
    import time as _time

    logging.basicConfig(
        filename="/tmp/operator.log",
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = logging.getLogger("operator")

    from _1_800_operator.bridges import claude as claude_bridge
    from _1_800_operator.connectors.attach_adapter import AttachAdapter, SlipAttachError
    from _1_800_operator.pipeline import ui
    from _1_800_operator.pipeline.chat_runner import ChatRunner
    from _1_800_operator.pipeline.meeting_record import MeetingRecord, slug_from_url

    t_start = _time.monotonic()

    slug = slug_from_url(url)
    meeting_record = MeetingRecord(slug=slug, meta={"meet_url": url, "mode": "wiretap"})

    try:
        marker = Path.home() / ".operator" / ".current_meeting"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(meeting_record.path), encoding="utf-8")
    except OSError as e:
        log.warning(f"could not write current-meeting marker: {e}")

    # The reply prefix is unused in wiretap (send_chat never fires) but
    # AttachAdapter requires the constructor arg.
    connector = AttachAdapter(reply_prefix=claude_bridge.REPLY_PREFIX_SLIP)

    def _on_utterance(speaker: str, text: str, timestamp: float) -> None:
        try:
            meeting_record.append(speaker, text, kind="caption", timestamp=timestamp)
        except Exception as exc:
            log.warning(f"wiretap: append caption failed: {exc}")

    connector.set_caption_callback(_on_utterance)

    ui.say("Launching slip Chrome for wiretap…")
    try:
        connector.join(url)
    except SlipAttachError as e:
        ui.err(str(e))
        return 2

    log.info(f"TIMING setup={_time.monotonic() - t_start:.1f}s")
    runner = ChatRunner(
        connector,
        llm=None,
        meeting_record=meeting_record,
        permission_classifier=None,
        mode="wiretap",
    )

    _shutdown_called = False

    def _shutdown(signum=None, frame=None):
        nonlocal _shutdown_called
        if _shutdown_called:
            return
        _shutdown_called = True
        if signum:
            log.info(f"Received signal {signum} — shutting down")
        try:
            marker = Path.home() / ".operator" / ".current_meeting"
            if marker.exists():
                marker.unlink()
        except OSError:
            pass
        _release_slip_lock()
        runner.stop()
        try:
            attended = sorted(getattr(runner, "_attended_participants", set()))
            self_name = getattr(runner, "_last_self_name", "") or ""
            meeting_record.close(attended=attended, self_name=self_name)
        except Exception as e:
            log.warning(f"MeetingRecord.close() failed: {e}")
        try:
            roster = Path(config.CURRENT_MEETING_PARTICIPANTS_PATH)
            if roster.exists():
                roster.unlink()
        except OSError:
            pass
        connector.leave()
        _kill_orphaned_children()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        log.info(f"Starting Operator wiretap mode — attached to {url}")
        runner.run(url)
    except KeyboardInterrupt:
        log.info("Interrupted — detaching")
    finally:
        _shutdown()
        ui.ok("Detached — meeting record saved. Goodbye.")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
