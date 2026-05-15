#!/usr/bin/env python3
"""
Interactive-classifier spike — one long-lived classifier claude per
"meeting", each permission ask is one tiny turn against it.

WHY THIS SPIKE EXISTS
---------------------
The 14_25 spike showed the deny+verbatim+retry pattern doesn't work:
claude treats hook deny-message text as suspected prompt injection
(same dynamic that killed Stop-block in 14.22). So we can't ask the
same inner-claude session to interpret the user's reply via the hook
channel.

Alternative: spin up a SEPARATE long-lived interactive claude as a
classifier sidecar. Per permission ask, send it one tiny turn:
"In a meeting, the user said '<reply>' to '<question>'. Did they
approve? Reply only YES or NO." Each ask gets a clean user-turn
input — no tool-result channel, no prompt-injection defense — and
each turn costs only ~50 tokens against the subscription pool.

WHAT THIS SPIKE TESTS
---------------------
  1. RELIABILITY — does the classifier reliably parse YES from common
     approvals (yes / sure / okay / sounds good / 👍 / sí adelante /
     do it / yeah / go ahead) and NO from refusals (no / nah / not now
     / skip it / don't)?
  2. LATENCY — how fast is a single classifier turn end-to-end (paste
     + Stop hook fires)? We need ~1-3s to be a usable meeting UX.
  3. AMBIGUOUS handling — what does the classifier say for unclear
     replies ("hmm sure but maybe later", "what would that do?", "?")?
     We don't expect a "right" answer — we just want to see the
     model's tendency (default to NO is safer than default to YES).

VERDICT
-------
  - Approvals all → YES, refusals all → NO, latency 1-3s/turn:
      VIABLE. Adopt this for yolo-off mode. Phase 2 rip-and-replace.
  - Approvals or refusals misclassified:
      Same prompt-injection-defense risk we saw in v2; classifier
      doesn't help. Fall back to (B) word-bag matcher or (C) drop
      yolo-off.
  - Latency >5s/turn:
      UX is too slow for a meeting; reconsider.

Usage:
    python debug/14_26_classifier_spike/spike_classifier.py
"""

from __future__ import annotations

import fcntl
import json
import os
import pathlib
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time

ROOT = pathlib.Path(__file__).parent
BENCH = ROOT / "bench"
STATE = BENCH / "state"
DOT_CLAUDE = BENCH / ".claude"
SETTINGS = DOT_CLAUDE / "settings.json"
REPLIES = STATE / "replies.jsonl"
HOOK_STOP = BENCH / "hook_stop.sh"

SETTLE_SECONDS = 6.0   # PTY/TUI settle before the first paste
TURN_TIMEOUT = 30.0    # Per-classifier-turn timeout


# ---- PTY plumbing ----------------------------------------------------

def set_winsize(fd, rows=40, cols=120):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def send_d(fd, msg):
    os.write(fd, b"\x1b[200~"); time.sleep(0.05)
    os.write(fd, msg.encode()); time.sleep(0.1)
    os.write(fd, b"\x1b[201~"); time.sleep(0.2)
    os.write(fd, b"\r")


def spawn():
    env = os.environ.copy()
    master_fd, slave_fd = pty.openpty()
    set_winsize(master_fd)
    # Naked spawn except --dangerously-skip-permissions: the classifier
    # shouldn't use tools, but if it ever decides to, skip-permissions
    # prevents a tool prompt from hanging the turn. Subscription pool —
    # no -p, no --append-system-prompt, no --mcp-config (the naked-
    # spawn invariant from 14.22).
    cmd = ["claude", "--dangerously-skip-permissions"]
    proc = subprocess.Popen(
        cmd, cwd=str(BENCH),
        stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        preexec_fn=os.setsid, env=env,
    )
    os.close(slave_fd)
    return proc, master_fd


def teardown(proc, fd):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        os.close(fd)
    except OSError:
        pass


def drain(fd, buf):
    r, _, _ = select.select([fd], [], [], 0.05)
    if r:
        try:
            c = os.read(fd, 4096)
            if c:
                buf.extend(c)
        except OSError:
            pass


def wait_for_reply(prev, timeout, fd, buf):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        drain(fd, buf)
        if REPLIES.exists():
            with REPLIES.open() as f:
                lines = f.readlines()
            if len(lines) > prev:
                return json.loads(lines[prev])
        time.sleep(0.15)
    return None


