"""Phase 14.18 pressure test harness.

Drives `operator try <bot>` via stdin pipe across a matrix of caption
loads, runs a fixed query battery, captures and parses the bot's
replies, and writes structured results so we can eyeball whether each
bot picks the right verb at each scale.

Caption-load levels:
  realistic    30 captions / 3 speakers / 15 min   (the multi-speaker fixture)
  long-real   442 captions / 1 speaker  / 85 min   (real history fixture)
  heavy      1000 captions / 5 speakers / 60 min
  stress     5000 captions / 5 speakers / 240 min
  extreme   20000 captions / 5 speakers / 480 min

Bots: claude (env-var resolution path), codex (marker-file resolution path).

This script is intentionally one self-contained file in debug/ — not a
pytest target, not a tests/ unit. It's a manual pressure rig run before
we ship 14.18.

Usage:
    cd /Users/jojo/Desktop/operator
    source venv/bin/activate
    python debug/14_18_pressure/run_pressure_test.py [bot] [load]

    # No args → run full matrix.
    # bot ∈ {claude, codex}; load ∈ {realistic, long-real, heavy, stress, extreme}.
"""
from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEBUG_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = DEBUG_DIR / "fixtures"
OUTPUTS_DIR = DEBUG_DIR / "outputs"
MARKER_FILE = Path.home() / ".operator" / ".current_meeting"
REAL_LONG_FIXTURE = Path.home() / ".operator" / "history" / "avd-axqi-obq.jsonl"

FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)


# ---- Synthesized caption content -----------------------------------

SPEAKERS = ["Alice", "Bob", "Carol", "Dan", "Eve"]

TOPIC_FRAGMENTS = [
    "the migration timeline keeps slipping",
    "Sentry alerts are firing on the auth path",
    "Mohammed pinged about the database pick",
    "Postgres for prod SQLite for tests is the standard split",
    "the launch is blocked on the codex parity work",
    "let's confirm the marker file mechanism survives a crash",
    "I think we should narrow the rollout to ten percent first",
    "the prompt cache hit rate looked great in the last run",
    "double-check the byte ceiling on the tool result",
    "Priya raised a concern about the approval policy",
    "we need to coordinate with the mobile release branch",
    "the new Sonnet benchmarks looked strong on coding tasks",
    "QA found a flaky test in the chat hardening suite",
    "let me share the dashboard link",
    "I'm seeing weird latency on the MCP startup path",
    "the captions stopped appearing for a stretch",
    "let's pull up the GitHub issue for that bug",
    "we still need to revoke that leaked token",
    "the wizard re-import flow is finally idempotent",
    "we should write a fixture for that edge case",
]

LONG_FRAGMENTS = [
    "okay so I want to walk through how I think about this — the way the byte ceiling interacts with the model's tendency to retry on truncation is going to determine whether this scales gracefully or not, and I'd like to make sure we're not setting ourselves up for a thrashing failure mode where the model keeps hitting the cap and never narrowing the filter",
    "the thing about the marker file approach versus the env var is that they have different startup ordering implications — codex spawns the transcript MCP at codex startup which is before the meeting URL exists in some flows so we can't bake the path into the spawn args which is exactly the problem the marker file solves",
    "I want to be honest about the testing gaps too — we're skipping the live Meet because we don't want to spend the time but the cost is that we don't exercise the DOM caption observer path or the meet.new resolution path or the participant auto-leave logic but those haven't been touched in this phase so the risk is bounded",
]


def _gen_text(rng: random.Random, want_long: bool = False) -> str:
    """Generate a single caption text. ~10% chance of being a long monologue."""
    if want_long or rng.random() < 0.10:
        return rng.choice(LONG_FRAGMENTS)
    parts = rng.choices(TOPIC_FRAGMENTS, k=rng.randint(1, 3))
    return " — ".join(parts)


