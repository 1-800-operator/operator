"""
Operator — AI Meeting Participant
Cross-platform entry point. Auto-detects OS and dispatches to the right adapter.

Usage:
    operator slip <name> [url]    Attach an agent to your own Chrome session
    operator dial <name> [url]    Dial named agent as a separate participant
    operator deploy <name> <url>  Send an agent into an existing meeting
    operator login <name>         Sign into Google for dial/deploy
    operator doctor               Diagnostic check — is the world ready?
    operator                      Print usage + agent list
"""
import os
import subprocess
import sys
import webbrowser
from pathlib import Path


# ── Prevent Ctrl+C from killing child processes ────────────────────
# Playwright's Node.js driver and Chrome are child processes in our
# terminal's foreground process group.  When the user presses Ctrl+C,
# the terminal sends SIGINT to the whole group — killing Chrome
# abruptly and leaving it in the meeting for ~60s until Meet's
# heartbeat times out.
#
# Fix: put every child in its own session (setsid) so SIGINT only
# reaches our Python process.  We then close Chrome cleanly via
# Playwright, and Meet sees an immediate disconnect.
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


def _print_usage():
    print("Usage:")
    print("  operator slip claude [url]      Attach claude to your own Chrome session")
    print("  operator dial claude [url]      Dial claude into a Meet as a separate participant")
    print("  operator deploy claude <url>    Send claude into an existing meeting")
    print("  operator login claude           Sign into Google for dial/deploy")
    print("  operator doctor                 Diagnostic check — is the world ready?")
    print()
    print("Flags:")
    print("  --force                         Retry join even if a session is flagged stuck")
    print("  --yolo                          Skip per-tool permission prompts (dial/deploy/slip)")
    print("  --resume-session <id>           Bridge an existing Claude Code session into slip (slip only)")


