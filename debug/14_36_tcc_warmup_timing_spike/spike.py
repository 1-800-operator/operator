#!/usr/bin/env python3
"""14.36 — TCC warmup timing spike.

Question: why does the install-time audio-helper warmup make the user wait
the full ~60s after clicking Allow on EACH dialog (mic + system audio),
instead of moving on the instant they click?

Diagnosis from code-read (operator-audio-capture.swift, S247 v0.1.29):
  - Mic leg  (:225): sema.wait(timeout: .now() + 60), early-bail when the
    AVCaptureDevice.requestAccess completion handler signals the semaphore.
  - Sys leg  (:381): poll TCCAccessPreflight up to 60s, early-bail when it
    flips from not_determined → ok.
Both early-bails depend on an IN-PROCESS signal flipping promptly after the
user clicks. Hypothesis: neither fires in-process (mic completion not
serviced because the main thread is blocked before RunLoop.main.run();
system TCCAccessPreflight returns a per-process-cached value that never
updates after the grant), so both legs run to the full 60s deadline and
recover only via a post-timeout fresh status read. install.sh still prints
"granted" because PROBE_AFTER is a FRESH process (non-stale preflight).

This spike reproduces the cold path and TIMESTAMPS the helper's own stderr
breadcrumbs so we can see, per leg, whether the early-bail signal fired or
the 60s deadline was hit — and what value the system leg resolved to.

Mechanism note: production warmup uses `open -W -n -a` (no stderr capture).
We use spawn_disclaimed so we can capture stderr. Both self-attribute to the
helper bundle (proven in 14_31). If the dialogs DON'T appear under disclaim,
that itself is a finding — abort and note it.

Run:  python3 debug/14_36_tcc_warmup_timing_spike/spike.py
Resets ONLY com.1-800-operator.audio-capture's two grants; re-grant = one
click each next time you use dial (or re-run install.sh).
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "src"))
from _1_800_operator.pipeline._disclaimed_spawn import (  # noqa: E402
    minimal_helper_env,
    spawn_disclaimed,
)

HELPER = os.path.expanduser("~/.operator/bin/Operator.app/Contents/MacOS/Operator")
BUNDLE = "com.1-800-operator.audio-capture"
EVENTS_LOG = os.path.join(HERE, "events.log")
MAX_WALL = 150.0  # safety: kill the run after this many seconds no matter what

# Markers we want timestamps for, mapped to a short label.
MARKERS = {
    "Microphone permission not determined": "mic_requesting",
    "Microphone access granted=": "mic_completion_fired",
    "waiting for Audio Capture grant": "sys_poll_start",
    "Audio Capture grant resolved:": "sys_resolved",
    "AVCaptureSession running": "mic_session_up",
    "system-audio tap capturing": "sys_tap_up",
}


def probe_disclaimed() -> str:
    """Read the helper's OWN grant state (disclaimed so TCC keys on the
    helper bundle, not this IDE's responsibility chain)."""
    try:
        p = spawn_disclaimed([HELPER, "--probe"], env=minimal_helper_env())
        data = p.stdout.read(4096).decode("utf-8", errors="replace").strip()
        p.wait(timeout=10)
        return data or "(empty)"
    except Exception as e:  # noqa: BLE001
        return f"(probe error: {e})"