def _gen_fixture(name: str, count: int, span_minutes: float, n_speakers: int,
                 seed: int = 1818) -> Path:
    """Generate a JSONL fixture with `count` captions over `span_minutes`,
    timestamps anchored to time.time() so 'minutes_ago' queries land sanely.
    """
    rng = random.Random(seed)
    speakers = SPEAKERS[:max(1, min(n_speakers, len(SPEAKERS)))]
    now = time.time()
    span_seconds = span_minutes * 60
    out_path = FIXTURES_DIR / f"{name}.jsonl"

    rows = [{"kind": "session_start", "timestamp": now - span_seconds - 5}]

    # Plant distinctive markers so the query battery has things to find:
    plants = [
        (0.02, "Alice", "I want to bring up the launch timeline"),
        (0.10, "Bob", "did anyone hear from Mohammed today"),
        (0.20, "Carol", "the Sentry alerts are firing again"),
        (0.40, "Alice", "Mohammed pinged me about the database pick"),
        (0.60, "Dan", "the launch is the priority this week"),
        (0.85, "Eve", "let me check Sentry one more time"),
    ]
    plant_indices = {int(count * frac): (sp, txt) for frac, sp, txt in plants}

    for i in range(count):
        # Distribute timestamps roughly evenly with some jitter
        t_frac = (i + rng.uniform(-0.3, 0.3)) / count
        t_frac = max(0.001, min(0.999, t_frac))
        ts = (now - span_seconds) + t_frac * span_seconds

        if i in plant_indices:
            speaker, text = plant_indices[i]
        else:
            speaker = rng.choice(speakers)
            text = _gen_text(rng)

        rows.append({
            "kind": "caption",
            "sender": speaker,
            "text": text,
            "timestamp": ts,
        })

    rows.sort(key=lambda r: r.get("timestamp") or 0)
    with out_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    return out_path


def _ensure_fixture(name: str) -> Path:
    """Build (or return) a fixture for the given load name."""
    specs = {
        "realistic":  (30,    15,   3),
        "long-real":  None,  # use the real history fixture
        "heavy":      (1000,  60,   5),
        "stress":     (5000,  240,  5),
        "extreme":    (20000, 480,  5),
    }
    if name == "long-real":
        if not REAL_LONG_FIXTURE.exists():
            raise FileNotFoundError(
                f"long-real fixture missing: {REAL_LONG_FIXTURE}. "
                "Run with another load level."
            )
        # Copy with re-anchored timestamps so time-window queries work.
        return _reanchor_real_fixture()
    count, span, speakers = specs[name]
    return _gen_fixture(name, count, span, speakers)


def _reanchor_real_fixture() -> Path:
    """Read the real long fixture and rewrite timestamps so its newest
    caption sits at ~1 min ago (relative to time.time()), preserving
    inter-caption deltas.
    """
    out = FIXTURES_DIR / "long-real-reanchored.jsonl"
    rows = []
    with REAL_LONG_FIXTURE.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    # Find max original timestamp among captions
    caption_ts = [r["timestamp"] for r in rows if r.get("kind") == "caption" and isinstance(r.get("timestamp"), (int, float))]
    if not caption_ts:
        raise RuntimeError("real fixture has no caption timestamps")
    orig_max = max(caption_ts)
    target_max = time.time() - 60  # newest caption ~1 min ago
    delta = target_max - orig_max
    for r in rows:
        ts = r.get("timestamp")
        if isinstance(ts, (int, float)):
            r["timestamp"] = ts + delta
    with out.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return out


# ---- Query battery -------------------------------------------------

QUERIES = [
    "what name did I just say",
    "summarize the last 5 minutes",
    "what did anyone say about Sentry",
    "who has spoken so far",
    "what was the topic 8 to 10 minutes ago",
    "did anyone mention launch",
    "summarize the whole meeting",
    "quote the part where someone talked about the database",
]


# ---- Driver --------------------------------------------------------

HISTORY_DIR = Path.home() / ".operator" / "history"


def _wire_path_codex(fixture_path: Path):
    """Codex path: marker file. Codex's transcript MCP reads marker first,
    so we don't care what record path operator's __main__ sets."""
    MARKER_FILE.parent.mkdir(parents=True, exist_ok=True)
    MARKER_FILE.write_text(str(fixture_path), encoding="utf-8")


def _unwire():
    if MARKER_FILE.exists():
        MARKER_FILE.unlink()
    os.environ.pop("OPERATOR_MEETING_RECORD_PATH", None)


def _read_caption_rows(fixture_path: Path) -> list[dict]:
    rows = []
    with fixture_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("kind") == "caption":
                rows.append(row)
    return rows


def _snapshot_history() -> set:
    if HISTORY_DIR.exists():
        return {p.name for p in HISTORY_DIR.glob("terminal-*.jsonl")}
    return set()


def _claude_inject_after_spawn(fixture_path: Path, pre_existing: set,
                                deadline_s: float = 30) -> Path | None:
    """Wait for a NEW terminal-*.jsonl to appear (created by the bot's
    MeetingRecord init), then append fixture caption rows after the
    bot's session_start so they're visible as the current session.

    Returns the record path it injected into, or None on timeout.
    """
    t0 = time.monotonic()
    target = None
    while time.monotonic() - t0 < deadline_s:
        if HISTORY_DIR.exists():
            for p in HISTORY_DIR.glob("terminal-*.jsonl"):
                if p.name not in pre_existing:
                    target = p
                    break
        if target is not None:
            break
        time.sleep(0.2)
    if target is None:
        return None

    # Wait briefly for MeetingRecord to finish writing meta + session_start.
    time.sleep(0.5)
    captions = _read_caption_rows(fixture_path)
    with target.open("a", encoding="utf-8") as f:
        for row in captions:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return target


