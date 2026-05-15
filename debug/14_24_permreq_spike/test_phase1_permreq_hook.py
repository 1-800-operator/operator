#!/usr/bin/env python3
"""
Phase 1 verification harness for operator-plugin/hooks/scripts/permission_request.sh.

Tests the new hook in isolation (no real claude subprocess — that's
what the 14_24 spike already proved). Each test invokes the hook
directly via bash, simulates ChatRunner's role with the answer-file
write, and asserts:
  - exit code is always 0 (operator hook contract)
  - stderr is empty (no leaked "Permission denied" / python stderr)
  - the emitted JSON on stdout is the right hookSpecificOutput shape
  - happy-path answers pass through verbatim
  - every internal failure mode degrades to a JSON deny with a clear
    message — never a non-zero exit, never a missing decision

Run:
    python debug/14_24_permreq_spike/test_phase1_permreq_hook.py
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HOOK = pathlib.Path("/Users/jojo/Desktop/operator-plugin/hooks/scripts/permission_request.sh")

results = []

def expect(name, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    results.append((status, name, detail))
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))


def fresh_session_dir():
    return pathlib.Path(tempfile.mkdtemp(prefix="permreq_phase1_"))


def run_hook(stdin, *, session_dir, timeout_s=2, extra_env=None, wall_timeout=15):
    """Invoke the hook. Returns (rc, stdout, stderr, decision)."""
    env = os.environ.copy()
    env["OPERATOR_SESSION_DIR"] = str(session_dir)
    env["OPERATOR_PERMREQ_TIMEOUT_S"] = str(timeout_s)
    if extra_env:
        env.update(extra_env)
    p = subprocess.run(
        ["bash", str(HOOK)],
        input=stdin.encode() if isinstance(stdin, str) else stdin,
        env=env, capture_output=True, timeout=wall_timeout,
    )
    decision = None
    if p.stdout.strip():
        try:
            decision = json.loads(p.stdout)
        except json.JSONDecodeError:
            pass
    return p.returncode, p.stdout.decode(errors="replace"), p.stderr.decode(errors="replace"), decision


def write_answer_after(session_dir, request_id, answer, *, delay_s):
    """Background thread: wait `delay_s`, then atomically write the
    answer file ChatRunner would write."""
    def _go():
        time.sleep(delay_s)
        ans_dir = session_dir / "permreq_answers"
        ans_dir.mkdir(exist_ok=True)
        tmp = ans_dir / (request_id + ".json.tmp")
        out = ans_dir / (request_id + ".json")
        tmp.write_text(json.dumps(answer))
        os.replace(tmp, out)
    t = threading.Thread(target=_go, daemon=True)
    t.start()
    return t


def watch_for_request(session_dir, *, timeout_s=5):
    """Block until permreq_requests.jsonl gets its first line. Returns
    the request_id (or None on timeout)."""
    requests_path = session_dir / "permreq_requests.jsonl"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if requests_path.exists():
            for line in requests_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    return rec.get("request_id")
                except json.JSONDecodeError:
                    pass
        time.sleep(0.05)
    return None


def is_deny(decision):
    return (decision is not None
            and isinstance(decision, dict)
            and decision.get("hookSpecificOutput", {}).get("decision", {}).get("behavior") == "deny")


def deny_message(decision):
    if decision is None:
        return ""
    return decision.get("hookSpecificOutput", {}).get("decision", {}).get("message", "")


# -----------------------------------------------------------------------

print("\n=== permission_request.sh ===\n")

# P1: happy path — write a request, simulate ChatRunner replying allow,
# hook returns the answer wrapped in hookSpecificOutput.
print("P1: happy path (allow)")
sd = fresh_session_dir()
def p1():
    # ChatRunner equivalent: watch for the request, then write the answer.
    rid = watch_for_request(sd, timeout_s=5)
    if rid is None:
        return
    write_answer_after(sd, rid, {"behavior": "allow"}, delay_s=0)
threading.Thread(target=p1, daemon=True).start()
rc, out, err, dec = run_hook(
    json.dumps({"hook_event_name": "PermissionRequest",
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"}}),
    session_dir=sd, timeout_s=10,
)
ok = (rc == 0 and not err.strip()
      and dec is not None
      and dec.get("hookSpecificOutput", {}).get("hookEventName") == "PermissionRequest"
      and dec["hookSpecificOutput"]["decision"].get("behavior") == "allow")
expect("P1 happy allow: passes ChatRunner answer through verbatim", ok,
       f"rc={rc} stderr={err.strip()[:80]!r} decision={dec}")
shutil.rmtree(sd, ignore_errors=True)

# P1b: same shape, allow with updatedPermissions (auto-allow-this-meeting)
print("\nP1b: happy path with updatedPermissions")
sd = fresh_session_dir()
def p1b():
    rid = watch_for_request(sd, timeout_s=5)
    if rid is None:
        return
    write_answer_after(sd, rid, {
        "behavior": "allow",
        "updatedPermissions": [{
            "type": "addRules",
            "rules": [{"toolName": "Bash", "ruleContent": "echo:*"}],
            "behavior": "allow",
            "destination": "session",
        }],
    }, delay_s=0)
threading.Thread(target=p1b, daemon=True).start()
rc, out, err, dec = run_hook(
    json.dumps({"hook_event_name": "PermissionRequest",
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"}}),
    session_dir=sd, timeout_s=10,
)
inner = dec["hookSpecificOutput"]["decision"] if dec else {}
ok = (rc == 0 and not err.strip()
      and inner.get("behavior") == "allow"
      and isinstance(inner.get("updatedPermissions"), list)
      and len(inner["updatedPermissions"]) == 1)
expect("P1b allow + updatedPermissions: extra fields preserved", ok,
       f"rc={rc} decision={dec}")
shutil.rmtree(sd, ignore_errors=True)

# P2: timeout — no answer ever written, hook denies cleanly.
print("\nP2: timeout (no chat reply)")
sd = fresh_session_dir()
t0 = time.monotonic()
rc, out, err, dec = run_hook(
    json.dumps({"hook_event_name": "PermissionRequest",
                "tool_name": "Bash", "tool_input": {"command": "x"}}),
    session_dir=sd, timeout_s=2, wall_timeout=10,
)
elapsed = time.monotonic() - t0
ok_exit = rc == 0
ok_clean_err = not err.strip()
ok_deny = is_deny(dec)
ok_msg = "no response" in deny_message(dec).lower()
ok_timing = 1.5 <= elapsed <= 6.0
expect("P2 timeout: exits 0, clean stderr",
       ok_exit and ok_clean_err, f"rc={rc} stderr={err.strip()!r}")
expect("P2 timeout: returns JSON deny with 'no response' message",
       ok_deny and ok_msg, f"decision={dec}")
expect(f"P2 timeout: respects OPERATOR_PERMREQ_TIMEOUT_S (~2s, got {elapsed:.1f}s)",
       ok_timing)
shutil.rmtree(sd, ignore_errors=True)

# P3: malformed input on stdin — hook denies cleanly.
print("\nP3: unparseable hook input")
sd = fresh_session_dir()
rc, out, err, dec = run_hook("not-json{{{", session_dir=sd, timeout_s=2)
ok = (rc == 0 and not err.strip()
      and is_deny(dec)
      and "could not parse" in deny_message(dec).lower())
expect("P3 bad input: exits 0 with deny + 'could not parse' message", ok,
       f"rc={rc} decision={dec}")
shutil.rmtree(sd, ignore_errors=True)

# P3b: input is JSON but not an object — hook denies.
print("\nP3b: JSON input but not an object")
sd = fresh_session_dir()
rc, out, err, dec = run_hook("[1,2,3]", session_dir=sd, timeout_s=2)
ok = (rc == 0 and not err.strip()
      and is_deny(dec)
      and "could not parse" in deny_message(dec).lower())
expect("P3b non-object JSON: exits 0 with deny", ok,
       f"rc={rc} decision={dec}")
shutil.rmtree(sd, ignore_errors=True)

# P4: malformed answer file — ChatRunner wrote garbage; hook denies.
print("\nP4: ChatRunner wrote unparseable answer")
sd = fresh_session_dir()
def p4():
    rid = watch_for_request(sd, timeout_s=5)
    if rid is None:
        return
    ans_dir = sd / "permreq_answers"
    ans_dir.mkdir(exist_ok=True)
    (ans_dir / (rid + ".json")).write_text("not-json{{{")
threading.Thread(target=p4, daemon=True).start()
rc, out, err, dec = run_hook(
    json.dumps({"hook_event_name": "PermissionRequest",
                "tool_name": "Bash", "tool_input": {"command": "x"}}),
    session_dir=sd, timeout_s=10,
)
ok = (rc == 0 and not err.strip()
      and is_deny(dec)
      and "unparseable" in deny_message(dec).lower())
expect("P4 bad answer: exits 0 with deny + 'unparseable' message", ok,
       f"rc={rc} decision={dec}")
shutil.rmtree(sd, ignore_errors=True)

# P5: answer JSON is valid but missing 'behavior' field — hook denies.
print("\nP5: answer object missing 'behavior'")
sd = fresh_session_dir()
def p5():
    rid = watch_for_request(sd, timeout_s=5)
    if rid is None:
        return
    ans_dir = sd / "permreq_answers"
    ans_dir.mkdir(exist_ok=True)
    (ans_dir / (rid + ".json")).write_text(json.dumps({"oops": "no behavior"}))
threading.Thread(target=p5, daemon=True).start()
rc, out, err, dec = run_hook(
    json.dumps({"hook_event_name": "PermissionRequest",
                "tool_name": "Bash", "tool_input": {"command": "x"}}),
    session_dir=sd, timeout_s=10,
)
ok = (rc == 0 and not err.strip()
      and is_deny(dec)
      and "unparseable" in deny_message(dec).lower())
expect("P5 answer missing behavior: exits 0 with deny", ok,
       f"rc={rc} decision={dec}")
shutil.rmtree(sd, ignore_errors=True)

# P6: read-only session dir — can't enqueue request — hook denies.
print("\nP6: read-only session dir (cannot enqueue)")
sd = fresh_session_dir()
os.chmod(sd, 0o500)
try:
    rc, out, err, dec = run_hook(
        json.dumps({"hook_event_name": "PermissionRequest",
                    "tool_name": "Bash", "tool_input": {"command": "x"}}),
        session_dir=sd, timeout_s=2,
    )
    ok = (rc == 0 and not err.strip()
          and is_deny(dec)
          and "denied for safety" in deny_message(dec).lower())
    expect("P6 read-only dir: exits 0 with safety deny, no stderr leak", ok,
           f"rc={rc} stderr={err.strip()!r} decision={dec}")
finally:
    os.chmod(sd, 0o700)
    shutil.rmtree(sd, ignore_errors=True)

# P7: env var unset — hook no-ops cleanly (gate in _common.sh).
print("\nP7: $OPERATOR_SESSION_DIR unset")
env = {k: v for k, v in os.environ.items() if k != "OPERATOR_SESSION_DIR"}
p = subprocess.run(["bash", str(HOOK)], input=b'{"x":1}',
                   env=env, capture_output=True, timeout=5)
expect("P7 no env var: exits 0 silently",
       p.returncode == 0 and not p.stdout and not p.stderr,
       f"rc={p.returncode} stdout={p.stdout!r} stderr={p.stderr!r}")

# P8: python3 stubbed broken — last-ditch bash safe_emit_permreq_deny fires.
print("\nP8: python3 broken (last-ditch bash fallback)")
sd = fresh_session_dir()
stub_dir = pathlib.Path(tempfile.mkdtemp(prefix="stub_no_py_"))
stub = stub_dir / "python3"
stub.write_text("#!/bin/sh\nexit 1\n")
stub.chmod(0o755)
try:
    rc, out, err, dec = run_hook(
        json.dumps({"hook_event_name": "PermissionRequest",
                    "tool_name": "Bash", "tool_input": {"command": "x"}}),
        session_dir=sd, timeout_s=2,
        extra_env={"PATH": f"{stub_dir}:{os.environ.get('PATH','')}"},
    )
    # safe_emit_permreq_deny has its own python-less printf fallback.
    ok = (rc == 0
          and is_deny(dec)
          and ("crashed" in deny_message(dec).lower()
               or "fallback" in deny_message(dec).lower()))
    expect("P8 broken python3: last-ditch bash deny still fires", ok,
           f"rc={rc} stderr={err.strip()[:120]!r} decision={dec}")
finally:
    shutil.rmtree(stub_dir, ignore_errors=True)
    shutil.rmtree(sd, ignore_errors=True)

# -----------------------------------------------------------------------

print("\n" + "=" * 60)
fails = [r for r in results if r[0] == "FAIL"]
total = len(results)
print(f"{total - len(fails)}/{total} passed")
if fails:
    print("FAILURES:")
    for status, name, detail in fails:
        print(f"  - {name}: {detail}")
    sys.exit(1)
print("Phase 1 PermissionRequest hook: all checks PASS")
sys.exit(0)
