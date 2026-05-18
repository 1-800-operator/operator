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
import json
import logging
import os
import subprocess
import sys
import time as _startup_time
from pathlib import Path

# Module-load timestamp — anchors the `TIMING slip_startup` line so we can
# attribute Python import overhead separately from operator's own preflight
# work. Stamp here (top of __main__) rather than inside main() so transitive
# imports above are counted as boot, not as dispatch.
_T_MODULE_LOAD = _startup_time.monotonic()

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
_AUDIO_HELPER_APP = Path.home() / ".operator" / "bin" / "Operator.app"
_AUDIO_HELPER_BIN = _AUDIO_HELPER_APP / "Contents" / "MacOS" / "Operator"
_HELPER_BUNDLE_ID = "com.1-800-operator.audio-capture"
_LSREGISTER = Path(
    "/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/"
    "LaunchServices.framework/Versions/A/Support/lsregister"
)

# macOS System Settings deep-link URLs (macOS 13+). When the user has
# explicitly denied a TCC service, the only path back is manual re-enable
# in Settings — `CGRequestScreenCaptureAccess` / `AVCaptureDevice.requestAccess`
# both no-op silently after explicit deny. Surfacing the right pane saves
# the user from hunting through Privacy & Security.
_SETTINGS_DEEP_LINK_SCREEN_CAPTURE = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
)
_SETTINGS_DEEP_LINK_MICROPHONE = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
)


def _probe_helper_tcc() -> str:
    """Return the helper's `--probe` JSON, or '' if unrunnable.

    Spawns the probe via `_disclaimed_spawn` so the child runs as its own
    TCC-responsible process. Without disclaim, `CGPreflightScreenCaptureAccess()`
    inside the probe checks against the parent IDE/terminal's grant rather
    than the helper bundle's — and on a freshly-installed setup where the
    IDE isn't granted but the helper is, the probe would falsely report
    "denied" and trigger an unnecessary warmup. See
    debug/14_31_tcc_warmup_spike/ for the empirical measurement that
    pinned this attribution down.

    Helper is short-lived (<200ms); probe is safe to call from anywhere.
    Falls back to '' so callers can treat parse failures as "unknown."
    """
    if not _AUDIO_HELPER_BIN.exists():
        return ""
    try:
        # Minimal env — the helper has no auth needs. Without this it'd
        # inherit the full shell env including API keys / cloud creds /
        # GitHub tokens. See _disclaimed_spawn.minimal_helper_env docstring.
        from _1_800_operator.pipeline._disclaimed_spawn import (
            minimal_helper_env,
            spawn_disclaimed,
        )
        p = spawn_disclaimed(
            [str(_AUDIO_HELPER_BIN), "--probe"],
            env=minimal_helper_env(),
        )
        try:
            out_bytes = p.stdout.read(4096)
            p.wait(timeout=5)
        finally:
            if p.poll() is None:
                try:
                    p.kill()
                    p.wait(timeout=2)
                except Exception:
                    pass
        return out_bytes.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _parse_probe_status(probe: str) -> tuple[str, str]:
    """Extract (screen_recording, microphone) status strings from probe JSON.

    Returns ('unknown', 'unknown') on parse failure so callers can treat
    unknown the same as not-yet-prompted (run the warmup, see what happens).
    """
    if not probe:
        return ("unknown", "unknown")
    try:
        d = json.loads(probe)
    except (json.JSONDecodeError, ValueError):
        return ("unknown", "unknown")
    return (
        str(d.get("screen_recording", "unknown")),
        str(d.get("microphone", "unknown")),
    )


