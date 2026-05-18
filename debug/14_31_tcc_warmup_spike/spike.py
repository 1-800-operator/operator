"""S243 follow-up: measure macOS responsibility attribution per spawn mechanism.

For each of four spawn mechanisms (plain Popen, _disclaimed_spawn, open -W -n -a,
open -g -n -a), spawn the Operator helper, capture its PID, query
`responsibility_get_pid_responsible_for_pid` for it, and report:

  helper_pid   responsible_pid   responsible_comm   probe_result

`responsible_pid == helper_pid` (and comm == 'Operator') means the child is its
own responsible process — TCC will check against the helper's bundle identity
(correct). Any other value means TCC checks against the named parent, which is
the failure mode S243 hit.

The helper sleeps in its preflight (3s after CGRequestScreenCaptureAccess), so
we have a window to query responsibility. We also run --probe on a separate
spawn under each mechanism to confirm the probe result correlates with the
attribution.

Run: python debug/14_31_tcc_warmup_spike/spike.py
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from _1_800_operator.pipeline._disclaimed_spawn import (  # noqa: E402
    minimal_helper_env,
    spawn_disclaimed,
)

HELPER_APP = Path.home() / ".operator" / "bin" / "Operator.app"
HELPER_BIN = HELPER_APP / "Contents" / "MacOS" / "Operator"


def responsible_pid(pid: int) -> int:
    """Wrapper around the private libSystem API."""
    libc = ctypes.CDLL("/usr/lib/libSystem.dylib")
    fn = libc.responsibility_get_pid_responsible_for_pid
    fn.argtypes = [ctypes.c_int]
    fn.restype = ctypes.c_int
    return fn(pid)


def comm_of(pid: int) -> str:
    """Best-effort process command name lookup."""
    try:
        r = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True, text=True, timeout=2,
        )
        return r.stdout.strip() or "?"
    except (subprocess.SubprocessError, OSError):
        return "?"


def find_helper_pid_under_pgid(parent_pid: int, timeout: float = 5.0) -> int | None:
    """Poll for a child process under parent_pid whose comm is 'Operator'."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = subprocess.run(
                ["pgrep", "-x", "Operator"],
                capture_output=True, text=True, timeout=2,
            )
            for line in r.stdout.strip().splitlines():
                pid = int(line)
                # Verify it's actually our binary (could be name collisions)
                try:
                    exe = subprocess.run(
                        ["lsof", "-p", str(pid), "-Fn"],
                        capture_output=True, text=True, timeout=2,
                    ).stdout
                    if str(HELPER_BIN) in exe:
                        return pid
                except (subprocess.SubprocessError, OSError):
                    pass
        except (subprocess.SubprocessError, OSError):
            pass
        time.sleep(0.1)
    return None


def measure(mechanism: str, helper_pid: int) -> dict:
    """Capture attribution data while the helper is alive."""
    rpid = responsible_pid(helper_pid)
    rcomm = comm_of(rpid) if rpid > 0 else "?"
    helper_comm = comm_of(helper_pid)
    self_attributed = (rpid == helper_pid)
    return {
        "mechanism": mechanism,
        "helper_pid": helper_pid,
        "helper_comm": helper_comm,
        "responsible_pid": rpid,
        "responsible_comm": rcomm,
        "self_attributed": self_attributed,
    }


# -- Mechanism A: plain subprocess.Popen ------------------------------------