def _run_login(name):
    """Single-purpose Google sign-in for dial/deploy (Phase 14.19.4).

    Wraps `_launch_signin_flow` from the wizard's step-2 code without any
    of the wizard's prompt scaffolding. Idempotent — running twice
    refreshes the session via Google's logout flow so the user lands on
    the account picker instead of being silently re-recognized.

    Slip mode launches its own dedicated Chrome under
    `~/.operator/slip_profile/`, which is independent of `auth_state.json`
    and lives outside this command's reach. login is for the headless
    profile that dial/deploy share.
    """
    if name != "claude":
        print(f"Unknown bot: {name!r} — only `claude` is supported in v1.\n")
        _print_usage()
        return 2

    from _1_800_operator.pipeline.google_signin import (
        _launch_signin_flow,
        detect_google_session,
    )

    detected = detect_google_session()
    sign_out_first = detected.detected
    if detected.detected and detected.email:
        print(f"Currently signed in as {detected.email}. Refreshing session…")
    elif detected.detected:
        print("Existing Google session detected. Refreshing…")
    else:
        print("No Google session yet. Opening sign-in window…")

    try:
        email = _launch_signin_flow(sign_out_first=sign_out_first)
    except KeyboardInterrupt:
        print("\nAborted.")
        return 130
    except Exception as e:
        print(f"Sign-in failed: {e}")
        return 1

    if email:
        print(f"✓ signed in as {email}")
    else:
        print("✓ Google session saved")
    return 0


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

    if first == "login":
        if len(argv) != 2:
            print("Usage: operator login <name>\n")
            _print_usage()
            return 2
        return _run_login(argv[1])
    if first == "doctor":
        if len(argv) != 1:
            print("Usage: operator doctor\n")
            _print_usage()
            return 2
        from _1_800_operator.pipeline.doctor import run_doctor
        return run_doctor()
    # `run` kept as a hidden alias for muscle memory + external links after
    # the dial rename — not advertised in --help; safe to drop later.
    if first in ("dial", "run"):
        if len(argv) < 2:
            print("Usage: operator dial <name> [url]\n")
            _print_usage()
            return 2
        name = argv[1]
        if name != "claude":
            print(f"Unknown bot: {name!r} — only `claude` is supported in v1.\n")
            _print_usage()
            return 2
        rest, yolo = _consume_yolo(argv[2:])
        if yolo:
            os.environ["OPERATOR_YOLO"] = "1"
        return _run_bot(name, rest)

    # Phase 14.19.2 — `deploy <name> <url>`. Sends agent as a separate
    # participant into an existing meeting. URL required (no meet.new
    # auto-open). Routes through the same `_run_bot` path as dial; the
    # only difference at this level is URL-required.
    if first == "deploy":
        if len(argv) < 3:
            print("Usage: operator deploy <name> <url>\n")
            _print_usage()
            return 2
        name = argv[1]
        url = argv[2]
        if name != "claude":
            print(f"Unknown bot: {name!r} — only `claude` is supported in v1.\n")
            _print_usage()
            return 2
        rest, yolo = _consume_yolo(argv[3:])
        if yolo:
            os.environ["OPERATOR_YOLO"] = "1"
        return _run_bot(name, [url] + rest)

    # Phase 14.19.2/3 — `slip <name> <url>`. CDP-attach to user's existing
    # Chrome session; agent responds *as the user* with a marker prefix.
    # claude-only in v0.0.1; URL required (no meet.new auto-open in slip
    # because the meeting is whatever the user has open).
    if first == "slip":
        if len(argv) < 2:
            print("Usage: operator slip claude <https://meet.google.com/xxx-xxxx-xxx>\n")
            _print_usage()
            return 2
        name = argv[1]
        if name != "claude":
            print(f"Unknown bot: {name!r} — only `claude` is supported in v1.\n")
            _print_usage()
            return 2
        rest, yolo = _consume_yolo(argv[2:])
        if yolo:
            os.environ["OPERATOR_YOLO"] = "1"
        return _run_slip(name, rest)

    if first.startswith("-"):
        print(f"Unknown option: {first}\n")
        _print_usage()
        return 2

    if first == "claude":
        print(
            "Dial claude via `operator dial claude`. "
            "Bare `operator claude` is no longer supported.\n"
        )
        return 2
    print(f"Unknown bot or subcommand: {first!r}\n")
    _print_usage()
    return 2


def _consume_yolo(args):
    """Strip `--yolo` from argv list; return (filtered_args, yolo_bool).

    Centralized so dial/deploy/slip get identical handling. The flag
    appends `--dangerously-skip-permissions` to the spawned `claude` CLI
    via the OPERATOR_YOLO env var read in providers/claude_cli.py:_spawn.
    """
    yolo = "--yolo" in args
    return [a for a in args if a != "--yolo"], yolo