def main() -> int:
    if not os.path.exists(HELPER):
        print(f"FATAL: helper not found at {HELPER}", file=sys.stderr)
        return 1

    print("=" * 70)
    print("14.36 TCC warmup timing spike")
    print("=" * 70)
    print(f"helper:  {HELPER}")
    print(f"BEFORE reset:  {probe_disclaimed()}")

    # --- reset only this bundle's two grants -------------------------------
    for svc in ("Microphone", "AudioCapture"):
        r = subprocess.run(
            ["tccutil", "reset", svc, BUNDLE],
            capture_output=True, text=True,
        )
        out = (r.stdout + r.stderr).strip()
        print(f"tccutil reset {svc} {BUNDLE}: rc={r.returncode}  {out}")

    after = probe_disclaimed()
    print(f"AFTER reset:   {after}")
    if '"microphone":"not_determined"' not in after:
        print("\n⚠️  Mic did not reset to not_determined — the cold-path may not")
        print("    reproduce. (tccutil sometimes needs the controlling process")
        print("    closed.) Continuing anyway; read results with that caveat.")
    print()
    print(">>> Two dialogs will appear: Microphone, then System Audio Recording.")
    print(">>> Click ALLOW on each AS SOON as it appears. I'm timestamping")
    print(">>> everything relative to helper launch (t=0.0).")
    print()

    # --- launch helper, capture stderr with timestamps ---------------------
    err_r, err_w = os.pipe()
    proc = spawn_disclaimed([HELPER], env=minimal_helper_env(), stderr_fd=err_w)
    os.close(err_w)  # parent drops the write end so EOF propagates on exit
    t0 = time.monotonic()

    # Drain framed-PCM stdout in the background so a full pipe never blocks
    # the helper's capture callbacks (which start right after grants resolve).
    def _drain_stdout():
        try:
            while proc.stdout.read(65536):
                pass
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_drain_stdout, daemon=True).start()

    timings: dict[str, float] = {}
    sys_resolved_value: str | None = None

    logf = open(EVENTS_LOG, "w")
    logf.write(f"# 14.36 spike  helper={HELPER}\n# AFTER reset: {after}\n")

    errf = os.fdopen(err_r, "r", errors="replace")
    saw_both = False

    def _watchdog():
        time.sleep(MAX_WALL)
        try:
            proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_watchdog, daemon=True).start()

    for line in errf:
        t = time.monotonic() - t0
        line = line.rstrip("\n")
        stamped = f"[t={t:6.2f}] {line}"
        print(stamped)
        logf.write(stamped + "\n")
        logf.flush()
        for needle, label in MARKERS.items():
            if needle in line and label not in timings:
                timings[label] = t
                if label == "sys_resolved":
                    sys_resolved_value = line.split("resolved:", 1)[-1].strip()
        # Once both legs are up (or sys resolved + mic session running), stop.
        if ("sys_resolved" in timings or "sys_tap_up" in timings) and (
            "mic_session_up" in timings or "mic_completion_fired" in timings
        ):
            if not saw_both:
                saw_both = True
                # let a couple more lines flush, then close stdin → helper exits
                threading.Timer(1.5, lambda: _safe_close(proc)).start()

    logf.close()
    try:
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        proc.terminate()

    # --- summary -----------------------------------------------------------
    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)

    def show(label: str, desc: str) -> None:
        v = timings.get(label)
        print(f"  {desc:<42} {'%.2fs' % v if v is not None else '— (never)'}")

    show("mic_requesting", "mic: dialog requested at")
    show("mic_completion_fired", "mic: requestAccess completion fired at")
    show("mic_session_up", "mic: AVCaptureSession running at")
    show("sys_poll_start", "sys: entered 60s preflight poll at")
    show("sys_resolved", "sys: poll resolved at")
    print(f"  sys: poll resolved VALUE                   {sys_resolved_value or '— (never)'}")
    show("sys_tap_up", "sys: tap capturing at")

    print()
    print("INTERPRETATION:")
    mc = timings.get("mic_completion_fired")
    if mc is not None:
        verdict = "DELAYED (~60s timeout)" if mc >= 45 else "PROMPT (early-bail worked)"
        print(f"  - Mic completion handler: {verdict}  (fired at {mc:.1f}s)")
    sr = timings.get("sys_resolved")
    if sr is not None:
        if sys_resolved_value == "ok" and sr < 45:
            print(f"  - System preflight: PROMPT — saw grant in-process at {sr:.1f}s")
        elif sr >= 45:
            print(f"  - System preflight: STALE — hit 60s deadline (resolved "
                  f"'{sys_resolved_value}') at {sr:.1f}s")
        else:
            print(f"  - System resolved '{sys_resolved_value}' at {sr:.1f}s")
    print()
    print(f"FINAL grant state (fresh probe): {probe_disclaimed()}")
    print(f"events log: {EVENTS_LOG}")
    return 0


def _safe_close(proc) -> None:
    try:
        proc.stdin.close()
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    raise SystemExit(main())