# ---- bench setup -----------------------------------------------------

def write_settings():
    DOT_CLAUDE.mkdir(parents=True, exist_ok=True)
    cfg = {
        "hooks": {
            "Stop": [
                {"hooks": [{"type": "command", "command": str(HOOK_STOP)}]}
            ],
        }
    }
    SETTINGS.write_text(json.dumps(cfg, indent=2) + "\n")


def reset_state():
    STATE.mkdir(parents=True, exist_ok=True)
    if REPLIES.exists():
        try:
            REPLIES.unlink()
        except OSError:
            pass


# ---- classification --------------------------------------------------

QUESTION = (
    "Claude wants to use the Bash tool to run a command in this meeting. "
    "Reply yes or no."
)

# A focused single-purpose prompt. Plain English, asks for one of two
# tokens, gives a fail-safe default. Same prompt structure operator
# would use in production.
def build_classifier_prompt(reply_text):
    return (
        "You are helping me interpret a participant's reply in a Google "
        "Meet chat. The bot just asked them a permission question. I "
        "need to know whether they approved.\n\n"
        f"The bot asked:\n> {QUESTION}\n\n"
        f"The participant replied:\n> {reply_text!r}\n\n"
        "Did they approve the request? Reply with exactly one word: "
        "YES if they approved, NO if they declined or were unclear. "
        "If you're unsure, reply NO (deny is the safe default)."
    )


def parse_yesno(text):
    """Parse the classifier's response. Looks for YES or NO as a
    standalone token. Returns 'YES', 'NO', or 'UNCLEAR'."""
    if not text:
        return "UNCLEAR"
    t = text.strip().upper()
    # Cheap robust check — first standalone YES or NO wins.
    import re
    m = re.search(r"\b(YES|NO)\b", t)
    return m.group(1) if m else "UNCLEAR"


# ---- per-scenario classify call --------------------------------------

def classify(reply_text, prev_reply_count, fd, buf):
    """Send one classification turn into the (already running) classifier
    session. Returns dict with the parsed verdict + latency + raw reply.
    """
    prompt = build_classifier_prompt(reply_text)
    t0 = time.monotonic()
    send_d(fd, prompt)
    reply = wait_for_reply(prev_reply_count, TURN_TIMEOUT, fd, buf)
    elapsed = time.monotonic() - t0
    if reply is None:
        return {
            "verdict": "TIMEOUT",
            "raw": "",
            "latency_s": round(elapsed, 2),
        }
    text = (reply.get("input", {}) or {}).get("last_assistant_message", "") or ""
    verdict = parse_yesno(text)
    return {
        "verdict": verdict,
        "raw": text,
        "latency_s": round(elapsed, 2),
    }


# ---- scenarios (same set as the v2 spike for direct comparison) -----

SCENARIOS = [
    # Approvals — expected YES
    ("approval", "yes"),
    ("approval", "sure"),
    ("approval", "okay"),
    ("approval", "do it"),
    ("approval", "go ahead"),
    ("approval", "yeah"),
    ("approval", "sounds good"),
    ("approval", "👍"),
    # Refusals — expected NO
    ("refusal", "no"),
    ("refusal", "nah"),
    ("refusal", "not now"),
    ("refusal", "skip it"),
    ("refusal", "don't"),
    # Ambiguous — observation only (we recommend "if unsure, NO" so
    # those should mostly come back NO; that's the safer default).
    ("ambiguous", "hmm sure but maybe later"),
    ("ambiguous", "what would that do?"),
    ("ambiguous", "?"),
    # Modified-intent
    ("modified", "yes but use --dry-run"),
    # Non-English approval
    ("non_english", "sí, adelante"),
    # Edge: empty
    ("edge", ""),
]


def expected(category):
    if category == "approval":
        return "YES"
    if category == "refusal":
        return "NO"
    return None  # ambiguous / modified / edge — observation only


# ---- main ------------------------------------------------------------

