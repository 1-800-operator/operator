#!/usr/bin/env python3
"""
Phase 0 verification harness for the operator-plugin hook hardening.

Tests the refactored session_start.sh and stop.sh against:
  - Happy-path output preservation (provider depends on exact JSON shape).
  - Fault injection — read-only session dir, no python3 on PATH, malformed
    input — confirms each script always exits 0 and degrades safely.

Why this lives in debug/14_24_permreq_spike/: Phase 0 sets the foundation
the Phase 1 PermissionRequest hook builds on; keeping the verification
near the spike keeps the related artifacts together. Operator-plugin
itself has no test suite (yet), and per-script unit tests would be a
larger scope than Phase 0 warrants.

Run:
    python debug/14_24_permreq_spike/test_phase0_hook_audit.py
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

PLUGIN = pathlib.Path("/Users/jojo/Desktop/operator-plugin/hooks/scripts")
SESSION_START = PLUGIN / "session_start.sh"
STOP = PLUGIN / "stop.sh"


# ----- helpers ---------------------------------------------------------

def run(script, *, stdin, env_extra=None, timeout=10):
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    p = subprocess.run(
        ["bash", str(script)],
        input=stdin.encode(),
        env=env,
        capture_output=True,
        timeout=timeout,
    )
    return p.returncode, p.stdout.decode(errors="replace"), p.stderr.decode(errors="replace")


def fresh_session_dir():
    return pathlib.Path(tempfile.mkdtemp(prefix="permreq_phase0_"))


# ----- assertions per scenario -----------------------------------------

results = []

def expect(name, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    results.append((status, name, detail))
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))


# ----- session_start.sh tests ------------------------------------------

print("\n=== session_start.sh ===\n")

# S1: happy path — valid JSON input → ready.flag has parsed payload + ts
print("S1: happy path (valid JSON in)")
sd = fresh_session_dir()
payload = {"session_id": "abc", "transcript_path": "/tmp/x.jsonl", "source": "spike"}
rc, _, _ = run(SESSION_START, stdin=json.dumps(payload),
               env_extra={"OPERATOR_SESSION_DIR": str(sd)})
flag = sd / "ready.flag"
ok = rc == 0 and flag.exists()
detail = ""
parsed = None
if ok:
    try:
        parsed = json.loads(flag.read_text())
        ok = (parsed.get("session_id") == "abc"
              and parsed.get("transcript_path") == "/tmp/x.jsonl"
              and "ts" in parsed and isinstance(parsed["ts"], (int, float)))
        detail = f"payload={parsed}"
    except Exception as e:
        ok = False
        detail = f"ready.flag not valid JSON: {e}"
expect("S1 happy path: ready.flag has merged payload + ts", ok, detail)
shutil.rmtree(sd, ignore_errors=True)

# S2: malformed JSON in → ready.flag exists with at least {ts}
print("\nS2: malformed JSON input")
sd = fresh_session_dir()
rc, _, _ = run(SESSION_START, stdin="not-json{{{",
               env_extra={"OPERATOR_SESSION_DIR": str(sd)})
flag = sd / "ready.flag"
ok = rc == 0 and flag.exists()
detail = ""
if ok:
    try:
        parsed = json.loads(flag.read_text())
        ok = "ts" in parsed
        detail = f"payload={parsed}"
    except Exception as e:
        ok = False
        detail = f"ready.flag not valid JSON: {e}"
expect("S2 bad JSON: still produces ready.flag with at least {ts}", ok, detail)
shutil.rmtree(sd, ignore_errors=True)

# S3: read-only session dir — exit 0, no crash
print("\nS3: read-only session dir")
sd = fresh_session_dir()
os.chmod(sd, 0o500)  # r-x: can't write
try:
    rc, out, err = run(SESSION_START, stdin=json.dumps({"x": 1}),
                       env_extra={"OPERATOR_SESSION_DIR": str(sd)})
    expect("S3 read-only dir: exits 0", rc == 0,
           f"rc={rc}, stderr={err.strip()[:200]!r}")
finally:
    os.chmod(sd, 0o700)
    shutil.rmtree(sd, ignore_errors=True)

# S4: no $OPERATOR_SESSION_DIR — no-op exit 0
print("\nS4: env var unset (user's normal Claude Code session)")
env = {k: v for k, v in os.environ.items() if k != "OPERATOR_SESSION_DIR"}
p = subprocess.run(["bash", str(SESSION_START)], input=b'{"x":1}',
                   env=env, capture_output=True, timeout=5)
expect("S4 no env var: exits 0 silently",
       p.returncode == 0 and not p.stdout and not p.stderr,
       f"rc={p.returncode}")


# ----- stop.sh tests ---------------------------------------------------

print("\n=== stop.sh ===\n")

# T1: happy path — valid JSON in → kind:"stop" row with parsed input
print("T1: happy path (valid JSON in)")
sd = fresh_session_dir()
hook_payload = {"hook_event_name": "Stop",
                "session_id": "sess-1",
                "transcript_path": "/tmp/t.jsonl",
                "last_assistant_message": "hello"}
rc, _, _ = run(STOP, stdin=json.dumps(hook_payload),
               env_extra={"OPERATOR_SESSION_DIR": str(sd)})
replies = sd / "replies.jsonl"
ok = rc == 0 and replies.exists()
detail = ""
if ok:
    rows = [json.loads(l) for l in replies.read_text().splitlines() if l.strip()]
    ok = (len(rows) == 1 and rows[0].get("kind") == "stop"
          and rows[0]["input"].get("session_id") == "sess-1"
          and rows[0]["input"].get("last_assistant_message") == "hello"
          and isinstance(rows[0].get("ts"), (int, float)))
    detail = f"row={rows[0]}"
expect("T1 happy path: kind=stop with parsed input + ts", ok, detail)
shutil.rmtree(sd, ignore_errors=True)

# T2: malformed JSON → kind:"raw" row with raw bytes
print("\nT2: malformed JSON input")
sd = fresh_session_dir()
rc, _, _ = run(STOP, stdin="not-json{{{",
               env_extra={"OPERATOR_SESSION_DIR": str(sd)})
replies = sd / "replies.jsonl"
ok = rc == 0 and replies.exists()
detail = ""
if ok:
    rows = [json.loads(l) for l in replies.read_text().splitlines() if l.strip()]
    ok = (len(rows) == 1 and rows[0].get("kind") == "raw"
          and rows[0].get("raw") == "not-json{{{")
    detail = f"row={rows[0]}"
expect("T2 bad JSON: kind=raw row written", ok, detail)
shutil.rmtree(sd, ignore_errors=True)

# T3: python3 broken — falls through to bash crashed-row fallback
print("\nT3: python3 broken (fallback path)")
sd = fresh_session_dir()
# Realistic scenario: python3 exists on PATH but is broken (runtime
# error, missing stdlib, whatever) — everything else (dirname, cat,
# mkdir, date) still works, so _common.sh loads cleanly. Stub a fake
# python3 ahead of the real one. ts_now falls back to `date +%s.000`;
# the row-building python pipeline fails, taking the crashed-row
# branch.
stub_dir = pathlib.Path(tempfile.mkdtemp(prefix="stub_no_py_"))
stub = stub_dir / "python3"
stub.write_text("#!/bin/sh\nexit 1\n")
stub.chmod(0o755)
try:
    rc, _, err = run(
        STOP, stdin='{"x":1}',
        env_extra={"OPERATOR_SESSION_DIR": str(sd),
                   "PATH": f"{stub_dir}:{os.environ.get('PATH','')}"},
        timeout=10,
    )
finally:
    shutil.rmtree(stub_dir, ignore_errors=True)
replies = sd / "replies.jsonl"
ok_exit = rc == 0
ok_row = False
detail = f"rc={rc}, stderr={err.strip()[:120]!r}"
if replies.exists():
    rows = [json.loads(l) for l in replies.read_text().splitlines() if l.strip()]
    # Either kind=crashed (bash fallback fired) or kind=stop (ts_now's
    # `date` fallback worked AND we wrote SOMETHING). What matters is
    # SOMETHING was written so operator's tail doesn't hang.
    ok_row = len(rows) >= 1
    if rows:
        detail = f"row={rows[0]}"
expect("T3 no python3: still exits 0", ok_exit, detail)
expect("T3 no python3: still writes a row to replies.jsonl (crashed/raw)",
       ok_row, detail)
shutil.rmtree(sd, ignore_errors=True)

# T4: read-only session dir — exit 0, no crash (no row possible)
print("\nT4: read-only session dir")
sd = fresh_session_dir()
os.chmod(sd, 0o500)
try:
    rc, _, err = run(STOP, stdin='{"x":1}',
                     env_extra={"OPERATOR_SESSION_DIR": str(sd)})
    expect("T4 read-only dir: exits 0",
           rc == 0,
           f"rc={rc}, stderr={err.strip()[:200]!r}")
finally:
    os.chmod(sd, 0o700)
    shutil.rmtree(sd, ignore_errors=True)

# T5: no $OPERATOR_SESSION_DIR — no-op exit 0
print("\nT5: env var unset")
env = {k: v for k, v in os.environ.items() if k != "OPERATOR_SESSION_DIR"}
p = subprocess.run(["bash", str(STOP)], input=b'{"x":1}',
                   env=env, capture_output=True, timeout=5)
expect("T5 no env var: exits 0 silently",
       p.returncode == 0 and not p.stdout and not p.stderr,
       f"rc={p.returncode}")


# ----- summary ---------------------------------------------------------

print("\n" + "=" * 60)
fails = [r for r in results if r[0] == "FAIL"]
total = len(results)
print(f"{total - len(fails)}/{total} passed")
if fails:
    print("FAILURES:")
    for status, name, detail in fails:
        print(f"  - {name}: {detail}")
    sys.exit(1)
print("Phase 0 hook audit: all checks PASS")
sys.exit(0)
