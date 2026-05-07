"""Run the notarized helper via plain subprocess.Popen (no disclaim).

If [S] callbacks fire here but NOT via AttachAdapter._start_audio_pipeline
(which uses _disclaimed_spawn), then the disclaim path is interfering with
SCStream now that the helper is notarized as its own bundle.

Make sure system audio is actively playing during the run.
"""
import subprocess
import time

HELPER = "/Users/jojo/.operator/bin/operator-audio-capture.app/Contents/MacOS/operator-audio-capture"

print(f">>> spawning {HELPER} via plain subprocess.Popen (no disclaim)")
proc = subprocess.Popen(
    [HELPER],
    stdin=subprocess.PIPE,
    stdout=subprocess.DEVNULL,  # raw frames go nowhere; we only care about stderr stats
    stderr=subprocess.PIPE,
    bufsize=0,
)

print(">>> letting it run for 12s (system audio should be playing)")
time.sleep(12.0)

print(">>> closing stdin to signal shutdown")
proc.stdin.close()
try:
    proc.wait(timeout=3.0)
except subprocess.TimeoutExpired:
    proc.terminate()
    proc.wait(timeout=2.0)

stderr = proc.stderr.read().decode("utf-8", errors="replace")
print(">>> helper stderr:")
print(stderr)