def main() -> int:
    if not HOOK_STOP.exists():
        print("missing hook_stop.sh — abort", file=sys.stderr)
        return 1
    os.chmod(HOOK_STOP, 0o755)

    print("Interactive classifier spike — one long-lived sidecar claude\n")
    write_settings()
    reset_state()

    print(f"Spawning classifier... (settle {SETTLE_SECONDS:.0f}s)")
    t_spawn = time.monotonic()
    proc, fd = spawn()
    buf = bytearray()
    while time.monotonic() - t_spawn < SETTLE_SECONDS:
        drain(fd, buf)
    boot_settle_s = time.monotonic() - t_spawn
    print(f"  boot+settle: {boot_settle_s:.1f}s\n")

    results = []
    prev_count = 0
    try:
        for category, reply in SCENARIOS:
            short = reply if len(reply) <= 40 else reply[:37] + "..."
            print(f"=== {category}: reply={short!r} ===")
            r = classify(reply, prev_count, fd, buf)
            prev_count += 1
            exp = expected(category)
            r["category"] = category
            r["reply"] = reply
            r["expected"] = exp
            r["match"] = (
                "MATCH" if exp is None else
                "MATCH" if r["verdict"] == exp else
                "MISMATCH"
            )
            results.append(r)
            short_raw = r["raw"][:120].replace("\n", " ")
            print(f"  verdict={r['verdict']:<8} latency={r['latency_s']:.2f}s  "
                  f"expected={exp or '-':<3}  {r['match']}")
            if r["raw"] and r["verdict"] == "UNCLEAR":
                print(f"  raw: {short_raw!r}")
            print()
    finally:
        teardown(proc, fd)

    # ---- summary ---------------------------------------------------------
    print("=" * 72)
    print("SUMMARY")
    print("=" * 72)

    cats = {}
    for r in results:
        cats.setdefault(r["category"], []).append(r)

    for cat in ("approval", "refusal", "ambiguous", "modified",
                "non_english", "edge"):
        rows = cats.get(cat, [])
        if not rows:
            continue
        print(f"\n{cat} ({len(rows)} scenarios):")
        for r in rows:
            short = r["reply"][:30] + ("..." if len(r["reply"]) > 30 else "")
            print(f"  {short!r:<35} → {r['verdict']:<8} "
                  f"({r['latency_s']:.2f}s)  {r['match']}")

    approvals = cats.get("approval", [])
    refusals = cats.get("refusal", [])
    appr_yes = sum(1 for r in approvals if r["verdict"] == "YES")
    ref_no = sum(1 for r in refusals if r["verdict"] == "NO")
    ref_yes = sum(1 for r in refusals if r["verdict"] == "YES")
    appr_no = sum(1 for r in approvals if r["verdict"] == "NO")
    avg_latency = (
        sum(r["latency_s"] for r in results) / max(len(results), 1)
    )

    print("\n" + "-" * 72)
    print(f"approvals correct: {appr_yes}/{len(approvals)}  "
          f"misclassified as NO: {appr_no}/{len(approvals)}")
    print(f"refusals correct:  {ref_no}/{len(refusals)}  "
          f"misclassified as YES (UNSAFE): {ref_yes}/{len(refusals)}")
    print(f"avg classifier turn latency: {avg_latency:.2f}s "
          f"(boot+settle: {boot_settle_s:.1f}s)")

    print("\n" + "-" * 72)
    print("RECOMMENDATION:")
    if not approvals or not refusals:
        print("  Insufficient data.")
    elif ref_yes > 0:
        print("  NOT VIABLE: classifier said YES on at least one explicit refusal — UNSAFE.")
    elif appr_yes == len(approvals) and ref_no == len(refusals) and avg_latency <= 5.0:
        print("  VIABLE: clean classification on every common reply, latency in")
        print("  the 1-3s/turn range. Adopt the interactive-classifier sidecar")
        print("  for yolo-off mode. Rip-and-replace Phase 2's matcher.")
    elif appr_yes >= 0.8 * len(approvals) and ref_no == len(refusals):
        print("  VIABLE WITH ONE CAVEAT: most approvals classified correctly,")
        print("  refusals safe. The misclassified approvals will frustrate users")
        print("  (their 'yes' silently denied) — worth a small prompt iteration.")
    elif avg_latency > 5.0:
        print(f"  TOO SLOW: avg {avg_latency:.1f}s/turn exceeds the meeting-UX")
        print("  target. Investigate boot, prompt size, or fall back to (B)/(C).")
    else:
        print("  See per-scenario rows; classifier's misses suggest the prompt")
        print("  needs tuning OR fall back to (B) word-bag / (C) drop yolo-off.")

    out = ROOT / "out_classifier_results.json"
    out.write_text(json.dumps({
        "boot_settle_s": round(boot_settle_s, 2),
        "scenarios": results,
    }, indent=2, default=str) + "\n")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