def _run_slip(name, rest):
    """slip mode — launch a dedicated Chrome window for the meeting and
    CDP-attach claude to it.

    Slip Chrome lives at ~/.operator/slip_profile/ — operator-owned,
    separate from the user's main browser. First run: user signs into
    Google in slip Chrome once, cookies persist for future sessions.
    User's main Chrome is never touched.

    Pipeline mirrors _run_macos's construction but swaps connectors,
    skips the meet.new auto-open (slip always takes a URL), and drops
    the user-browser auto-open (slip Chrome IS where the meeting opens).
    Track A only — claude owns its MCPs; no MCPClient setup.

    Caller must have already filtered for `name == "claude"` (the main
    dispatcher does this at argv parse time).
    """
    # `--resume-session <id>` bridges an existing Claude Code session into
    # the meeting. The plugin's slash command always passes this
    # (substituted from `${CLAUDE_SESSION_ID}` at execution time) so the
    # meeting brain rehydrates the user's pre-meeting context on the first
    # @mention. Terminal-direct invocation omits it; a fresh session is
    # born on first @mention and re-used thereafter.
    url = None
    resume_session_id = None
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--resume-session":
            if i + 1 >= len(rest):
                print("--resume-session requires a session id", file=sys.stderr)
                return 2
            resume_session_id = rest[i + 1]
            i += 2
            continue
        if arg.startswith("--resume-session="):
            resume_session_id = arg.split("=", 1)[1]
            i += 1
            continue
        if arg.startswith("-"):
            print(f"Unknown flag: {arg}", file=sys.stderr)
            return 2
        if url is None:
            url = arg
            i += 1
            continue
        print(f"Unexpected argument: {arg}", file=sys.stderr)
        return 2

    if not url:
        print(
            "slip requires a Meet URL: operator slip claude <https://meet.google.com/xxx-xxxx-xxx>",
            file=sys.stderr,
        )
        return 2

    if sys.platform != "darwin":
        print(
            "slip mode is currently macOS-only. Use `operator dial claude` or "
            "`operator deploy claude <url>` on Linux.",
            file=sys.stderr,
        )
        return 2

    # claude binary preflight — same gate _run_bot uses for the claude agent.
    # Fail loud and early; no browser dance, no config load if claude isn't
    # installed or logged in.
    from _1_800_operator.pipeline.claude_code_import import (
        claude_code_installed_and_logged_in,
    )
    ok, reason = claude_code_installed_and_logged_in()
    if not ok:
        print(
            f"\nslip claude requires the Claude Code CLI.\n"
            f"  {reason}\n"
            f"\nInstall Claude Code (https://claude.ai/code) and run "
            f"`claude login`, then re-run.\n",
            file=sys.stderr,
        )
        return 2

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
    meeting_record = MeetingRecord(slug=slug, meta={"meet_url": url, "mode": "slip"})

    llm = LLMClient(build_provider(resume_session_id=resume_session_id))
    llm.set_record(meeting_record)
    if resume_session_id:
        log.info(f"slip: bridging existing Claude Code session {resume_session_id} into meeting")

    # Active-meeting marker (parity with _run_macos — useful for any
    # static-config MCPs that need the active meeting JSONL path).
    try:
        marker = Path.home() / ".operator" / ".current_meeting"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(meeting_record.path), encoding="utf-8")
    except OSError as e:
        log.warning(f"could not write current-meeting marker: {e}")

    connector = AttachAdapter(reply_prefix=claude_bridge.REPLY_PREFIX_SLIP)

    # Wire whisper utterances → meeting record. Direct-write (no
    # TranscriptFinalizer): each callback delivers ONE finalized
    # utterance, not a streaming partial. Routing through TF would
    # double-buffer (whisper finalize + TF silence wait) and risk
    # coalescing two same-speaker utterances inside TF's 0.7s window —
    # whisper's segmentation must be authoritative here.
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
        return 2

    log.info(f"TIMING setup={_time.monotonic() - t_start:.1f}s")
    runner = ChatRunner(
        connector,
        llm,
        meeting_record=meeting_record,
        # slip is "speak when spoken to": no intro, no Hold-for-Claude
        # filler, no 1-on-1 trigger bypass. claude only responds when
        # explicitly @claude'd. dial/deploy leave this default.
        quiet_mode=True,
    )

    _shutdown_called = False

    def _shutdown(signum=None, frame=None):
        nonlocal _shutdown_called
        if _shutdown_called:
            return
        _shutdown_called = True
        if signum:
            log.info(f"Received signal {signum} — shutting down")
        runner.stop()
        try:
            marker = Path.home() / ".operator" / ".current_meeting"
            if marker.exists():
                marker.unlink()
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


