#!/usr/bin/env python3
"""
PermissionRequest probe for interactive (PTY-driven) claude — the
"yolo off" feasibility spike.

WHY THIS SPIKE EXISTS
---------------------
Operator spawns inner-claude with `--dangerously-skip-permissions`
because a normal spawn hits interactive approval dialogs in a PTY TUI
nobody is watching, and the meeting hangs. We want a "yolo off" mode.
The proposed mechanism: spawn WITHOUT the bypass flag and ship a
`PermissionRequest` hook that bridges the approval question into meeting
chat (operator posts it, watches chat for a yes/no, answers the hook).

`PermissionRequest` fires only for the "ask bucket" — tools that are
neither pre-allowed nor pre-denied — which is exactly the uncategorized
new-MCP-tool case the whole mode is for. But one thing is unverified:
does `PermissionRequest` actually fire in interactive PTY mode (vs.
headless `claude -p`), and can a *blocking* hook resolve the dialog
without the TUI hanging? `defer` is documented headless-only; we need to
confirm `PermissionRequest` is not similarly restricted before building.

WHAT IT TESTS  (each test = fresh non-bypass spawn, cwd=bench/)
--------------------------------------------------------------
  T1  fires + allow      PermissionRequest hook returns behavior=allow.
                         Proves the event fires in PTY mode and an
                         immediate allow lets the tool run + turn end.
  T2  blocking round-trip Hook writes a request, BLOCKS ~3s while the
                         driver simulates a human chat reply, then
                         returns allow. Proves the real operator
                         mechanism — a synchronous blocking hook —
                         resolves the dialog without hanging the TUI.
  T3  deny via exit 2    Hook exits 2. Proves the fail-safe deny path:
                         tool blocked, turn still completes.
  T4  deny via JSON      Hook returns behavior=deny + message. Proves a
                         structured deny + the reason reaching claude.
  T5  pre-allowed bypass bench settings allow the tool outright.
                         Proves PermissionRequest does NOT fire for
                         pre-categorised tools — the narrow-firing
                         property the design depends on.

Spawn shape: `claude --permission-mode default` (the permission layer is
ON — the opposite of operator's current `--dangerously-skip-permissions`
spawn). The bench `.claude/settings.json` is generated at runtime with
absolute hook paths.

CAVEAT — the user's own ~/.claude/settings.json still applies. If a
global `allow` rule pre-approves the probe command, PermissionRequest
won't fire (correctly) and T1 will read INCONCLUSIVE rather than FAIL.
The pre-check dumps the user's allow/deny so an inconclusive result is
self-explaining; pick a more exotic PROBE_CMD if it happens.

Usage:
    python spike_permreq.py
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

REPLIES = STATE / "replies.jsonl"          # Stop hook   -> turn completed
TOOL_EVENTS = STATE / "tool_events.jsonl"  # PreToolUse  -> tool attempted
PERMREQ_EVENTS = STATE / "permreq_events.jsonl"   # PermissionRequest fired
PERMREQ_REQUESTS = STATE / "permreq_requests.jsonl"  # block_allow round-trip
PERMREQ_ANSWER = STATE / "permreq_answer.json"        # driver writes the reply

HOOK_STOP = BENCH / "hook_stop.sh"
HOOK_PRETOOL = BENCH / "hook_pretool.sh"
HOOK_PERMREQ = BENCH / "hook_permreq.sh"

CANARY_DIR = pathlib.Path("/tmp/permreq_spike")

SETTLE_SECONDS = 6.0
TURN_TIMEOUT = 90.0
ROUNDTRIP_REPLY_DELAY = 3.0   # simulated "human reads chat and replies"


# ---- PTY plumbing (same shape as the 14.22 spikes) ---------------------

def set_winsize(fd, rows=40, cols=120):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def send_d(fd, msg):
    """Bracketed-paste wrap + CR — the proven universal input strategy."""
    os.write(fd, b"\x1b[200~"); time.sleep(0.05)
    os.write(fd, msg.encode()); time.sleep(0.1)
    os.write(fd, b"\x1b[201~"); time.sleep(0.2)
    os.write(fd, b"\r")


def spawn(permreq_mode):
    env = os.environ.copy()
    env["PERMREQ_MODE"] = permreq_mode
    master_fd, slave_fd = pty.openpty()
    set_winsize(master_fd)
    # The whole point: NO --dangerously-skip-permissions. Force the
    # permission layer on explicitly so a global defaultMode can't
    # silently turn it back into bypass.
    cmd = ["claude", "--permission-mode", "default"]
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


def wait_for_reply(prev, timeout, fd, buf, on_poll=None):
    """Tail replies.jsonl until count > prev or timeout. Drains the PTY
    and fires on_poll every iteration (T2 uses it for the round-trip)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        drain(fd, buf)
        if on_poll is not None:
            on_poll()
        if REPLIES.exists():
            with REPLIES.open() as f:
                lines = f.readlines()
            if len(lines) > prev:
                return json.loads(lines[prev])
        time.sleep(0.15)
    return None