def test_plain_popen() -> dict:
    """Spawn helper via plain Popen — inherits the launching process's
    responsibility chain. This is the known-bad control case.
    """
    p = subprocess.Popen(
        [str(HELPER_BIN)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=minimal_helper_env(),
    )
    try:
        # Helper sleeps 3s in the screen-rec request; we have time
        time.sleep(0.5)
        m = measure("A: plain Popen", p.pid)
    finally:
        p.kill()
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    return m


# -- Mechanism B: _disclaimed_spawn -----------------------------------------

def test_disclaimed_spawn() -> dict:
    """Spawn helper via _disclaimed_spawn — child is its own responsible
    process. This is what slip-live uses.
    """
    p = spawn_disclaimed([str(HELPER_BIN)], env=minimal_helper_env())
    try:
        time.sleep(0.5)
        m = measure("B: _disclaimed_spawn", p.pid)
    finally:
        try:
            p.kill()
            p.wait(timeout=2)
        except Exception:
            pass
    return m


# -- Mechanism C: open -W -n -a (current install-time warmup) ---------------

def test_open_W_n_a() -> dict:
    """Launch helper via `open -W -n -a /path/to/Operator.app`.
    This is the current install-time warmup mechanism.
    """
    # Launch in background since `-W` waits and we need to query mid-flight
    p = subprocess.Popen(
        ["open", "-W", "-n", "-a", str(HELPER_APP)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Give launchd a moment to spawn the actual helper
        helper_pid = find_helper_pid_under_pgid(p.pid, timeout=5.0)
        if helper_pid is None:
            return {"mechanism": "C: open -W -n -a", "error": "helper PID not found"}
        m = measure("C: open -W -n -a", helper_pid)
    finally:
        # Helper exits on its own after preflight; kill the open wrapper
        try:
            subprocess.run(["pkill", "-x", "Operator"], capture_output=True, timeout=2)
        except (subprocess.SubprocessError, OSError):
            pass
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
    return m


# -- Mechanism D: open -g -n -a (background launch variant) -----------------

def test_open_g_n_a() -> dict:
    """Variant of C with `-g` (background, no foreground bring-up). Checks
    whether foreground/background changes responsibility attribution.
    """
    p = subprocess.Popen(
        ["open", "-g", "-n", "-a", str(HELPER_APP)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        helper_pid = find_helper_pid_under_pgid(p.pid, timeout=5.0)
        if helper_pid is None:
            return {"mechanism": "D: open -g -n -a", "error": "helper PID not found"}
        m = measure("D: open -g -n -a", helper_pid)
    finally:
        try:
            subprocess.run(["pkill", "-x", "Operator"], capture_output=True, timeout=2)
        except (subprocess.SubprocessError, OSError):
            pass
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
    return m


def main() -> int:
    if not HELPER_BIN.exists():
        print(f"FAIL: helper not found at {HELPER_BIN}")
        print("Run scripts/build_signed_helper.sh first.")
        return 1

    print(f"This process PID: {os.getpid()}")
    print(f"This process responsible: {responsible_pid(os.getpid())} ({comm_of(responsible_pid(os.getpid()))})")
    print()

    results = []
    for fn, label in [
        (test_plain_popen, "A: plain Popen"),
        (test_disclaimed_spawn, "B: _disclaimed_spawn"),
        (test_open_W_n_a, "C: open -W -n -a"),
        (test_open_g_n_a, "D: open -g -n -a"),
    ]:
        print(f"--- Running {label} ---")
        try:
            r = fn()
        except Exception as e:
            r = {"mechanism": label, "error": f"{type(e).__name__}: {e}"}
        results.append(r)
        print(json.dumps(r, indent=2))
        print()

    # Summary table
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"{'Mechanism':<28} {'Helper PID':>10} {'Resp PID':>10} {'Resp comm':<24} {'Self?':>6}")
    for r in results:
        if "error" in r:
            print(f"{r['mechanism']:<28} ERROR: {r['error']}")
            continue
        print(
            f"{r['mechanism']:<28} {r['helper_pid']:>10} {r['responsible_pid']:>10} "
            f"{r['responsible_comm']:<24} {'✓' if r['self_attributed'] else '✗':>6}"
        )

    # Save results JSON for the writeup
    out = Path(__file__).parent / "results.json"
    out.write_text(json.dumps(results, indent=2))
    print(f"\nSaved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
