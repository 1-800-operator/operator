#!/usr/bin/env python3
"""Verify the no-rebuild warmup fix: reset grants, run the NEW
_run_audio_tcc_warmup (source __main__ + installed helper), and time how long
it takes to return after the user clicks both dialogs.

PASS = returns within a few seconds of the second click (not ~120s).
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, "src")
from _1_800_operator.__main__ import _run_audio_tcc_warmup  # noqa: E402

BUNDLE = "com.1-800-operator.audio-capture"

for svc in ("Microphone", "AudioCapture"):
    subprocess.run(["tccutil", "reset", svc, BUNDLE],
                   capture_output=True, text=True)
print("grants reset — two dialogs (Microphone, then System Audio) will appear.",
      flush=True)
print("click ALLOW on each as soon as you see it.\n", flush=True)

t0 = time.monotonic()
sa, mic = _run_audio_tcc_warmup()
dt = time.monotonic() - t0

print(f"\nwarmup returned after {dt:.1f}s  →  system_audio={sa}  microphone={mic}",
      flush=True)
verdict = "PASS" if (dt < 20 and sa == "ok" and mic == "ok") else "CHECK"
print(f"VERDICT: {verdict}  (PASS = both ok and well under the old ~120s)",
      flush=True)