# ---- bench harness setup ----------------------------------------------

def write_settings(allow_rules=None):
    """(Re)generate bench/.claude/settings.json with absolute hook paths.

    allow_rules: optional list of permission rule strings to drop into
    permissions.allow — T5 uses this to pre-allow the probe tool.
    """
    DOT_CLAUDE.mkdir(parents=True, exist_ok=True)
    cfg = {
        "hooks": {
            "Stop": [
                {"hooks": [{"type": "command", "command": str(HOOK_STOP)}]}
            ],
            "PreToolUse": [
                {"matcher": "*",
                 "hooks": [{"type": "command", "command": str(HOOK_PRETOOL)}]}
            ],
            "PermissionRequest": [
                {"matcher": "*",
                 "hooks": [{"type": "command", "command": str(HOOK_PERMREQ)}]}
            ],
        }
    }
    if allow_rules:
        cfg["permissions"] = {"allow": list(allow_rules)}
    SETTINGS.write_text(json.dumps(cfg, indent=2) + "\n")


def reset_state():
    STATE.mkdir(parents=True, exist_ok=True)
    for p in (REPLIES, TOOL_EVENTS, PERMREQ_EVENTS,
              PERMREQ_REQUESTS, PERMREQ_ANSWER):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    # Wipe canaries so a previous run can't read as a pass.
    if CANARY_DIR.exists():
        for f in CANARY_DIR.iterdir():
            try:
                f.unlink()
            except OSError:
                pass


def read_jsonl(path):
    if not path.exists():
        return []
    out = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def dump_user_permissions():
    """Print the user's global allow/deny so an INCONCLUSIVE T1/T5 is
    self-explaining (a global allow rule pre-approving the probe is the
    most likely cause)."""
    path = pathlib.Path.home() / ".claude" / "settings.json"
    print(f"--- user global permissions ({path}) ---")
    try:
        cfg = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"  (could not read: {e})")
        return
    perms = cfg.get("permissions", {}) if isinstance(cfg, dict) else {}
    for bucket in ("allow", "deny", "ask"):
        vals = perms.get(bucket, [])
        print(f"  {bucket}: {vals if vals else '(none)'}")
    print(f"  defaultMode: {perms.get('defaultMode', '(unset)')}")
    print()


# ---- the probe turn ----------------------------------------------------

def probe_prompt(tag):
    """An imperative single-tool turn. The command is compound (mkdir &&
    printf) so a simple prefix allow rule is unlikely to match it — that
    keeps PermissionRequest in play. The canary file is the proof the
    tool actually ran."""
    canary = CANARY_DIR / f"{tag}.txt"
    cmd = f"mkdir -p {CANARY_DIR} && printf '{tag}-canary\\n' > {canary}"
    prompt = (
        "Use the Bash tool to run exactly this one command, then stop. "
        "Do not describe it first, do not run anything else:\n\n"
        f"{cmd}"
    )
    return prompt, canary


# ---- one test ----------------------------------------------------------