def _ls_cleanup_stale_helpers() -> int:
    """Unregister Launch Services entries for our bundle ID that don't point
    at the canonical install path. Returns count of paths unregistered.

    Why this exists: every `uv tool install --reinstall` extracts a fresh
    wheel into a new `~/.cache/uv/archive-v0/<hash>/` dir, and Launch
    Services auto-registers any `.app` it finds during directory scans.
    Over a development cycle that's dozens of stale copies all claiming
    the same bundle ID (`com.1-800-operator.audio-capture`). When the
    TCC warmup dialog click attaches to "whichever copy LS resolved
    first," the grant can land on a stale archive copy instead of our
    canonical `~/.operator/bin/Operator.app`, and the runtime helper
    invocation silently fails because its code-requirement doesn't match
    the granted entry. The S240 debugging arc hit this with 36 stale
    registrations; cleanup fixed it instantly.

    Idempotent — safe to call on every cold start. Best-effort; failures
    are swallowed (LS cleanup is defense, not load-bearing).
    """
    if not _LSREGISTER.exists():
        return 0
    try:
        r = subprocess.run(
            [str(_LSREGISTER), "-dump"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return 0
    if r.returncode != 0:
        return 0

    # lsregister -dump emits sections with "path: <path>" headers; within
    # each section, our bundle ID appears as `identifier: <bid>` and inside
    # CFBundleIdentifier strings. Match either form by substring within
    # the section bounded by the next "path:" line.
    paths_for_bundle: set[str] = set()
    current_path: str | None = None
    for raw in r.stdout.splitlines():
        line = raw.strip()
        if line.startswith("path:"):
            current_path = line[len("path:"):].strip()
        elif _HELPER_BUNDLE_ID in line and current_path:
            paths_for_bundle.add(current_path)

    canonical = str(_AUDIO_HELPER_APP)
    stale = [p for p in paths_for_bundle if p != canonical]

    count = 0
    for p in stale:
        try:
            subprocess.run(
                [str(_LSREGISTER), "-u", p],
                capture_output=True, timeout=5,
            )
            count += 1
        except (subprocess.SubprocessError, OSError):
            pass
    return count


def _open_settings_pane(url: str) -> None:
    """Open a System Settings deep link. Best-effort."""
    try:
        subprocess.run(["open", url], capture_output=True, timeout=3)
    except (subprocess.SubprocessError, OSError):
        pass


def _preflight_audio_helper_tcc() -> None:
    """First-run / per-slip TCC warmup for the signed audio helper.

    install.sh runs the equivalent warmup at install time, but TCC state
    can desync afterwards (OS upgrade, manual `tccutil reset`, the user
    toggling perms off in Settings, the bundle being re-copied, etc.).
    This preflight catches that case so users don't hit a silent broken-
    audio slip when they're a minute away from a real meeting.

    Flow:
      1. **LS cleanup** — `lsregister -u` any cached duplicate
         registrations of our bundle ID. Without this, a TCC grant click
         can attach to a stale wheel-cache copy and the runtime helper
         silently runs with the wrong code requirement (S240 hit 36
         duplicates). Idempotent, ~100ms.
      2. **Probe** the helper's TCC state via `_disclaimed_spawn` (so the
         probe answers against the helper's own identity, not the parent
         IDE's — see `_probe_helper_tcc` docstring).
      3. **Both granted → no-op fast path** (the common case).
      4. **`not_determined` or `unknown` → run the warmup**. Invoke
         `open -W -n -a` on the helper bundle. macOS surfaces dialogs
         attributed to "Operator" (verified in
         debug/14_31_tcc_warmup_spike/). The `-W` blocks until the helper
         exits (~10s via its watchdog). Re-probe; report success.
      5. **`denied` → DON'T re-run the warmup**. macOS no-ops
         `CGRequestScreenCaptureAccess` / `AVCaptureDevice.requestAccess`
         after explicit deny — re-running the warmup would just spin for
         10s and exit without surfacing anything. Instead, deep-link the
         user straight to the relevant System Settings pane and tell them
         what to re-enable. Slip degrades to chat-only; user fixes once
         and reruns.

    **Why `open -W -n -a` for the warmup (not `_disclaimed_spawn`):** both
    mechanisms produce correct attribution (spike: 14_31_tcc_warmup_spike).
    They're picked per-context for ergonomics — `open -W` is a one-shot
    foreground launcher that blocks until the helper exits and gives us
    Launch Services lifecycle for free, which is what the warmup needs.
    `_disclaimed_spawn` is built for long-lived pipe-managed spawns (slip-
    live), where you need stdin/stdout plumbing across the whole meeting.
    See `_disclaimed_spawn.spawn_disclaimed` docstring for the contrast.

    Helper isn't installed → no-op (dev fallback path or skipped install).
    Non-macOS → no-op (slip is mac-only anyway, but defensive).
    """
    if sys.platform != "darwin":
        return
    if not _AUDIO_HELPER_BIN.exists():
        return

    # Phase timing — the whole function is on the synchronous pre-fork
    # path, so every ms is user-visible. Probes split the two fast-path
    # phases (lsregister cleanup + helper --probe) and the slow-path
    # warmup. Emitted as one TIMING line at function exit so the slip
    # startup trace can subtract this cost.
    _t_tcc_entry = _startup_time.monotonic()

    # (1) LS cleanup — strip stale duplicates so any subsequent warmup
    # click attaches to the canonical bundle unambiguously.
    n_stale = _ls_cleanup_stale_helpers()
    if n_stale:
        log = logging.getLogger("operator")
        log.info(
            f"Unregistered {n_stale} stale Launch Services entries for "
            f"{_AUDIO_HELPER_APP.name}"
        )
    _t_after_ls = _startup_time.monotonic()

    # (2) Probe current state (self-attributed via _disclaimed_spawn).
    sr, mic = _parse_probe_status(_probe_helper_tcc())
    _t_after_probe = _startup_time.monotonic()
    if sr == "ok" and mic == "ok":
        logging.getLogger("operator").info(
            f"TIMING tcc_preflight path=fast "
            f"ls_cleanup_ms={int((_t_after_ls - _t_tcc_entry) * 1000)} "
            f"probe_ms={int((_t_after_probe - _t_after_ls) * 1000)} "
            f"sr={sr} mic={mic}"
        )
        return  # fast path — both granted

    # (5) Explicit denies — re-warmup can't recover; surface help and
    # open the relevant Settings pane.
    denied: list[tuple[str, str]] = []
    if sr == "denied":
        denied.append(("Screen & System Audio Recording", _SETTINGS_DEEP_LINK_SCREEN_CAPTURE))
    if mic == "denied":
        denied.append(("Microphone", _SETTINGS_DEEP_LINK_MICROPHONE))
    if denied:
        print()
        print("⚠ Operator audio capture is disabled in macOS Privacy & Security.")
        print()
        for pane, _ in denied:
            print(f"  {pane}: DENIED")
        print()
        print("  Fix: System Settings → Privacy & Security → enable 'Operator' under:")
        for pane, _ in denied:
            print(f"        - {pane}")
        print()
        print("  Slip will continue in chat-only mode until you re-enable.")
        # Open the first relevant pane to save the user a navigation click.
        _open_settings_pane(denied[0][1])
        print()
        return

    # (4) Not-determined / unknown / partial-ok — warmup will surface
    # fresh dialogs for whichever perm is missing.
    print(
        "macOS audio permissions needed — surfacing dialogs for the audio helper.\n"
        "  Click Allow on each as it appears (Screen Recording + Microphone).\n"
        "  This takes ~10 seconds. The helper exits on its own when done."
    )
    _t_before_warmup = _startup_time.monotonic()
    try:
        subprocess.run(
            ["open", "-W", "-n", "-a", str(_AUDIO_HELPER_APP)],
            capture_output=True, timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        pass
    _t_after_warmup = _startup_time.monotonic()

    sr2, mic2 = _parse_probe_status(_probe_helper_tcc())
    _t_after_reprobe = _startup_time.monotonic()
    logging.getLogger("operator").info(
        f"TIMING tcc_preflight path=warmup "
        f"ls_cleanup_ms={int((_t_after_ls - _t_tcc_entry) * 1000)} "
        f"probe_ms={int((_t_after_probe - _t_after_ls) * 1000)} "
        f"warmup_ms={int((_t_after_warmup - _t_before_warmup) * 1000)} "
        f"reprobe_ms={int((_t_after_reprobe - _t_after_warmup) * 1000)} "
        f"sr_before={sr} mic_before={mic} sr_after={sr2} mic_after={mic2}"
    )
    if sr2 == "ok" and mic2 == "ok":
        print("✓ Audio permissions granted — proceeding.")
        return

    # Still missing after warmup. Most likely cause: user clicked Don't
    # Allow (which flips status to denied for next run) or Apple's prompt
    # cooldown after recent rapid-fire grants/denies. Don't block the
    # slip — degraded slip beats refusing to join the user's meeting.
    print(
        f"⚠ Audio permissions not fully granted (screen_recording={sr2}, microphone={mic2}).\n"
        "  Slip will run in chat-only mode (no caption transcript).\n"
        "  Fix: System Settings → Privacy & Security → enable 'Operator' under\n"
        "       Screen & System Audio Recording AND Microphone. Then re-run slip."
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

    # Poll on the slip lockfile being released, not on the daemon's pid
    # exiting. The daemon's _shutdown releases the lock early (~500ms
    # after SIGTERM — intentional design so /operator:slip can
    # immediately retry without hitting the singleton guard). The
    # remaining 5-12s of background teardown (PTY drain,
    # connector.leave, audio helper exit) doesn't affect the room's
    # view of the bot — chat panel is detached, claude is no longer
    # responding. So "hung up" is truthful the moment the lock is free.
    # Per-resource defenses (H-16 user-data-dir check on CDP reuse,
    # H-25 sealed JSONL on close) cover any shared-resource overlap a
    # follow-up /operator:slip would otherwise race on.
    deadline = _time.monotonic() + 3.0
    while _time.monotonic() < deadline:
        if not _SLIP_LOCK_PATH.exists():
            break
        # Belt-and-suspenders: if the daemon died before releasing the
        # lock (crashed mid-_shutdown, or never reached it), pid exit
        # is the secondary signal — release the lock ourselves and exit.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            try:
                _SLIP_LOCK_PATH.unlink()
            except OSError:
                pass
            break
        _time.sleep(0.1)
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
    # Pre-daemonize phase timing. The parent process synchronously runs
    # preflights before forking; the bash `!` block in the desktop-app
    # surface waits for the parent's _os._exit(0), so every ms here is
    # user-visible startup latency. The child inherits the file-logging
    # handler (set up just before the fork) so the TIMING line lands in
    # /tmp/operator.log even though the parent dies immediately after.
    _t_slip_entry = _startup_time.monotonic()
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
    _t_argv_done = _startup_time.monotonic()

    # Configure file logging now (before any preflight) so the preflights
    # can emit their own TIMING breadcrumbs. The post-fork basicConfig in
    # the child is a no-op once handlers exist.
    logging.basicConfig(
        filename="/tmp/operator.log",
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

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
    _t_lock_done = _startup_time.monotonic()

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
    _t_claude_check_done = _startup_time.monotonic()

    # First-run TCC warmup for the signed audio helper. No-op when both
    # perms are already granted (common case after install.sh's warmup);
    # surfaces dialogs synchronously when not, so the user clicks Allow
    # BEFORE we daemonize + join the meeting (otherwise the audio helper
    # dies silently on first use). Best-effort: prints a warn and
    # continues if user denies — degraded slip (silent captions) beats
    # refusing to join the meeting they're about to attend.
    _preflight_audio_helper_tcc()
    _t_tcc_done = _startup_time.monotonic()

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

    # File logging was configured earlier in this function (right after
    # argv parse) so preflights could emit their own TIMING breadcrumbs.
    # Emit the pre-daemonize startup line now; the child inherits the
    # file-handler fd across the fork and appends atomically.
    _log_pre = logging.getLogger("operator")
    _t_fork = _startup_time.monotonic()
    _log_pre.info(
        f"TIMING slip_startup "
        f"python_boot_ms={int((_t_slip_entry - _T_MODULE_LOAD) * 1000)} "
        f"argv_ms={int((_t_argv_done - _t_slip_entry) * 1000)} "
        f"lock_ms={int((_t_lock_done - _t_argv_done) * 1000)} "
        f"claude_check_ms={int((_t_claude_check_done - _t_lock_done) * 1000)} "
        f"tcc_preflight_ms={int((_t_fork - _t_claude_check_done) * 1000)} "
        f"total_ms={int((_t_fork - _T_MODULE_LOAD) * 1000)} "
        f"mode={mode}"
    )

    _daemonize_and_announce(url)

    import signal
    import time as _time

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

    # Export the meeting-record path so the bundled MCP server picks it up
    # at spawn time. inner-claude inherits this env, and the MCP subprocess
    # under inner-claude inherits it in turn — atomic, can't be race-
    # overwritten by any same-uid attacker the way the marker file could.
    # Marker file is still written below as a legacy fallback for static
    # MCP registrations that miss this env, and is now validated MCP-side.
    os.environ["OPERATOR_MEETING_RECORD_PATH"] = str(meeting_record.path)

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
        # Shutdown structure (post-S243):
        #
        # Fast phase  (sync, ~0s): unlink .current_meeting marker + release
        #   slip.pid. These are pure lookup signals — nothing in the rest of
        #   the teardown reads them, so releasing first means subsequent
        #   /operator:status and /operator:slip see truth immediately (the
        #   H-11 "hangup feels fast" promise).
        #
        # Phase 1     (parallel): runner.stop() (which itself parallelizes
        #   provider.stop + classifier.stop) AND connector.leave (browser
        #   thread + audio pipeline + Playwright). They touch independent
        #   subprocess + thread state; no shared mutable data. Each branch
        #   has its own try/except — one failure must not stall the other.
        #   30s safety join per branch — wedges fall through to the reaper.
        #
        # Phase 2     (sync, ~0s): meeting_record.close() bakes
        #   participants_final + meeting_end into the JSONL. Reads
        #   runner._attended_participants which is stable only after
        #   runner.stop returned (chat thread joined). Roster file unlinks
        #   here too — chat_runner can no longer race-rewrite it.
        #
        # Phase 3     (~0.6s): _kill_orphaned_children. MUST run after Phase 1
        #   branches join, else we could SIGKILL grandchildren a branch was
        #   still tearing down.
        import time as _time
        import threading as _threading
        _t_start = _time.monotonic()
        try:
            marker = Path.home() / ".operator" / ".current_meeting"
            if marker.exists():
                marker.unlink()
        except OSError:
            pass
        _release_slip_lock()
        _t_fast = _time.monotonic()

        # Phase 1: parallel teardown of inner subprocs + browser/audio.
        _t_runner_end = [_t_fast]
        _t_leave_end = [_t_fast]

        def _do_runner_stop():
            try:
                runner.stop()
            except Exception as e:
                log.warning(f"_shutdown: runner.stop raised: {e}")
            finally:
                _t_runner_end[0] = _time.monotonic()

        def _do_connector_leave():
            try:
                connector.leave()
            except Exception as e:
                log.warning(f"_shutdown: connector.leave raised: {e}")
            finally:
                _t_leave_end[0] = _time.monotonic()

        t_runner_thread = _threading.Thread(target=_do_runner_stop, daemon=True)
        t_leave_thread = _threading.Thread(target=_do_connector_leave, daemon=True)
        t_runner_thread.start()
        t_leave_thread.start()
        t_runner_thread.join(timeout=30)
        t_leave_thread.join(timeout=30)
        if t_runner_thread.is_alive():
            log.warning("_shutdown: runner.stop branch did not complete in 30s — abandoning to reaper")
        if t_leave_thread.is_alive():
            log.warning("_shutdown: connector.leave branch did not complete in 30s — abandoning to reaper")
        _t_phase1_end = _time.monotonic()

        # Phase 2: bake participants_final + meeting_end into JSONL.
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
        _t_record = _time.monotonic()

        # Phase 3: safety-net reap.
        _kill_orphaned_children()
        _t_done = _time.monotonic()
        log.info(
            f"TIMING shutdown mode=slip "
            f"fast_unlink={_t_fast - _t_start:.2f}s "
            f"runner_stop={_t_runner_end[0] - _t_fast:.2f}s "
            f"connector_leave={_t_leave_end[0] - _t_fast:.2f}s "
            f"phase1_total={_t_phase1_end - _t_fast:.2f}s "
            f"record_close={_t_record - _t_phase1_end:.2f}s "
            f"reap={_t_done - _t_record:.2f}s "
            f"total={_t_done - _t_start:.2f}s"
        )

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
        # Same parallel-teardown structure as slip mode (see slip _shutdown
        # for the phase explanation). Wiretap has no provider + no
        # classifier so runner.stop is a fast no-op; connector.leave still
        # has the audio pipeline + Playwright teardown to do. The
        # asymmetric parallelism is fine; structure stays uniform across
        # modes so failure paths look the same.
        import time as _time
        import threading as _threading
        _t_start = _time.monotonic()
        try:
            marker = Path.home() / ".operator" / ".current_meeting"
            if marker.exists():
                marker.unlink()
        except OSError:
            pass
        _release_slip_lock()
        _t_fast = _time.monotonic()

        _t_runner_end = [_t_fast]
        _t_leave_end = [_t_fast]

        def _do_runner_stop():
            try:
                runner.stop()
            except Exception as e:
                log.warning(f"_shutdown: runner.stop raised: {e}")
            finally:
                _t_runner_end[0] = _time.monotonic()

        def _do_connector_leave():
            try:
                connector.leave()
            except Exception as e:
                log.warning(f"_shutdown: connector.leave raised: {e}")
            finally:
                _t_leave_end[0] = _time.monotonic()

        t_runner_thread = _threading.Thread(target=_do_runner_stop, daemon=True)
        t_leave_thread = _threading.Thread(target=_do_connector_leave, daemon=True)
        t_runner_thread.start()
        t_leave_thread.start()
        t_runner_thread.join(timeout=30)
        t_leave_thread.join(timeout=30)
        if t_runner_thread.is_alive():
            log.warning("_shutdown: runner.stop branch did not complete in 30s — abandoning to reaper")
        if t_leave_thread.is_alive():
            log.warning("_shutdown: connector.leave branch did not complete in 30s — abandoning to reaper")
        _t_phase1_end = _time.monotonic()

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
        _t_record = _time.monotonic()
        _kill_orphaned_children()
        _t_done = _time.monotonic()
        log.info(
            f"TIMING shutdown mode=wiretap "
            f"fast_unlink={_t_fast - _t_start:.2f}s "
            f"runner_stop={_t_runner_end[0] - _t_fast:.2f}s "
            f"connector_leave={_t_leave_end[0] - _t_fast:.2f}s "
            f"phase1_total={_t_phase1_end - _t_fast:.2f}s "
            f"record_close={_t_record - _t_phase1_end:.2f}s "
            f"reap={_t_done - _t_record:.2f}s "
            f"total={_t_done - _t_start:.2f}s"
        )

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