def _drive_bot(bot: str, queries: list[str], fixture_path: Path,
               per_query_timeout_s: int = 120,
               total_timeout_s: int = 1800) -> dict:
    """Spawn `operator try <bot>`, inject fixture, drive queries one at
    a time waiting for each reply before sending the next. Returns
    results with per-query timing and the parsed reply text.
    """
    import re
    import threading
    cmd = [sys.executable, "-m", "_1_800_operator", "try", bot]
    env = os.environ.copy()
    env["OPERATOR_BOT"] = bot

    pre_existing_terminals = _snapshot_history() if bot == "claude" else set()
    t0 = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(ROOT),
    )

    # Streamed buffers, accumulated by reader threads.
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    stdout_lock = threading.Lock()
    stderr_lock = threading.Lock()

    def _drain(stream, sink, lock):
        try:
            while True:
                chunk = stream.read(1)
                if not chunk:
                    break
                with lock:
                    sink.append(chunk)
        except Exception:
            pass

    t_out = threading.Thread(target=_drain, args=(proc.stdout, stdout_chunks, stdout_lock), daemon=True)
    t_err = threading.Thread(target=_drain, args=(proc.stderr, stderr_chunks, stderr_lock), daemon=True)
    t_out.start()
    t_err.start()

    def _stdout_text() -> str:
        with stdout_lock:
            return b"".join(stdout_chunks).decode("utf-8", errors="replace")

    def _stderr_text() -> str:
        with stderr_lock:
            return b"".join(stderr_chunks).decode("utf-8", errors="replace")

    # Wait for bot's "chat ready" banner on stderr so we know it's ready
    # to accept input. Banner text from terminal connector setup:
    # "chat ready — type to message…" written to stderr at line 801.
    ready_deadline = time.monotonic() + 90  # claude-cli + MCP can take a bit
    while time.monotonic() < ready_deadline:
        if "chat ready" in _stderr_text():
            break
        if proc.poll() is not None:
            return _bail_early(bot, queries, t0, _stdout_text(), _stderr_text(), proc.returncode, "bot died before ready banner")
        time.sleep(0.2)
    else:
        proc.kill()
        return _bail_early(bot, queries, t0, _stdout_text(), _stderr_text(), -1, "timeout waiting for chat ready banner")

    injection_target = None
    if bot == "claude":
        injection_target = _claude_inject_after_spawn(
            fixture_path, pre_existing_terminals, deadline_s=15
        )

    label_re = re.compile(r"\x1b\[36m\[([^\]]+)\]\x1b\[0m")

    # Use stderr's "✓ Replied — Xs" marker (chat_runner.py:683) as a
    # deterministic turn-end signal. The bot may emit multiple [Claude]
    # segments per turn (progress narration + final answer); collecting
    # everything between the queries' "Replied" markers is much more
    # reliable than idle-time detection.
    replied_re = re.compile(r"✓ Replied — [0-9.]+s")
    per_query_results = []
    for qi, q in enumerate(queries):
        baseline_replied = len(replied_re.findall(_stderr_text()))
        baseline_label_count = len(label_re.findall(_stdout_text()))
        try:
            proc.stdin.write((q + "\n").encode())
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            per_query_results.append({"query": q, "timed_out": True, "reply": None, "elapsed_s": 0})
            break

        q_start = time.monotonic()
        deadline = q_start + per_query_timeout_s
        reply = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            current_replied = len(replied_re.findall(_stderr_text()))
            if current_replied > baseline_replied:
                # Turn complete. Give a tiny grace period in case stdout's
                # final buffer hasn't flushed yet.
                time.sleep(0.4)
                stdout_now = _stdout_text()
                matches = list(label_re.finditer(stdout_now))
                # All segments emitted since baseline_label_count.
                segments = []
                for mi in range(baseline_label_count, len(matches)):
                    start = matches[mi].end()
                    end = matches[mi + 1].start() if mi + 1 < len(matches) else len(stdout_now)
                    segments.append(stdout_now[start:end].strip())
                reply = "\n--- next message ---\n".join(s for s in segments if s)
                break
            time.sleep(0.3)
        per_query_results.append({
            "query": q,
            "timed_out": reply is None,
            "reply": reply,
            "elapsed_s": round(time.monotonic() - q_start, 1),
        })
        if proc.poll() is not None:
            break

    # Send /quit to trigger clean shutdown
    try:
        proc.stdin.write(b"/quit\n")
        proc.stdin.flush()
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()

    elapsed = time.monotonic() - t0
    return {
        "bot": bot,
        "elapsed_seconds": round(elapsed, 1),
        "timed_out": False,
        "exit_code": proc.returncode,
        "stdout": _stdout_text(),
        "stderr_tail": _stderr_text()[-3000:],
        "queries": queries,
        "per_query": per_query_results,
        "claude_injection_target": str(injection_target) if injection_target else None,
    }