def run_test(name, permreq_mode, *, allow_rules=None, roundtrip=False):
    print(f"\n=== {name}  (PERMREQ_MODE={permreq_mode}"
          f"{', pre-allowed' if allow_rules else ''}) ===")
    write_settings(allow_rules=allow_rules)
    reset_state()

    prompt, canary = probe_prompt(name)

    proc, fd = spawn(permreq_mode)
    buf = bytearray()
    t0 = time.monotonic()
    while time.monotonic() - t0 < SETTLE_SECONDS:
        drain(fd, buf)

    # T2 round-trip: when the hook posts a request, wait a beat (simulate
    # a human reading meeting chat) then write the allow answer.
    rt = {"seen_at": None, "answered": False}

    def roundtrip_poll():
        if rt["answered"]:
            return
        if PERMREQ_REQUESTS.exists() and PERMREQ_REQUESTS.stat().st_size > 0:
            if rt["seen_at"] is None:
                rt["seen_at"] = time.monotonic()
                print(f"  {name}: permission request seen — simulating "
                      f"{ROUNDTRIP_REPLY_DELAY:.0f}s human reply delay")
            elif time.monotonic() - rt["seen_at"] >= ROUNDTRIP_REPLY_DELAY:
                PERMREQ_ANSWER.write_text(json.dumps({"behavior": "allow"}))
                rt["answered"] = True
                print(f"  {name}: wrote allow answer to permreq_answer.json")

    t_send = time.monotonic()
    send_d(fd, prompt)
    reply = wait_for_reply(
        0, TURN_TIMEOUT, fd, buf,
        on_poll=roundtrip_poll if roundtrip else None,
    )
    elapsed = time.monotonic() - t_send
    teardown(proc, fd)

    # ---- collect signals ----
    permreq_events = read_jsonl(PERMREQ_EVENTS)
    tool_events = read_jsonl(TOOL_EVENTS)
    permreq_fired = len(permreq_events) > 0
    pretool_fired = len(tool_events) > 0
    stop_fired = reply is not None
    canary_exists = canary.exists()

    permreq_tool = None
    if permreq_fired:
        inp = permreq_events[0].get("input", {})
        permreq_tool = {
            "tool_name": inp.get("tool_name"),
            "tool_input": inp.get("tool_input"),
            "permission_mode": inp.get("permission_mode"),
        }
    last_msg = ""
    if reply:
        last_msg = (reply.get("input", {}) or {}).get(
            "last_assistant_message", "") or ""

    # ---- verdict ----
    # Expectations differ per test; see each branch.
    verdict, reason = _verdict(
        name, permreq_mode, allow_rules,
        permreq_fired, pretool_fired, stop_fired, canary_exists,
    )

    print(f"  permreq_fired={permreq_fired}  pretool_fired={pretool_fired}  "
          f"stop_fired={stop_fired}  canary_exists={canary_exists}  "
          f"elapsed={elapsed:.1f}s")
    if permreq_tool:
        print(f"  permreq saw: tool_name={permreq_tool['tool_name']!r} "
              f"permission_mode={permreq_tool['permission_mode']!r}")
    if last_msg:
        print(f"  claude said: {last_msg[:160]!r}")
    print(f"  --> {verdict}: {reason}")

    return {
        "name": name,
        "permreq_mode": permreq_mode,
        "pre_allowed": bool(allow_rules),
        "verdict": verdict,
        "reason": reason,
        "signals": {
            "permreq_fired": permreq_fired,
            "pretool_fired": pretool_fired,
            "stop_fired": stop_fired,
            "canary_exists": canary_exists,
            "elapsed_s": round(elapsed, 1),
        },
        "permreq_tool": permreq_tool,
        "claude_last_message": last_msg,
    }