def _run_bot(name, rest):
    url = None
    force = False
    for arg in rest:
        if arg == "--force":
            force = True
        elif arg.startswith("-"):
            print(f"Unknown flag: {arg}")
            return 2
        elif url is None:
            url = arg
        else:
            print(f"Unexpected argument: {arg}")
            return 2

    # Claude binary preflight — claude is operator v1's only brain; if the
    # CLI isn't installed or the user isn't logged in, fail loudly before
    # any browser spins up.
    from _1_800_operator.pipeline.claude_code_import import (
        claude_code_installed_and_logged_in,
    )
    ok, reason = claude_code_installed_and_logged_in()
    if not ok:
        print(
            f"\nThe `claude` agent requires the Claude Code CLI.\n"
            f"  {reason}\n"
            f"\nInstall Claude Code (https://claude.ai/code) and run "
            f"`claude login`, then re-run `operator dial claude`.\n",
            file=sys.stderr,
        )
        return 2

    if sys.platform == "darwin":
        return _run_macos(url, force=force) or 0
    return _run_linux(url, force=force) or 0


def _run_macos(meeting_url=None, force=False):
    """Run on macOS — direct URL or meet.new auto-launch."""
    from _1_800_operator.pipeline.chrome_preflight import (
        require_chrome_or_exit,
        require_signed_in_or_exit,
    )
    require_chrome_or_exit()
    require_signed_in_or_exit()

    import logging
    import signal
    import time as _time

    logging.basicConfig(
        filename="/tmp/operator.log",
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    # Stderr stays reserved for the user-facing narrative (pipeline.ui).
    # Detailed diagnostics live in /tmp/operator.log only.
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    log = logging.getLogger("operator")

    from _1_800_operator import config
    from _1_800_operator.connectors.macos_adapter import MacOSAdapter
    from _1_800_operator.pipeline import ui
    from _1_800_operator.pipeline.chat_runner import ChatRunner
    from _1_800_operator.pipeline.llm import LLMClient
    from _1_800_operator.pipeline.providers import build_provider

    t_start = _time.monotonic()

    ui.say("Launching Chrome…")

    connector = MacOSAdapter(force=force)
    llm = LLMClient(build_provider())

    # Captions → MeetingRecord wiring. The JS bridge (window.__onCaption) is
    # exposed by MacOSAdapter at browser startup whenever config.CAPTIONS_ENABLED
    # is true, so set_caption_callback is safe to call before OR after
    # connector.join(). meet.new mode late-binds after the URL resolves.
    def _wire_meeting_record(url):
        if not config.CAPTIONS_ENABLED:
            return None, None
        from _1_800_operator.pipeline.meeting_record import MeetingRecord, slug_from_url
        from _1_800_operator.pipeline.transcript import TranscriptFinalizer
        slug = slug_from_url(url)
        record = MeetingRecord(slug=slug, meta={"meet_url": url})
        llm.set_record(record)
        try:
            marker = Path.home() / ".operator" / ".current_meeting"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(str(record.path), encoding="utf-8")
        except OSError as e:
            log.warning(f"could not write current-meeting marker: {e}")
        finalizer = TranscriptFinalizer(record, silence_seconds=config.CAPTION_SILENCE_SECONDS)
        connector.set_caption_callback(finalizer.on_caption_update)
        log.info("captions enabled — transcript will be appended to meeting record")
        return record, finalizer

    meeting_record = None
    transcript_finalizer = None
    if meeting_url:
        meeting_record, transcript_finalizer = _wire_meeting_record(meeting_url)

    connector.join(meeting_url)

    # meet.new mode: wait for the browser to redirect and publish the real URL.
    if meeting_url is None:
        meeting_url = connector.wait_for_resolved_url(timeout=45)
        if not meeting_url:
            log.error("meet.new did not produce a meeting URL — exiting")
            ui.err("meet.new did not produce a meeting URL")
            connector.leave()
            _kill_orphaned_children()
            return 1
        log.info(f"meet.new resolved to {meeting_url}")
        ui.ok(f"Fresh meeting: {meeting_url}")
        # The bot joins in a headless Chrome — pop the Meet open in the
        # user's default browser so they can see and chat with the bot.
        try:
            webbrowser.open(meeting_url)
        except Exception as e:
            log.warning(f"could not auto-open meeting URL in browser: {e}")
        meeting_record, transcript_finalizer = _wire_meeting_record(meeting_url)

    log.info(f"TIMING setup={_time.monotonic() - t_start:.1f}s")
    runner = ChatRunner(
        connector,
        llm,
        meeting_record=meeting_record,
    )

    _shutdown_called = False

    def _shutdown(signum=None, frame=None):
        nonlocal _shutdown_called
        if _shutdown_called:
            return
        _shutdown_called = True
        reason_file = os.path.join(config.BROWSER_PROFILE_DIR, ".operator.kill_reason")
        try:
            with open(reason_file) as _f:
                reason = _f.read().strip()
            os.remove(reason_file)
            ui.err(reason, hint_log=False)
            log.info(reason)
        except FileNotFoundError:
            if signum:
                log.info(f"Received signal {signum} — shutting down")
        runner.stop()
        if transcript_finalizer:
            transcript_finalizer.stop()
        try:
            marker = Path.home() / ".operator" / ".current_meeting"
            if marker.exists():
                marker.unlink()
        except OSError:
            pass
        connector.leave()
        _kill_orphaned_children()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        log.info(f"Starting Operator — joining {meeting_url}")
        runner.run(meeting_url)
        if not runner._stop_event.is_set():
            ui.say(f"Restart with: operator dial claude {meeting_url}")
    except KeyboardInterrupt:
        log.info("Interrupted — leaving meeting")
    finally:
        _shutdown()
        ui.ok("Left meeting — goodbye.")
    return 0


def _run_linux(meeting_url, force=False):
    """Run on Linux — requires a meeting URL and a live DISPLAY."""
    import logging
    import signal

    logging.basicConfig(
        filename="/tmp/operator.log",
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = logging.getLogger("operator")

    if not meeting_url:
        meeting_url = os.environ.get("MEETING_URL")
    if not meeting_url:
        print("A meeting URL is required on Linux:", file=sys.stderr)
        print("   operator dial claude <meet-url>", file=sys.stderr)
        print("   MEETING_URL=<url> operator dial claude", file=sys.stderr)
        sys.exit(1)

    display = os.environ.get("DISPLAY")
    if not display:
        log.error("DISPLAY is not set")
        print("DISPLAY is not set — start Xvfb first:", file=sys.stderr)
        print("   Xvfb :99 -screen 0 1920x1080x24 &", file=sys.stderr)
        print("   export DISPLAY=:99", file=sys.stderr)
        sys.exit(1)
    log.info(f"DISPLAY={display}")

    from _1_800_operator.connectors.linux_adapter import LinuxAdapter
    from _1_800_operator.pipeline import ui
    from _1_800_operator.pipeline.chat_runner import ChatRunner
    from _1_800_operator.pipeline.llm import LLMClient
    from _1_800_operator.pipeline.providers import build_provider
    from _1_800_operator import config

    ui.say("Launching Chromium…")

    log.info(f"Starting Operator (Linux) — joining {meeting_url}")
    connector = LinuxAdapter()
    llm = LLMClient(build_provider())

    runner = ChatRunner(
        connector,
        llm,
    )

    _shutdown_called = False

    def _shutdown(signum=None, frame=None):
        nonlocal _shutdown_called
        if _shutdown_called:
            return
        _shutdown_called = True
        if signum:
            log.info(f"Received signal {signum} — shutting down")
        runner.stop()
        connector.leave()
        _kill_orphaned_children()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        runner.run(meeting_url)
        if not runner._stop_event.is_set():
            ui.say(f"Restart with: operator dial claude {meeting_url}")
    except KeyboardInterrupt:
        log.info("Interrupted — leaving meeting")
    finally:
        _shutdown()
        ui.ok("Left meeting — goodbye.")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
