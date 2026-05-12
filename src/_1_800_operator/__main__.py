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
    print("  operator status                 Is operator currently in a meeting?")
    print("  operator hangup                 Gracefully disconnect the running slip session")
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

    if first == "status":
        if len(argv) != 1:
            print("Usage: operator status\n")
            _print_usage()
            return 2
        return _run_status()

    if first == "hangup":
        if len(argv) != 1:
            print("Usage: operator hangup\n")
            _print_usage()
            return 2
        return _run_hangup()

    if first == "slip":
        if len(argv) < 2:
            print("Usage: operator slip claude <https://meet.google.com/xxx-xxxx-xxx>\n")
            _print_usage()
            return 2
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
        # look URL-shaped.
        if name not in KNOWN_BOTS and name.startswith(("http://", "https://")):
            inferred = _infer_bot_from_surface()
            if inferred:
                argv.insert(1, inferred)
                name = inferred
        if name not in KNOWN_BOTS:
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
        # Parent — emit the synchronous status line and exit clean.
        # The Bash tool's stdout capture closes at this exit, so the
        # model gets exactly this line as the command's response.
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
    """Strip `--yolo` from argv list; return (filtered_args, yolo_bool).

    The flag appends `--dangerously-skip-permissions` to the spawned
    `claude` CLI via the OPERATOR_YOLO env var read in
    providers/claude_cli.py:_spawn.
    """
    yolo = "--yolo" in args
    return [a for a in args if a != "--yolo"], yolo


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
    """Send SIGTERM to any running `operator slip` process.

    The slip process's signal handler does the graceful teardown:
    ChatRunner.stop(), connector.leave() (CDP detach — does NOT quit
    Chrome or close the chat panel, per spec), and clears the
    .current_meeting marker. If no slip process is running but the
    marker is stale, clean it up so `operator status` doesn't lie.
    """
    import signal as _sig
    import time as _time
    marker = Path.home() / ".operator" / ".current_meeting"
    try:
        result = subprocess.run(
            ["pgrep", "-f", "operator slip"],
            capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"hangup: could not query running processes: {e}", file=sys.stderr)
        return 2
    pids = [int(p) for p in result.stdout.strip().split("\n") if p.strip()]
    pids = [p for p in pids if p != os.getpid()]
    if not pids:
        if marker.exists():
            try:
                marker.unlink()
            except OSError:
                pass
        print("not in a meeting")
        return 0
    for pid in pids:
        try:
            os.kill(pid, _sig.SIGTERM)
        except ProcessLookupError:
            pass
    # Brief wait so the slip process can run its shutdown handler
    # (connector.leave waits up to 10s for the browser thread). We poll
    # only for ~3s — long enough to confirm exit on the happy path, not
    # so long that the plugin skill feels stuck.
    deadline = _time.monotonic() + 3.0
    while _time.monotonic() < deadline:
        alive = []
        for pid in pids:
            try:
                os.kill(pid, 0)
                alive.append(pid)
            except ProcessLookupError:
                pass
        if not alive:
            break
        _time.sleep(0.2)
    print(f"hung up ({len(pids)} session{'s' if len(pids) != 1 else ''})")
    return 0


def _discover_session_from_claude_state(window_seconds: float = 300.0):
    """Find the active desktop-app Claude Code session UUID by reading
    Claude desktop's per-conversation state files.

    Claude desktop writes a JSON state file per conversation at
    ~/Library/Application Support/Claude/claude-code-sessions/<agent>/<workspace>/local_<msg>.json
    and rewrites it as turns complete. Each file carries `cliSessionId`
    (the Claude Code session UUID we want as --resume-session).

    Algorithm: pick the most-recently-modified state file (filesystem
    mtime, not the in-file `lastActivityAt`) whose mtime falls within
    `window_seconds`. We use mtime instead of the JSON field because the
    field is what the desktop app wrote on its *last* completed turn —
    when operator runs tier 3 ~1-2s into startup, the current turn's
    file update may not have landed yet. mtime catches the actual disk
    write the instant it happens; both will converge but mtime is at
    least as fresh. Window is 5 min because (a) the firing conversation
    will usually update during operator's startup window, (b) failing
    that, its mtime from its prior turn is still recent enough to
    identify it as the active conversation, (c) wider windows risk
    bridging to a stale conversation that hasn't been touched in hours.

    Earlier versions scanned /private/tmp/claude-{uid}/.../tasks/*.output
    instead, but that signal is fragile: foreground commands (no &) don't
    create .output files at all, and even backgrounded ones have mtime
    races against operator's import-time startup latency. The state file
    is updated by the desktop app independently of the bash task
    subsystem, which makes it the reliable signal.
    """
    import glob
    import json as _json
    import time as _time

    home = os.path.expanduser("~")
    pattern = (
        f"{home}/Library/Application Support/Claude/claude-code-sessions/"
        f"*/*/local_*.json"
    )
    now = _time.time()
    cutoff = now - window_seconds

    best_path = None
    best_mtime = 0.0
    for path in glob.glob(pattern):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime < cutoff or mtime <= best_mtime:
            continue
        best_path = path
        best_mtime = mtime

    if not best_path:
        return None
    try:
        with open(best_path, "r", encoding="utf-8") as fh:
            return _json.load(fh).get("cliSessionId") or None
    except (OSError, _json.JSONDecodeError):
        return None


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
    # Resume-session resolution has three tiers (see logic below):
    #   1. --resume-session <id> on the command line.
    #   2. CLAUDE_CODE_SESSION_ID env var (terminal Claude Code sets this).
    #   3. Read cliSessionId from the most-recently-active desktop-app
    #      session state file under ~/Library/Application Support/Claude/
    #      claude-code-sessions/ (desktop app — no env var exposed).
    # The skill body no longer passes --resume-session because the desktop-app
    # model rewrites/mangles flags; tiers 2/3 catch that case.
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

    # Singleton guard — refuse to start if another operator slip is already
    # running. Without this, the desktop app can stack operators on the same
    # slip Chrome (e.g. when the model retries a failed dispatch), each
    # spawning its own audio helper and writing to the same meeting JSONL.
    try:
        result = subprocess.run(
            ["pgrep", "-f", "operator slip claude"],
            capture_output=True, text=True, timeout=3,
        )
        other_pids = [
            int(p) for p in result.stdout.strip().split("\n")
            if p.strip() and int(p) != os.getpid()
        ]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        other_pids = []  # fail open if pgrep unavailable
    if other_pids:
        print(
            f"operator slip is already running (pid {other_pids[0]}). "
            f"Run `operator hangup` (or `/operator:hangup`) first, then retry.",
            file=sys.stderr,
        )
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

    # All synchronous validation has passed. Hand the caller (Bash tool,
    # shell, etc.) a one-line success acknowledgement, then detach so
    # the long-running meeting work doesn't block the response. See
    # _daemonize_and_announce docstring for why.
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
    meeting_record = MeetingRecord(slug=slug, meta={"meet_url": url, "mode": "slip"})

    # Three-tier resume-session resolution (see docstring at top of _run_slip).
    resume_source = None
    if resume_session_id:
        resume_source = "flag"
    if not resume_session_id:
        resume_session_id = os.environ.get("CLAUDE_CODE_SESSION_ID") or None
        if resume_session_id:
            resume_source = "env"
    if not resume_session_id:
        resume_session_id = _discover_session_from_claude_state()
        if resume_session_id:
            resume_source = "state-scan"

    llm = LLMClient(build_provider(resume_session_id=resume_session_id))
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
