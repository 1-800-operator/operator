"""
Operator — AI Meeting Participant
Slip mode entry point. CDP-attaches `claude` to a dedicated Chrome window
running the meeting.

Usage:
    operator slip claude <url>    Attach claude to a slip Chrome session
    operator doctor               Diagnostic check — is the world ready?
    operator                      Print usage
"""
import os
import subprocess
import sys
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
    print("  operator slip claude <url>      Attach claude to a slip Chrome session")
    print("  operator doctor                 Diagnostic check — is the world ready?")
    print()
    print("Flags:")
    print("  --yolo                          Skip per-tool permission prompts")
    print("  --resume-session <id>           Bridge an existing Claude Code session into slip")


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

    if first == "doctor":
        if len(argv) != 1:
            print("Usage: operator doctor\n")
            _print_usage()
            return 2
        from _1_800_operator.pipeline.doctor import run_doctor
        return run_doctor()

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

    print(f"Unknown subcommand: {first!r}\n")
    _print_usage()
    return 2


def _consume_yolo(args):
    """Strip `--yolo` from argv list; return (filtered_args, yolo_bool).

    The flag appends `--dangerously-skip-permissions` to the spawned
    `claude` CLI via the OPERATOR_YOLO env var read in
    providers/claude_cli.py:_spawn.
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
        print("slip mode is currently macOS-only.", file=sys.stderr)
        return 2

    # claude binary preflight — fail loud and early; no browser dance,
    # no config load if claude isn't installed or logged in.
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
        return 2

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


if __name__ == "__main__":
    sys.exit(main() or 0)