def _bail_early(bot, queries, t0, stdout, stderr, code, reason):
    return {
        "bot": bot,
        "elapsed_seconds": round(time.monotonic() - t0, 1),
        "timed_out": True,
        "exit_code": code,
        "stdout": stdout,
        "stderr_tail": stderr[-3000:],
        "queries": queries,
        "per_query": [],
        "bail_reason": reason,
    }


def _parse_replies(stdout: str, bot: str) -> list[str]:
    """Pull bot replies out of stdout. The terminal connector emits each
    reply prefixed with `\\n[<bot_name>]` (color-escaped). We strip ANSI
    and split on the prefix.
    """
    import re
    # Strip ANSI color codes
    clean = re.sub(r"\x1b\[[0-9;]*m", "", stdout)
    # Each reply starts with \n[<bot_name>] and ends at the next prompt or /quit.
    # The bot_name in the connector is config.AGENT_NAME, e.g. "Claude" / "Codex".
    label_pattern = re.compile(rf"\n\[(?:Claude|Codex|operator|Operator|{bot})\]\s*", re.IGNORECASE)
    parts = label_pattern.split(clean)
    # First chunk is everything before the first reply — discard.
    return [p.strip() for p in parts[1:] if p.strip()]


def run_one(bot: str, load: str) -> Path:
    """Run one (bot, load) cell. Returns path to the result JSON."""
    print(f"\n=== {bot} × {load} ===", flush=True)
    fixture_path = _ensure_fixture(load)
    fx_size = fixture_path.stat().st_size
    fx_lines = sum(1 for _ in fixture_path.open())
    print(f"  fixture: {fixture_path.name}  ({fx_lines} lines, {fx_size:,} bytes)", flush=True)

    if bot == "codex":
        _wire_path_codex(fixture_path)
    # claude path is handled inside _drive_bot via _claude_inject_after_spawn
    result = _drive_bot(bot, QUERIES, fixture_path, per_query_timeout_s=180)

    out_path = OUTPUTS_DIR / f"{bot}__{load}.json"
    per_query = result.get("per_query", [])
    n_answered = sum(1 for r in per_query if not r.get("timed_out"))
    out_path.write_text(json.dumps({
        **result,
        "load": load,
        "fixture_lines": fx_lines,
        "fixture_bytes": fx_size,
        "n_answered": n_answered,
    }, indent=2))
    print(f"  elapsed: {result['elapsed_seconds']}s  exit: {result['exit_code']}  "
          f"answered: {n_answered}/{len(QUERIES)}",
          flush=True)
    for i, qr in enumerate(per_query):
        reply = qr.get("reply") or "<no reply / timeout>"
        preview = reply.replace("\n", " ⏎ ")[:160]
        print(f"  Q{i+1} ({qr.get('elapsed_s', '?')}s) {qr['query']}", flush=True)
        print(f"      → {preview}", flush=True)
    return out_path


def main():
    bots = ["claude", "codex"]
    loads = ["realistic", "long-real", "heavy", "stress", "extreme"]

    if len(sys.argv) >= 2:
        if sys.argv[1] not in bots:
            print(f"Unknown bot: {sys.argv[1]}; choose from {bots}", file=sys.stderr)
            return 2
        bots = [sys.argv[1]]
    if len(sys.argv) >= 3:
        if sys.argv[2] not in loads:
            print(f"Unknown load: {sys.argv[2]}; choose from {loads}", file=sys.stderr)
            return 2
        loads = [sys.argv[2]]

    summary = []
    for bot in bots:
        for load in loads:
            try:
                out = run_one(bot, load)
                summary.append({"bot": bot, "load": load, "out": str(out), "ok": True})
            except Exception as e:
                summary.append({"bot": bot, "load": load, "error": repr(e), "ok": False})
                print(f"  ERROR: {e}", flush=True)
            finally:
                _unwire()

    summary_path = OUTPUTS_DIR / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    print(f"\n=== summary written to {summary_path} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