def _verdict(name, mode, allow_rules,
             permreq_fired, pretool_fired, stop_fired, canary_exists):
    """Per-test pass/fail logic. The critical FAIL is T1/T2 hanging:
    PermissionRequest never fired and the turn never completed -> the
    dialog is stuck in the TUI and the yolo-off design does not work as
    proposed."""
    if not pretool_fired:
        return ("INCONCLUSIVE",
                "claude never attempted the tool — reprompt or check the "
                "model declined to run it")

    # T5 — pre-allowed: PermissionRequest must NOT fire; tool runs natively.
    if allow_rules:
        if not permreq_fired and canary_exists and stop_fired:
            return ("PASS",
                    "pre-allowed tool ran natively; PermissionRequest "
                    "correctly did NOT fire (narrow-firing confirmed)")
        if permreq_fired:
            return ("FAIL",
                    "PermissionRequest fired for a pre-allowed tool — it "
                    "is NOT narrow-firing; design assumption broken")
        return ("INCONCLUSIVE",
                "pre-allowed path did not complete cleanly — inspect state")

    # T3 / T4 — deny paths: hook must fire, tool must NOT run, turn still ends.
    if mode in ("deny_exit2", "deny_json"):
        if not permreq_fired:
            if not stop_fired:
                return ("FAIL-CRITICAL",
                        "PermissionRequest did NOT fire AND the turn hung — "
                        "the approval dialog is stuck in the PTY TUI")
            return ("INCONCLUSIVE",
                    "PermissionRequest did not fire but the turn ended — "
                    "tool was likely pre-allowed/denied by global settings")
        if canary_exists:
            return ("FAIL",
                    "hook denied the call but the tool ran anyway — deny "
                    "was not honored")
        if not stop_fired:
            return ("FAIL",
                    "denied correctly but the turn never completed — claude "
                    "may be retry-looping on the denial")
        return ("PASS",
                "PermissionRequest fired, deny honored (tool did not run), "
                "turn still completed")

    # T1 / T2 — allow paths: hook must fire, tool must run, turn ends.
    if not permreq_fired:
        if not stop_fired:
            return ("FAIL-CRITICAL",
                    "PermissionRequest did NOT fire AND the turn hung — the "
                    "approval dialog is stuck in the PTY TUI. The yolo-off "
                    "design as proposed does not work; rethink needed.")
        if canary_exists:
            return ("INCONCLUSIVE",
                    "tool ran without PermissionRequest firing — it was "
                    "pre-allowed by the user's global ~/.claude settings. "
                    "Re-run with a PROBE_CMD not covered by your allow list.")
        return ("INCONCLUSIVE",
                "PermissionRequest did not fire and the tool did not run — "
                "possibly pre-denied by global settings, or claude declined")
    if not canary_exists:
        return ("FAIL",
                "PermissionRequest fired and the hook allowed, but the tool "
                "did not run — allow decision not honored")
    if not stop_fired:
        return ("FAIL",
                "tool ran but the turn never completed — investigate the "
                "post-allow path")
    return ("PASS",
            "PermissionRequest fired in PTY mode, the hook's allow was "
            "honored, the tool ran, and the turn completed cleanly")


# ---- main --------------------------------------------------------------

def main() -> int:
    if not all(h.exists() for h in (HOOK_STOP, HOOK_PRETOOL, HOOK_PERMREQ)):
        print("missing hook scripts in bench/ — abort", file=sys.stderr)
        return 1
    for h in (HOOK_STOP, HOOK_PRETOOL, HOOK_PERMREQ):
        os.chmod(h, 0o755)

    print("PermissionRequest PTY-mode spike — yolo-off feasibility\n")
    dump_user_permissions()

    results = []
    results.append(run_test("T1", "allow"))
    results.append(run_test("T2", "block_allow", roundtrip=True))
    results.append(run_test("T3", "deny_exit2"))
    results.append(run_test("T4", "deny_json"))
    results.append(run_test("T5", "allow", allow_rules=["Bash"]))

    # Restore a clean default settings file for the next manual run.
    write_settings()

    # ---- overall verdict ----
    # The spike's headline question is answered by T1+T2: does
    # PermissionRequest fire in PTY mode and can a (blocking) hook
    # resolve it. A FAIL-CRITICAL anywhere kills the design.
    verdicts = {r["name"]: r["verdict"] for r in results}
    critical = [r["name"] for r in results
                if r["verdict"] == "FAIL-CRITICAL"]
    core_pass = verdicts.get("T1") == "PASS" and verdicts.get("T2") == "PASS"

    if critical:
        overall = "FAIL-CRITICAL"
        headline = (f"PermissionRequest does not fire in interactive PTY "
                    f"mode ({', '.join(critical)} hung). The yolo-off "
                    f"design needs a rethink.")
    elif core_pass:
        overall = "PASS"
        headline = ("PermissionRequest fires in PTY mode and a blocking "
                    "hook resolves the dialog without hanging the TUI — "
                    "the yolo-off mechanism is viable.")
    else:
        overall = "INCONCLUSIVE"
        headline = ("Core tests did not cleanly pass — most likely the "
                    "probe command was pre-allowed/denied by global "
                    "settings. See per-test reasons and the permissions "
                    "dump above.")

    print("\n" + "=" * 68)
    print(f"OVERALL: {overall}")
    print(headline)
    for r in results:
        print(f"  {r['name']}: {r['verdict']}  — {r['reason']}")
    print("=" * 68)

    out = ROOT / "out_permreq_results.json"
    out.write_text(json.dumps({
        "overall": overall,
        "headline": headline,
        "tests": results,
    }, indent=2) + "\n")
    print(f"\nwrote {out}")

    return 0 if overall == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
