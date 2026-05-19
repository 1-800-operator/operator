"""
Terminal-mode self-test harness — drive the chat→LLM→reply path without a
real Google Meet.

Spirit-of-the-old-`operator try`: a TerminalConnector that implements
MeetingConnector with full parity (stdin lines → chat messages, send_chat
→ stdout), wired through the same MeetingRecord + LLMClient +
ClaudeCLIProvider + ChatRunner that dial mode uses. Lets the assistant
exercise:

    - trigger phrase gating (only `@claude …` forwards)
    - LLMClient → ClaudeCLIProvider streaming + paragraph flush
    - operator-voice narration (running <tool>, denial, connection events)
    - auto-leave timer (disabled here — participants pinned at 2)
    - transcript MCP search against the live JSONL
    - --yolo flag via OPERATOR_YOLO=1

What it does NOT cover (all browser/audio-layer): AttachAdapter CDP
attach, chat-panel DOM, Swift audio helper, TCC perms. Those need a real
Meet — that's what 14.22.9 live-test is for.

Usage:
    python tests/try_terminal.py
    > @claude what's 2+2
    [🤖 Claude] 4

    OPERATOR_YOLO=1 python tests/try_terminal.py   # skip per-tool prompts
"""
import logging
import os
import queue
import signal
import sys
import threading
import time
import uuid
from pathlib import Path

# Make the src/ layout importable when run directly from a checkout.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from _1_800_operator.bridges import claude as claude_bridge  # noqa: E402
from _1_800_operator.connectors.base import MeetingConnector  # noqa: E402
from _1_800_operator.pipeline.chat_runner import ChatRunner  # noqa: E402
from _1_800_operator.pipeline.llm import LLMClient  # noqa: E402
from _1_800_operator.pipeline.meeting_record import (  # noqa: E402
    MeetingRecord,
    slug_from_url,
)
from _1_800_operator.pipeline.providers import build_provider  # noqa: E402


FAKE_URL = "https://meet.google.com/terminal-try-harness"


class TerminalConnector(MeetingConnector):
    """MeetingConnector backed by stdin/stdout — full parity, not a stub.

    Implements every method on the base. Browser-specific concepts
    (participant count, captions, connection liveness) get pragmatic
    real implementations rather than no-ops so ChatRunner's logic
    exercises the same code paths it would in dial mode.
    """

    def __init__(self, reply_prefix: str = ""):
        super().__init__()
        self._reply_prefix = reply_prefix
        self._inbox: queue.Queue[dict] = queue.Queue()
        self._alive = True
        self._caption_cb = None
        # Pinned > 1 so ChatRunner's auto-leave grace timer never fires.
        # The point of the harness is to keep the loop alive across many
        # turns so the assistant can drive multi-turn dialogue.
        self._participants = ["You", "Claude"]
        self._stdin_thread: threading.Thread | None = None

    def join(self, meeting_url):
        # No real meeting to join; spin up the stdin pump and return.
        # Leaving join_status as None tells ChatRunner to skip the wait.
        self._stdin_thread = threading.Thread(
            target=self._stdin_pump, name="terminal-stdin", daemon=True,
        )
        self._stdin_thread.start()
        sys.stdout.write(
            "[harness] terminal mode ready — type messages to simulate the meeting chat.\n"
            "[harness] mention @claude to trigger the bot. Ctrl+C to exit.\n"
        )
        sys.stdout.flush()

    def _stdin_pump(self):
        """Read stdin line-by-line; each line becomes one chat message."""
        try:
            for line in sys.stdin:
                if not self._alive:
                    return
                text = line.rstrip("\n")
                if not text.strip():
                    continue
                self._inbox.put({
                    "id": str(uuid.uuid4()),
                    "sender": "You",
                    "text": text,
                })
        except Exception as e:
            logging.getLogger("terminal").warning(f"stdin pump exited: {e}")

    def send_chat(self, message):
        """Bot-voice post — prepend the dial reply prefix so the harness
        output mirrors what a participant would see in Meet chat."""
        prefixed = f"{self._reply_prefix}{message}" if self._reply_prefix else message
        sys.stdout.write(prefixed + "\n")
        sys.stdout.flush()
        return str(uuid.uuid4())

    def read_chat(self):
        msgs = []
        while True:
            try:
                msgs.append(self._inbox.get_nowait())
            except queue.Empty:
                break
        return msgs

    def get_participant_count(self):
        return len(self._participants) if self._alive else 0

    def get_participant_names(self):
        return list(self._participants) if self._alive else []

    def is_connected(self):
        return self._alive

    def set_caption_callback(self, fn):
        self._caption_cb = fn

    def feed_caption(self, speaker: str, text: str):
        """Inject a caption — useful if the harness wants to test the
        transcript MCP against a known utterance. Not wired to any UI
        affordance today; left as a hook for follow-on test scripts."""
        if self._caption_cb:
            self._caption_cb(speaker, text, time.time())

    def leave(self):
        self._alive = False


def _setup_logging():
    logging.basicConfig(
        filename="/tmp/operator.log",
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main():
    _setup_logging()
    log = logging.getLogger("terminal-harness")

    slug = slug_from_url(FAKE_URL)
    meeting_record = MeetingRecord(
        slug=slug,
        meta={"meet_url": FAKE_URL, "mode": "terminal-try"},
    )

    llm = LLMClient(build_provider(resume_session_id=None))
    llm.set_record(meeting_record)

    # Same marker file dial mode writes — lets the bundled transcript MCP
    # find this harness's JSONL when inner-claude calls search_captions.
    marker = Path.home() / ".operator" / ".current_meeting"
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(meeting_record.path), encoding="utf-8")
    except OSError as e:
        log.warning(f"could not write current-meeting marker: {e}")

    connector = TerminalConnector(reply_prefix=claude_bridge.REPLY_PREFIX_DIAL)
    runner = ChatRunner(connector, llm, meeting_record=meeting_record)

    _shutdown_called = False

    def _shutdown(signum=None, frame=None):
        nonlocal _shutdown_called
        if _shutdown_called:
            return
        _shutdown_called = True
        if signum:
            log.info(f"received signal {signum}")
        runner.stop()
        try:
            if marker.exists():
                marker.unlink()
        except OSError:
            pass
        connector.leave()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    sys.stdout.write(f"[harness] meeting record: {meeting_record.path}\n")
    sys.stdout.flush()
    try:
        runner.run(FAKE_URL)
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown()
        sys.stdout.write("[harness] goodbye.\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
